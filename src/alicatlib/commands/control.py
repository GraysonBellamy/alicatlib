r"""Control-setup commands — primer §Control Setup Controllers.

Two Tier-2 commands ship here; both are controller-only. The remaining
Control-Setup surface (``LCDM`` / ``LCA`` / ``LCGD`` / ``LCG`` / ``LCVO``
/ ``LSRC`` / ``LCZA`` / ``OPL`` / ``TB``) is planned future work — primer
text and quick-reference tables disagree on ``LDM`` vs ``LCDM`` for
deadband mode, and gain / valve-offset commands are destructive in a
way that benefits from hardware validation before shipping.

- :data:`RAMP_RATE` (``SR``, 7v11+) — query or set the max ramp rate
  the controller uses when moving to a new setpoint. ``max_ramp=0``
  disables ramping ("jump to setpoint"). Requires a
  :class:`TimeUnit` alongside the rate.
- :data:`DEADBAND_LIMIT` (``LCDB``, 10v05+) — query or set the
  allowable drift around the setpoint. ``deadband_limit=0`` disables
  the deadband. Accepts a ``save`` flag that feeds through the
  session's EEPROM-wear guard (design §5.20.7).

Design reference: ``docs/design.md`` §9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import DeadbandSetting, RampRateSetting, TimeUnit
from alicatlib.errors import (
    AlicatValidationError,
    ErrorContext,
    UnknownUnitError,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_fields, parse_float, parse_int
from alicatlib.registry import Unit, unit_registry
from alicatlib.registry._codes_gen import UNIT_BY_CATEGORY_CODE

__all__ = [
    "DEADBAND_LIMIT",
    "RAMP_RATE",
    "DeadbandLimit",
    "DeadbandLimitRequest",
    "RampRate",
    "RampRateRequest",
]


_CONTROLLER_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(
    {DeviceKind.FLOW_CONTROLLER, DeviceKind.PRESSURE_CONTROLLER},
)


# Primer p. 15: ``SR`` is 7v11+ within V1_V7; V8_V9 and V10 support it
# unconditionally. Family-scoped ``min_firmware`` only gates pre-7v11
# V1_V7.
_MIN_FIRMWARE_RAMP_RATE: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V1_V7,
    major=7,
    minor=11,
    raw="7v11",
)


# Primer p. 14: ``LCDB`` is 10v05+; only V10 supports it.
_MIN_FIRMWARE_DEADBAND: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=5,
    raw="10v05",
)


def _resolve_unit_label(label: str, code: int) -> Unit | None:
    """Best-effort resolve a wire unit label + code to a typed :class:`Unit`.

    Mirrors the shared helper in :mod:`alicatlib.commands.units` but
    kept local to avoid a cross-module import at command-spec-definition
    time. Labels that don't resolve against the registry return
    ``None``; callers still get the raw label on the result object for
    diagnostics.
    """
    try:
        return unit_registry.coerce(label)
    except UnknownUnitError:
        pass
    matches = {u for (_cat, c), u in UNIT_BY_CATEGORY_CODE.items() if c == code}
    if len(matches) == 1:
        return next(iter(matches))
    return None


# ---------------------------------------------------------------------------
# RAMP_RATE (``SR``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RampRateRequest:
    """Arguments for :data:`RAMP_RATE`.

    Attributes:
        max_ramp: Ramp step size in the current engineering units.
            ``0.0`` disables ramping; negative values are rejected
            pre-I/O. ``None`` issues the query form.
        time_unit: Time base (:class:`TimeUnit`) — required when
            ``max_ramp`` is set; silently ignored on query. Primer p. 15
            says the time-unit parameter is required *even when disabling*
            ramping, so the encoder enforces presence whenever
            ``max_ramp`` is not ``None``.
    """

    max_ramp: float | None = None
    time_unit: TimeUnit | None = None


@dataclass(frozen=True, slots=True)
class RampRate(Command[RampRateRequest, RampRateSetting]):
    r"""``SR`` — max ramp rate query/set (7v11+).

    Wire shape:

    - Query: ``<uid><prefix>SR\r``
    - Set:   ``<uid><prefix>SR <max_ramp> <time_unit_code>\r``

    Response (primer p. 15, hardware-correctable): 5 fields —
    ``<uid> <max_ramp> <setpoint_unit_code> <time_value> <rate_unit_label>``
    (e.g. ``A 25.0 12 4 SCCM/s``).
    """

    name: str = "ramp_rate"
    token: str = "SR"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_RAMP_RATE
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(self, ctx: DecodeContext, request: RampRateRequest) -> bytes:
        """Emit the SR query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.max_ramp is None:
            # Query — ``time_unit`` is silently ignored here (set-only
            # semantics). Primer has no query-form ``time_unit``.
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

        if request.max_ramp < 0:
            raise AlicatValidationError(
                f"{self.name}: max_ramp must be >= 0 (0 disables), got {request.max_ramp}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"max_ramp": request.max_ramp},
                ),
            )
        if request.time_unit is None:
            raise AlicatValidationError(
                f"{self.name}: time_unit is required when setting max_ramp "
                "(including when disabling ramping via max_ramp=0); pass a "
                "TimeUnit member.",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"max_ramp": request.max_ramp},
                ),
            )
        return (
            f"{ctx.unit_id}{prefix}{self.token} {request.max_ramp} {int(request.time_unit)}\r"
        ).encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> RampRateSetting:
        """Parse the 5-field reply into :class:`RampRateSetting`."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=5)
        unit_id, ramp_s, unit_code_s, time_s, rate_label = fields
        max_ramp = parse_float(ramp_s, field=f"{self.name}.max_ramp")
        setpoint_unit_code = parse_int(unit_code_s, field=f"{self.name}.setpoint_unit_code")
        time_code = parse_int(time_s, field=f"{self.name}.time_unit")
        try:
            time_unit = TimeUnit(time_code)
        except ValueError as err:
            raise AlicatValidationError(
                f"{self.name}: device returned time-unit code {time_code!r} "
                f"not in TimeUnit enum {sorted(int(m) for m in TimeUnit)}",
                context=ErrorContext(
                    command_name=self.name,
                    raw_response=response,
                    extra={"time_code": time_code},
                ),
            ) from err

        # The rate label is ``<setpoint_unit>/<time_unit>`` (e.g.
        # ``SCCM/s``). Derive the typed setpoint unit from the
        # setpoint-unit code; the registry is the authoritative source
        # for ``setpoint_unit``, and the rate label is kept as the raw
        # diagnostic string.
        matches = {u for (_cat, c), u in UNIT_BY_CATEGORY_CODE.items() if c == setpoint_unit_code}
        setpoint_unit: Unit | None = next(iter(matches)) if len(matches) == 1 else None

        return RampRateSetting(
            unit_id=unit_id,
            max_ramp=max_ramp,
            setpoint_unit_code=setpoint_unit_code,
            setpoint_unit=setpoint_unit,
            time_unit=time_unit,
            rate_unit_label=rate_label,
        )


RAMP_RATE: RampRate = RampRate()


# ---------------------------------------------------------------------------
# DEADBAND_LIMIT (``LCDB``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeadbandLimitRequest:
    """Arguments for :data:`DEADBAND_LIMIT`.

    Attributes:
        deadband: Acceptable drift around the setpoint in the
            controlled variable's engineering units. ``0.0`` disables
            the deadband; negative values are rejected pre-I/O.
            ``None`` issues the query form.
        save: ``True`` persists the new value to EEPROM (subject to
            the :attr:`AlicatConfig.save_rate_warn_per_min` rate-warn
            guard); ``False`` / ``None`` keeps the change volatile.
            Primer requires ``save`` in the *first* wire position on
            set, so the encoder emits ``0``/``1`` in that slot; ``None``
            defaults to ``0`` (volatile) per primer-safe behaviour.
    """

    deadband: float | None = None
    save: bool | None = None


@dataclass(frozen=True, slots=True)
class DeadbandLimit(Command[DeadbandLimitRequest, DeadbandSetting]):
    r"""``LCDB`` — deadband-limit query/set (10v05+).

    Wire shape (primer p. 14):

    - Query: ``<uid><prefix>LCDB\r``
    - Set:   ``<uid><prefix>LCDB <save> <deadband_limit>\r``

    Response: ``<uid> <current_deadband> <unit_code> <unit_label>``
    (4 fields).
    """

    name: str = "deadband_limit"
    token: str = "LCDB"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_DEADBAND
    firmware_families: frozenset[FirmwareFamily] = frozenset({FirmwareFamily.V10})

    def encode(self, ctx: DecodeContext, request: DeadbandLimitRequest) -> bytes:
        """Emit the LCDB query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.deadband is None:
            # Query — ``save`` is set-only semantics, silently ignored
            # here so a caller that reuses a request dataclass with
            # default flags for both query and set doesn't have to
            # reset them.
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        if request.deadband < 0:
            raise AlicatValidationError(
                f"{self.name}: deadband must be >= 0 (0 disables), got {request.deadband}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"deadband": request.deadband},
                ),
            )
        save_flag = "1" if request.save else "0"
        return (f"{ctx.unit_id}{prefix}{self.token} {save_flag} {request.deadband}\r").encode(
            "ascii"
        )

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> DeadbandSetting:
        """Parse the 4-field reply into :class:`DeadbandSetting`."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=4)
        unit_id, deadband_s, unit_code_s, unit_label = fields
        deadband = parse_float(deadband_s, field=f"{self.name}.deadband")
        unit_code = parse_int(unit_code_s, field=f"{self.name}.unit_code")
        return DeadbandSetting(
            unit_id=unit_id,
            deadband=deadband,
            unit_code=unit_code,
            unit=_resolve_unit_label(unit_label, unit_code),
            unit_label=unit_label,
        )


DEADBAND_LIMIT: DeadbandLimit = DeadbandLimit()
