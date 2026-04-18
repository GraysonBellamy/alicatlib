"""Core package configuration.

Keeping this as a plain dataclass (rather than ``pydantic-settings``) keeps the
core install free of any validation-library dependency; the tradeoff is that
env coercion lives in :func:`config_from_env` rather than being automatic.

Design reference: ``docs/design.md`` §5.18.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Final, Self

DEFAULT_ENV_PREFIX: Final[str] = "ALICATLIB_"


@dataclass(frozen=True, slots=True)
class AlicatConfig:
    """Process-wide default settings.

    Individual sessions may override any of these at construction time.

    Attributes:
        default_timeout_s: Default per-command response timeout for single-line
            commands, in seconds.
        multiline_timeout_s: Default response timeout for multiline table
            commands (``??M*``, ``??D*``, gas list), in seconds. Larger than
            ``default_timeout_s`` because the table commands are paced at
            device speed across 5–20 lines.
        write_timeout_s: Upper bound on a single ``Transport.write`` call, in
            seconds. Writes can block on RS-485 hardware flow control, a
            hung device, or a TCP transport's send buffer; this bounds that.
        default_baudrate: Default serial baudrate when none is specified.
        drain_before_write: Whether the protocol client should drain any stale
            input bytes before each command. Useful for re-syncing after a
            timeout; adds latency per command.
        save_rate_warn_per_min: EEPROM-wear warning threshold. Any command
            carrying a ``save=True`` flag (active gas, PID/PDF gains, deadband,
            batch, valve offset, totalizer save, setpoint source, …) logs at
            WARN when fired more than this many times per minute per device.
            See design §5.20.7.
        eager_tasks: Opt-in to ``asyncio.eager_task_factory`` on the running
            event loop. Skips one event-loop round-trip when a newly-created
            task's first ``await`` doesn't suspend — a measurable win under
            tight command loops. Off by default because it is a scheduling
            semantic change (tasks that return before their first suspension
            never hit the loop). No-op on trio. See design §5.2 and the
            :func:`alicatlib._runtime.install_eager_task_factory` helper
            users call near app startup.
    """

    default_timeout_s: float = 0.5
    multiline_timeout_s: float = 1.0
    write_timeout_s: float = 0.5
    default_baudrate: int = 19200
    drain_before_write: bool = False
    save_rate_warn_per_min: int = 10
    eager_tasks: bool = False

    def replace(self, **updates: Any) -> Self:
        """Return a copy of this config with ``updates`` applied."""
        return replace(self, **updates)


def config_from_env(prefix: str = DEFAULT_ENV_PREFIX) -> AlicatConfig:
    """Best-effort env loader.

    Only reads well-known keys; unknown keys are ignored. Missing or
    unparseable values fall back to :class:`AlicatConfig`'s defaults — this
    function never raises. Use explicit dataclass construction when you need
    strict validation.

    Recognised keys (with ``prefix="ALICATLIB_"``):

    - ``ALICATLIB_DEFAULT_TIMEOUT_S`` — float seconds
    - ``ALICATLIB_MULTILINE_TIMEOUT_S`` — float seconds
    - ``ALICATLIB_WRITE_TIMEOUT_S`` — float seconds
    - ``ALICATLIB_DEFAULT_BAUDRATE`` — int
    - ``ALICATLIB_DRAIN_BEFORE_WRITE`` — ``"1"`` / ``"true"`` / ``"yes"``
    - ``ALICATLIB_SAVE_RATE_WARN_PER_MIN`` — int
    - ``ALICATLIB_EAGER_TASKS`` — ``"1"`` / ``"true"`` / ``"yes"``

    Args:
        prefix: Prefix to prepend to each env key. Defaults to ``"ALICATLIB_"``.

    Returns:
        An :class:`AlicatConfig`, falling back to defaults for any missing or
        unparseable env var.
    """
    base = AlicatConfig()

    timeout = _float_env(f"{prefix}DEFAULT_TIMEOUT_S", base.default_timeout_s)
    multiline_timeout = _float_env(f"{prefix}MULTILINE_TIMEOUT_S", base.multiline_timeout_s)
    write_timeout = _float_env(f"{prefix}WRITE_TIMEOUT_S", base.write_timeout_s)
    baudrate = _int_env(f"{prefix}DEFAULT_BAUDRATE", base.default_baudrate)
    drain = _bool_env(f"{prefix}DRAIN_BEFORE_WRITE", base.drain_before_write)
    save_rate = _int_env(f"{prefix}SAVE_RATE_WARN_PER_MIN", base.save_rate_warn_per_min)
    eager = _bool_env(f"{prefix}EAGER_TASKS", base.eager_tasks)

    return AlicatConfig(
        default_timeout_s=timeout,
        multiline_timeout_s=multiline_timeout,
        write_timeout_s=write_timeout,
        default_baudrate=baudrate,
        drain_before_write=drain,
        save_rate_warn_per_min=save_rate,
        eager_tasks=eager,
    )


def _float_env(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_TRUE_STRS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRS = frozenset({"0", "false", "no", "off", ""})


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE_STRS:
        return True
    if lowered in _FALSE_STRS:
        return False
    return default
