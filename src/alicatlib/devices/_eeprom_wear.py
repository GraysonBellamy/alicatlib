"""EEPROM-wear-rate guard for commands carrying ``save=True``.

Several Alicat commands persist to the device's EEPROM when their
request's ``save`` flag is true (Active Gas, PID/PDF gains, deadband,
batch, valve offset, totalizer save, setpoint source, …). EEPROM has
finite write endurance, and a poll loop that forgets to set
``save=False`` can burn through that budget silently. Per design
§5.20.7, the library detects accidental save-in-a-loop patterns and
logs a WARN when the per-device rate exceeds a configurable threshold
(``AlicatConfig.save_rate_warn_per_min``, default 10/min).

This module provides the monitor used by :class:`Session.execute` just
before dispatch. It runs pre-I/O, cheap (``O(1)`` amortised), and
produces no network traffic or external state — if the logger has no
handlers attached the guard is effectively free.
"""

from __future__ import annotations

from collections import deque
from time import monotonic
from typing import TYPE_CHECKING, Any, Final

from alicatlib._logging import get_logger

if TYPE_CHECKING:
    from alicatlib.commands.base import Command

_WINDOW_SECONDS: Final[float] = 60.0


class EepromWearMonitor:
    """Rolling 60 s window of ``save=True`` commands per :class:`Session`.

    Call :meth:`record` once per :meth:`Session.execute`, passing the
    command spec and the caller's request object. If ``request.save`` is
    True, the timestamp is appended; if the rolling count exceeds
    ``warn_per_minute``, a WARN log fires on the ``alicatlib.session``
    logger with structured ``extra`` fields for downstream filtering.

    The guard re-logs at most once per threshold crossing — after a
    warning fires, no further warnings emit until the window drops
    back below the threshold and crosses again. This keeps hot loops
    from flooding the log with duplicate warnings while still surfacing
    new episodes.

    Attributes:
        unit_id: Device unit id used in the log extra.
        warn_per_minute: Threshold for triggering the WARN log. A value
            of ``0`` disables monitoring entirely (the monitor still
            records hits but never logs).
    """

    __slots__ = ("_hits", "_tripped", "unit_id", "warn_per_minute")

    def __init__(self, *, unit_id: str, warn_per_minute: int) -> None:
        self.unit_id = unit_id
        self.warn_per_minute = warn_per_minute
        self._hits: deque[tuple[float, str]] = deque()
        self._tripped: bool = False

    def record(self, command: Command[Any, Any], request: object) -> None:
        """Note a dispatch; warn if the save rate is over the threshold.

        Safe to call for every command — the ``save`` check short-circuits
        when the request either has no ``save`` attribute or its value
        is not exactly ``True`` (the sentinel-vs-bool distinction matters:
        ``None`` means "not requested", ``False`` means "explicitly don't
        save"; neither should trip the guard).
        """
        if self.warn_per_minute <= 0:
            return
        save_flag = getattr(request, "save", None)
        if save_flag is not True:
            return

        now = monotonic()
        self._evict_before(now - _WINDOW_SECONDS)
        self._hits.append((now, command.name))

        count = len(self._hits)
        if count > self.warn_per_minute:
            if not self._tripped:
                self._tripped = True
                get_logger("session").warning(
                    "EEPROM-wear guard: %d save=True commands in the last %.0f s",
                    count,
                    _WINDOW_SECONDS,
                    extra={
                        "unit_id": self.unit_id,
                        "command": command.name,
                        "save_count_window": count,
                        "warn_per_minute": self.warn_per_minute,
                    },
                )
        else:
            # Window dropped back below threshold — arm the guard again
            # so the next crossing produces a fresh warning.
            self._tripped = False

    def _evict_before(self, cutoff: float) -> None:
        hits = self._hits
        while hits and hits[0][0] < cutoff:
            hits.popleft()
