r"""Data-reading commands — primer §Data Readings.

Four V10 10v05+ commands ship here, all prefixed ``DC`` on the wire
because they tune how the device's data-conversion pipeline reports
readings:

- :data:`ZERO_BAND` (``DCZ``, all devices) — minimum-reporting
  threshold as a percent of full scale. Readings below the band are
  reported as zero.
- :data:`AVERAGE_TIMING` (``DCA``, all devices) — per-statistic
  rolling-average window for display / data-frame updates.
- :data:`STP_NTP_PRESSURE` (``DCFRP``, mass-flow devices) — standard
  or normal pressure reference for density calculation.
- :data:`STP_NTP_TEMPERATURE` (``DCFRT``, mass-flow devices) —
  standard or normal temperature reference.

Design reference: ``docs/design.md`` §9 (Tier-2 all-device scope).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.medium import Medium
from alicatlib.devices.models import (
    AverageTimingSetting,
    StpNtpMode,
    StpNtpPressureSetting,
    StpNtpTemperatureSetting,
    ZeroBandSetting,
)
from alicatlib.errors import (
    AlicatParseError,
    AlicatValidationError,
    ErrorContext,
    UnknownUnitError,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_fields, parse_float, parse_int
from alicatlib.registry import Unit, unit_registry
from alicatlib.registry._codes_gen import UNIT_BY_CATEGORY_CODE

__all__ = [
    "AVERAGE_TIMING",
    "DCA_ALLOWED_STATISTIC_CODES",
    "DCZ_MAX_ZERO_BAND",
    "STP_NTP_PRESSURE",
    "STP_NTP_TEMPERATURE",
    "ZERO_BAND",
    "AverageTiming",
    "AverageTimingRequest",
    "StpNtpPressure",
    "StpNtpPressureRequest",
    "StpNtpTemperature",
    "StpNtpTemperatureRequest",
    "ZeroBand",
    "ZeroBandRequest",
]


# Primer p. 18 / p. 14 all tag these four commands as ``10v05+`` and
# V10-only. GP / V1_V7 / V8_V9 have no DC* variant.
_MIN_FIRMWARE_V10_05: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=5,
    raw="10v05",
)

_V10_ONLY: Final[frozenset[FirmwareFamily]] = frozenset({FirmwareFamily.V10})

_ALL_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(DeviceKind)


# Mass-flow devices only — DCFRP / DCFRT retune the pressure /
# temperature reference for density calculation, which only applies to
# thermal mass-flow devices. Pressure controllers and CODA Coriolis
# devices don't use the STP/NTP reference point (they measure mass
# directly or by delta-P), so gate kind to flow.
_FLOW_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(
    {DeviceKind.FLOW_METER, DeviceKind.FLOW_CONTROLLER},
)


#: Primer p. 18 statistic codes accepted by ``DCA``.
#:
#: Not every :class:`Statistic` code can be averaged — the device
#: exposes only the pressure / flow primary + secondary variants.
#: Validating against this set pre-I/O turns a silent device-side
#: reject into a typed error with a clear remediation.
DCA_ALLOWED_STATISTIC_CODES: Final[frozenset[int]] = frozenset(
    {1, 2, 4, 5, 6, 7, 17, 344, 352, 360},
)


#: Primer p. 14 upper bound for ``DCZ`` zero band (percent of full scale).
DCZ_MAX_ZERO_BAND: Final[float] = 6.38


#: Primer p. 18 upper bound for ``DCA`` averaging window (ms).
_DCA_MAX_AVERAGE_MS: Final[int] = 9999

#: Primer shape for ``DCA`` reply: ``<uid> <stat> <averaging_ms>``.
_DCA_FIELDS_WITH_STAT: Final[int] = 3

#: Real-hardware shape on 10v20: ``<uid> <averaging_ms>`` (no stat echo).
_DCA_FIELDS_WITHOUT_STAT: Final[int] = 2


def _resolve_unit_label(label: str, code: int) -> Unit | None:
    """Best-effort label → :class:`Unit`; shared by DCFRP / DCFRT decoders."""
    try:
        return unit_registry.coerce(label)
    except UnknownUnitError:
        pass
    matches = {u for (_cat, c), u in UNIT_BY_CATEGORY_CODE.items() if c == code}
    if len(matches) == 1:
        return next(iter(matches))
    return None


# ---------------------------------------------------------------------------
# ZERO_BAND (``DCZ``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZeroBandRequest:
    """Arguments for :data:`ZERO_BAND`.

    Attributes:
        zero_band: Zero-band threshold as a percent of full scale.
            ``0.0`` disables the zero band; valid range is
            ``0.0..6.38``. ``None`` issues the query form.
    """

    zero_band: float | None = None


@dataclass(frozen=True, slots=True)
class ZeroBand(Command[ZeroBandRequest, ZeroBandSetting]):
    r"""``DCZ`` — zero-band query/set (V10 10v05+).

    Wire shape (primer p. 14):

    - Query: ``<uid><prefix>DCZ\r``
    - Set:   ``<uid><prefix>DCZ 0 <zero_band>\r`` (the literal ``0`` is
      the primer's placeholder for a statistic slot that ``DCZ`` does
      not actually use).

    Response: ``<uid> 0 <zero_band>`` (3 fields).
    """

    name: str = "zero_band"
    token: str = "DCZ"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_V10_05
    firmware_families: frozenset[FirmwareFamily] = _V10_ONLY

    def encode(self, ctx: DecodeContext, request: ZeroBandRequest) -> bytes:
        """Emit DCZ query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.zero_band is None:
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        if request.zero_band < 0 or request.zero_band > DCZ_MAX_ZERO_BAND:
            raise AlicatValidationError(
                f"{self.name}: zero_band must be in [0, {DCZ_MAX_ZERO_BAND}]%, "
                f"got {request.zero_band}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"zero_band": request.zero_band},
                ),
            )
        return f"{ctx.unit_id}{prefix}{self.token} 0 {request.zero_band}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ZeroBandSetting:
        """Parse ``<uid> 0 <zero_band>`` into :class:`ZeroBandSetting`."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=3)
        unit_id, _stat_slot, zero_band_s = fields
        return ZeroBandSetting(
            unit_id=unit_id,
            zero_band=parse_float(zero_band_s, field=f"{self.name}.zero_band"),
        )


ZERO_BAND: ZeroBand = ZeroBand()


# ---------------------------------------------------------------------------
# AVERAGE_TIMING (``DCA``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AverageTimingRequest:
    """Arguments for :data:`AVERAGE_TIMING`.

    Attributes:
        statistic_code: One of the primer's permitted averaging codes
            (see :data:`DCA_ALLOWED_STATISTIC_CODES`). Required for
            both query and set (``DCA`` is per-statistic).
        averaging_ms: Rolling window in ms. ``None`` issues the query
            form. ``0..9999`` otherwise; ``0`` means "update every
            millisecond" (no averaging).
    """

    statistic_code: int
    averaging_ms: int | None = None


@dataclass(frozen=True, slots=True)
class AverageTiming(Command[AverageTimingRequest, AverageTimingSetting]):
    r"""``DCA`` — per-statistic averaging-window query/set (V10 10v05+).

    Wire shape (primer p. 18):

    - Query: ``<uid><prefix>DCA <stat>\r``
    - Set:   ``<uid><prefix>DCA <stat> <averaging_ms>\r``

    Response: ``<uid> <stat> <averaging_ms>`` (3 fields).
    """

    name: str = "average_timing"
    token: str = "DCA"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_V10_05
    firmware_families: frozenset[FirmwareFamily] = _V10_ONLY

    def encode(self, ctx: DecodeContext, request: AverageTimingRequest) -> bytes:
        """Emit the DCA query or set bytes."""
        if request.statistic_code not in DCA_ALLOWED_STATISTIC_CODES:
            raise AlicatValidationError(
                f"{self.name}: statistic_code {request.statistic_code} not in "
                f"{sorted(DCA_ALLOWED_STATISTIC_CODES)} — DCA only averages "
                "pressure / flow statistics per primer p. 18.",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"statistic_code": request.statistic_code},
                ),
            )
        prefix = ctx.command_prefix.decode("ascii")
        head = f"{ctx.unit_id}{prefix}{self.token} {request.statistic_code}"
        if request.averaging_ms is None:
            return (head + "\r").encode("ascii")
        if request.averaging_ms < 0 or request.averaging_ms > _DCA_MAX_AVERAGE_MS:
            raise AlicatValidationError(
                f"{self.name}: averaging_ms must be in [0, {_DCA_MAX_AVERAGE_MS}], "
                f"got {request.averaging_ms}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"averaging_ms": request.averaging_ms},
                ),
            )
        return f"{head} {request.averaging_ms}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> AverageTimingSetting:
        """Parse the ``DCA`` reply into :class:`AverageTimingSetting`.

        Primer p. 18 documents ``<uid> <stat> <averaging_ms>`` (3
        fields). Hardware validation (2026-04-17) on 10v20 firmware shows
        the device omits the ``<stat>`` echo and replies with just
        ``<uid> <averaging_ms>`` (2 fields). Accept both shapes; when
        the 2-field form arrives, ``statistic_code`` is ``0`` and the
        facade re-populates it from the request.
        """
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name)
        if len(fields) == _DCA_FIELDS_WITH_STAT:
            unit_id, stat_s, avg_s = fields
            statistic_code = parse_int(stat_s, field=f"{self.name}.statistic_code")
        elif len(fields) == _DCA_FIELDS_WITHOUT_STAT:
            unit_id, avg_s = fields
            statistic_code = 0
        else:
            raise AlicatParseError(
                f"{self.name}: expected 2 or 3 fields, got {len(fields)} — {text!r}",
                field_name="average_timing",
                expected="2 or 3 fields",
                actual=len(fields),
                context=ErrorContext(command_name=self.name, raw_response=response),
            )
        return AverageTimingSetting(
            unit_id=unit_id,
            statistic_code=statistic_code,
            averaging_ms=parse_int(avg_s, field=f"{self.name}.averaging_ms"),
        )


AVERAGE_TIMING: AverageTiming = AverageTiming()


# ---------------------------------------------------------------------------
# STP/NTP shared plumbing
# ---------------------------------------------------------------------------


def _stp_ntp_encode(
    token: str,
    ctx: DecodeContext,
    mode: StpNtpMode,
    value: float | None,
    unit_code: int | None,
) -> bytes:
    """Shared encode for ``DCFRP`` / ``DCFRT``.

    Query shape is ``<uid><prefix><token> <S|N>``; set shape adds
    ``<unit_code> <value>``. ``unit_code=0`` is the primer's "keep
    current units" sentinel.
    """
    prefix = ctx.command_prefix.decode("ascii")
    head = f"{ctx.unit_id}{prefix}{token} {mode.value}"
    if value is None:
        return (head + "\r").encode("ascii")
    if unit_code is None:
        unit_code = 0
    return f"{head} {unit_code} {value}\r".encode("ascii")


def _stp_ntp_decode(
    command_name: str,
    response: bytes | tuple[bytes, ...],
) -> tuple[str, float, int, str]:
    """Shared decode: ``<uid> <value> <unit_code> <unit_label>`` — 4 fields."""
    if isinstance(response, tuple):
        raise TypeError(
            f"{command_name}.decode expected single-line response, got {len(response)} lines",
        )
    text = response.decode("ascii")
    fields = parse_fields(text, command=command_name, expected_count=4)
    unit_id, value_s, code_s, label = fields
    value = parse_float(value_s, field=f"{command_name}.value")
    code = parse_int(code_s, field=f"{command_name}.unit_code")
    return unit_id, value, code, label


# ---------------------------------------------------------------------------
# STP_NTP_PRESSURE (``DCFRP``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StpNtpPressureRequest:
    """Arguments for :data:`STP_NTP_PRESSURE`.

    Attributes:
        mode: :class:`StpNtpMode` — which reference (standard or
            normal) to query or set.
        pressure: Reference pressure. ``None`` issues the query form.
        unit_code: Engineering-unit code for the pressure value.
            ``None`` or ``0`` means "keep current units" on set; the
            query form ignores this.
    """

    mode: StpNtpMode
    pressure: float | None = None
    unit_code: int | None = None


@dataclass(frozen=True, slots=True)
class StpNtpPressure(Command[StpNtpPressureRequest, StpNtpPressureSetting]):
    r"""``DCFRP`` — STP/NTP pressure reference query/set (V10 10v05+, mass-flow).

    Wire shape (primer p. 18):

    - Query: ``<uid><prefix>DCFRP <S|N>\r``
    - Set:   ``<uid><prefix>DCFRP <S|N> <unit_code> <pressure>\r``

    Response: ``<uid> <pressure> <unit_code> <unit_label>`` (4 fields).
    """

    name: str = "stp_ntp_pressure"
    token: str = "DCFRP"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _FLOW_DEVICE_KINDS
    media: Medium = Medium.GAS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_V10_05
    firmware_families: frozenset[FirmwareFamily] = _V10_ONLY

    def encode(self, ctx: DecodeContext, request: StpNtpPressureRequest) -> bytes:
        """Emit DCFRP query or set bytes."""
        return _stp_ntp_encode(self.token, ctx, request.mode, request.pressure, request.unit_code)

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> StpNtpPressureSetting:
        """Parse the 4-field reply into :class:`StpNtpPressureSetting`.

        ``mode`` is carried on the request-echo convention — the device
        doesn't re-echo ``S`` / ``N`` in the reply, so the facade
        fills the returned dataclass's ``mode`` via
        :func:`dataclasses.replace` (same pattern as DCU / FPF).
        """
        del ctx
        unit_id, value, code, label = _stp_ntp_decode(self.name, response)
        return StpNtpPressureSetting(
            unit_id=unit_id,
            mode=StpNtpMode.STP,  # facade replaces with request.mode
            pressure=value,
            unit_code=code,
            unit=_resolve_unit_label(label, code),
            unit_label=label,
        )


STP_NTP_PRESSURE: StpNtpPressure = StpNtpPressure()


# ---------------------------------------------------------------------------
# STP_NTP_TEMPERATURE (``DCFRT``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StpNtpTemperatureRequest:
    """Arguments for :data:`STP_NTP_TEMPERATURE`.

    Same shape as :class:`StpNtpPressureRequest` but for temperature.
    """

    mode: StpNtpMode
    temperature: float | None = None
    unit_code: int | None = None


@dataclass(frozen=True, slots=True)
class StpNtpTemperature(Command[StpNtpTemperatureRequest, StpNtpTemperatureSetting]):
    r"""``DCFRT`` — STP/NTP temperature reference query/set (V10 10v05+, mass-flow).

    Wire + response shape: see :class:`StpNtpPressure`; substitute
    ``DCFRT`` for ``DCFRP`` and temperature for pressure.
    """

    name: str = "stp_ntp_temperature"
    token: str = "DCFRT"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _FLOW_DEVICE_KINDS
    media: Medium = Medium.GAS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_V10_05
    firmware_families: frozenset[FirmwareFamily] = _V10_ONLY

    def encode(self, ctx: DecodeContext, request: StpNtpTemperatureRequest) -> bytes:
        """Emit DCFRT query or set bytes."""
        return _stp_ntp_encode(
            self.token, ctx, request.mode, request.temperature, request.unit_code
        )

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> StpNtpTemperatureSetting:
        """Parse the 4-field reply. Facade replaces ``mode`` from the request."""
        del ctx
        unit_id, value, code, label = _stp_ntp_decode(self.name, response)
        return StpNtpTemperatureSetting(
            unit_id=unit_id,
            mode=StpNtpMode.STP,  # facade replaces with request.mode
            temperature=value,
            unit_code=code,
            unit=_resolve_unit_label(label, code),
            unit_label=label,
        )


STP_NTP_TEMPERATURE: StpNtpTemperature = StpNtpTemperature()
