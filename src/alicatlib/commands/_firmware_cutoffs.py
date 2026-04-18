"""Shared firmware cutoff constants + helpers for legacy-path dispatch.

Several Alicat commands introduced new forms at a firmware boundary:

- ``GS`` (modern Active Gas) at ``V10`` 10v05; older hardware and other
  families keep the legacy ``G``.
- ``LS`` (modern setpoint) at ``V8_V9`` 9v00; earlier firmware uses ``S``.

The paired-command pattern (design §5.4) routes to the modern spec when
firmware supports it, falls back to legacy otherwise. Both predicates
here are family-aware — ``FirmwareVersion`` refuses to order across
families (design §5.10), so the helpers centralise the "same family and
high enough" check and keep the callers from reinventing it.

This module defines *only* the firmware constants and predicates.
Command specs (``GAS_SELECT`` / ``GAS_SELECT_LEGACY`` / ``SETPOINT`` /
``SETPOINT_LEGACY``) live alongside each other under the command-group
file (``commands/gas.py``, ``commands/setpoint.py``) and import these
constants at the top.
"""

from __future__ import annotations

from typing import Final

from alicatlib.firmware import FirmwareFamily, FirmwareVersion

__all__ = [
    "MIN_FIRMWARE_DCU",
    "MIN_FIRMWARE_GAS_SELECT",
    "MIN_FIRMWARE_LSS",
    "MIN_FIRMWARE_SETPOINT_LS",
    "uses_modern_gas_select",
    "uses_modern_setpoint",
]


#: Cut-over to the modern :data:`~alicatlib.commands.gas.GAS_SELECT` (``GS``).
#: Only meaningful inside :attr:`FirmwareFamily.V10`; other families never
#: see ``GS``. Primer lists this at 10v05.
MIN_FIRMWARE_GAS_SELECT: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=5,
    raw="10v05",
)


#: Minimum firmware for ``DCU`` unit-query semantics (primer's 10v05+
#: annotation). Pre-10v05 devices either reject ``DCU`` outright or
#: respond with raw ADC counts under an entirely different command
#: shape (design §16.6.2/§16.6.3/§16.6.5); neither parses as a
#: :class:`UnitSetting`, so the session must gate pre-I/O.
MIN_FIRMWARE_DCU: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=5,
    raw="10v05",
)


#: Minimum firmware for ``LSS`` setpoint-source query (10v05+ per primer).
#: Pre-10v05 devices tend to fall through to the full data frame under
#: display-locked ``LCK`` state (design §16.6.3 / §16.6.5), which fails
#: the :class:`SetpointSourceResult` 2-field decoder. Firmware-gating
#: at the command spec keeps that path out of the wire entirely.
MIN_FIRMWARE_LSS: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=5,
    raw="10v05",
)


#: Cut-over to the modern ``LS`` setpoint command inside :attr:`FirmwareFamily.V8_V9`.
#: All of :attr:`FirmwareFamily.V10` supports ``LS``, so the predicate
#: below treats V10 unconditionally-modern.
MIN_FIRMWARE_SETPOINT_LS: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V8_V9,
    major=9,
    minor=0,
    raw="9v00",
)


def uses_modern_gas_select(firmware: FirmwareVersion) -> bool:
    """Return ``True`` iff ``firmware`` supports ``GS`` rather than legacy ``G``.

    Family-aware: GP / V1_V7 / V8_V9 devices never reach ``GS`` regardless
    of their numeric version, so the predicate returns ``False`` without
    attempting a cross-family comparison.
    """
    if firmware.family is not FirmwareFamily.V10:
        return False
    return firmware >= MIN_FIRMWARE_GAS_SELECT


def uses_modern_setpoint(firmware: FirmwareVersion) -> bool:
    """Return ``True`` iff ``firmware`` supports ``LS`` rather than legacy ``S``.

    :attr:`FirmwareFamily.V10` always uses ``LS``; :attr:`FirmwareFamily.V8_V9`
    uses ``LS`` from :data:`MIN_FIRMWARE_SETPOINT_LS` onward; earlier families
    (V1_V7, GP) use legacy ``S``.
    """
    if firmware.family is FirmwareFamily.V10:
        return True
    if firmware.family is FirmwareFamily.V8_V9:
        return firmware >= MIN_FIRMWARE_SETPOINT_LS
    return False
