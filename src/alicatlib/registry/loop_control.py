"""Loop-control variable — restricted subset of :class:`Statistic` for ``LV``.

The ``LV`` command (primer §Loop Control Variable, 9v00+) accepts only a
short list of statistics as the quantity a controller's loop tracks.
Modelling this as its own enum rather than reusing :class:`Statistic`
keeps the surface honest: an invalid selection (e.g. ``MASS_FLOW`` for a
pressure controller, or any non-setpoint statistic) is a typed miss at
the call site, not a device rejection after I/O.

See ``docs/design.md`` §5.4 (argument-range validation) and §9 Tier 1.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

from alicatlib.errors import AlicatValidationError
from alicatlib.registry._codes_gen import STATISTIC_BY_CODE, Statistic

__all__ = [
    "LOOP_CONTROL_VARIABLE_CODES",
    "LoopControlVariable",
    "coerce_loop_control_variable",
]


class LoopControlVariable(IntEnum):
    """Statistics a controller's feedback loop can track.

    Values are the primer's statistic codes, so ``LV <value>`` over the
    wire is a direct ``str(member.value)``. The members mirror the
    :class:`Statistic` names they correspond to — ``LoopControlVariable.MASS_FLOW_SETPT``
    matches :data:`Statistic.MASS_FLOW_SETPT` (code 37).
    """

    ABS_PRESS_SETPT = 34
    VOL_FLOW_SETPT = 36
    MASS_FLOW_SETPT = 37
    GAUGE_PRESS_SETPT = 38
    DIFF_PRESS_SETPT = 39
    ABS_PRESS_SECOND_SETPT = 345
    GAUGE_PRESS_SECOND_SETPT = 353
    DIFF_PRESS_SECOND_SETPT = 361

    @property
    def statistic(self) -> Statistic:
        """The :class:`Statistic` member that shares this wire code."""
        return STATISTIC_BY_CODE[int(self)]


LOOP_CONTROL_VARIABLE_CODES: Final[frozenset[int]] = frozenset(m.value for m in LoopControlVariable)
"""Wire codes accepted by ``LV``. Useful for pre-I/O gating on raw input."""


_BY_NAME: Final[dict[str, LoopControlVariable]] = {m.name.lower(): m for m in LoopControlVariable}


def coerce_loop_control_variable(
    value: LoopControlVariable | Statistic | str | int,
) -> LoopControlVariable:
    """Resolve ``value`` to a :class:`LoopControlVariable`.

    Accepts:

    - a :class:`LoopControlVariable` member (returned unchanged);
    - a :class:`Statistic` member whose code is in the LV subset;
    - an ``int`` wire code (one of :data:`LOOP_CONTROL_VARIABLE_CODES`);
    - a ``str`` matching a member name case-insensitively (``"mass_flow_setpt"``)
      or a :class:`Statistic` member value (``"mass_flow_setpt"``).

    Raises:
        AlicatValidationError: ``value`` is not one of the eight LV-eligible
            statistics. The error is validation-level (not
            ``UnknownStatisticError``) because the surface is deliberately
            a restricted subset — the wider ``Statistic`` enum is fine,
            just not valid *here*.
    """
    if isinstance(value, LoopControlVariable):
        return value
    if isinstance(value, Statistic):
        code = _statistic_code(value)
        try:
            return LoopControlVariable(code)
        except ValueError:
            raise _not_eligible(value.value) from None
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return LoopControlVariable(value)
        except ValueError:
            raise _not_eligible(value) from None
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _BY_NAME:
            return _BY_NAME[key]
        raise _not_eligible(value)
    raise TypeError(
        f"cannot coerce {type(value).__name__} to LoopControlVariable",
    )


def _statistic_code(stat: Statistic) -> int:
    for code, member in STATISTIC_BY_CODE.items():
        if member is stat:
            return code
    # Every Statistic member is codegen'd from a code, so the reverse lookup
    # should always succeed. If it doesn't, codes.json and _codes_gen.py
    # drifted — surface it loudly rather than pretending the statistic is
    # "not eligible".
    raise AssertionError(f"Statistic {stat!r} has no code in STATISTIC_BY_CODE")


def _not_eligible(value: object) -> AlicatValidationError:
    eligible = ", ".join(m.name for m in LoopControlVariable)
    return AlicatValidationError(
        f"{value!r} is not a valid loop-control variable; choose one of: {eligible}",
    )
