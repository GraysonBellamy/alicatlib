"""Typed device-identity and measurement models.

These are the frozen dataclasses returned by the session layer for
identification results (:class:`DeviceInfo`), capability probe outcomes
(:data:`ProbeOutcome`), individual statistic readings
(:class:`MeasurementSet`), and cached full-scale ranges
(:class:`FullScaleValue`). Together with :mod:`alicatlib.devices.data_frame`
they are the full set of public models referenced by the rest of the
package, per design doc §5.5.

Data-frame models (:class:`~alicatlib.devices.data_frame.DataFrame`,
:class:`~alicatlib.devices.data_frame.DataFrameFormat`, ...) live in
:mod:`alicatlib.devices.data_frame` to keep the wire-parsing machinery
separate from the identity models that cache it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date, datetime

    from alicatlib.commands.base import Capability
    from alicatlib.devices.data_frame import DataFrame
    from alicatlib.devices.kind import DeviceKind
    from alicatlib.devices.medium import Medium
    from alicatlib.firmware import FirmwareVersion
    from alicatlib.registry._codes_gen import Statistic, Unit
    from alicatlib.registry.loop_control import LoopControlVariable

__all__ = [
    "TOTALIZER_DISABLED_CODE",
    "AnalogOutputChannel",
    "AnalogOutputSourceSetting",
    "AutoTareState",
    "AverageTimingSetting",
    "BlinkDisplayState",
    "DeadbandSetting",
    "DeviceInfo",
    "DisplayLockResult",
    "FullScaleValue",
    "LoopControlState",
    "ManufacturingInfo",
    "MeasurementSet",
    "PowerUpTareState",
    "ProbeOutcome",
    "RampRateSetting",
    "SetpointState",
    "StatusCode",
    "StpNtpMode",
    "StpNtpPressureSetting",
    "StpNtpTemperatureSetting",
    "TareResult",
    "TimeUnit",
    "TotalizerConfig",
    "TotalizerId",
    "TotalizerLimitMode",
    "TotalizerMode",
    "TotalizerResetResult",
    "TotalizerSaveState",
    "UnitSetting",
    "UserDataSetting",
    "ValveDriveState",
    "ValveHoldResult",
    "ZeroBandSetting",
]


type ProbeOutcome = Literal["present", "absent", "timeout", "rejected", "parse_error"]
"""Per-:class:`Capability` probe result.

Retained on :attr:`DeviceInfo.probe_report` for diagnostics and user-facing
override guidance. The gating check in the session is binary — the flag
is either set in :attr:`DeviceInfo.capabilities` or not — but the per-flag
outcome is useful when a user needs to understand *why* a capability was
marked absent. A timeout looks the same as "hardware missing" at the gating
layer, but they imply very different remediations.
"""


class StatusCode(StrEnum):
    """Device status codes that may appear in the data-frame tail.

    The Alicat primer defines these as 3-letter tokens trailing the numeric
    fields when the condition is active. Multiple codes may be present
    simultaneously (e.g. ``MOV`` + ``TMF``); :class:`alicatlib.devices.data_frame.DataFrame`
    carries them as a :class:`frozenset` so ordering on the wire doesn't
    matter downstream.
    """

    ADC = "ADC"
    """Internal analog-to-digital communication error."""

    EXH = "EXH"
    """Manual exhaust valve override active."""

    HLD = "HLD"
    """Valve-drive hold enabled."""

    LCK = "LCK"
    """Display front-panel buttons disabled."""

    MOV = "MOV"
    """Mass-flow rate over full-scale."""

    OPL = "OPL"
    """Overpressure limit actively throttling."""

    OVR = "OVR"
    """Totalizer rolled over / frozen at max."""

    POV = "POV"
    """Pressure reading over full-scale."""

    TMF = "TMF"
    """Totalizer missed data (typically following ``MOV`` or ``VOV``)."""

    TOV = "TOV"
    """Temperature reading over full-scale."""

    VOV = "VOV"
    """Volumetric-flow reading over full-scale."""


@dataclass(frozen=True, slots=True)
class FullScaleValue:
    """Cached full-scale range for one statistic.

    Populated by the session's capability-probe step (``FPF`` queries) and
    then used by :meth:`Device.setpoint` and similar facades for pre-I/O
    range validation (design §5.20.2). ``unit`` is ``None`` when the
    device's unit doesn't map to a known :class:`Unit` — the raw
    ``unit_label`` is always preserved for diagnostics.

    ``statistic`` is filled by the facade after dispatch — the device's
    ``FPF`` reply doesn't echo the requested statistic (verified against
    a V10 capture on 2026-04-17), so the decoder leaves it as
    :attr:`Statistic.NONE` and the facade calls
    :func:`dataclasses.replace` to populate it from the request.
    """

    statistic: Statistic
    value: float
    unit: Unit | None
    unit_label: str


def _empty_probe_report() -> dict[Capability, ProbeOutcome]:
    return {}


def _empty_full_scale() -> dict[Statistic, FullScaleValue]:
    return {}


@dataclass(frozen=True, slots=True)
class MeasurementSet:
    """Result of a :class:`~alicatlib.commands.polling.RequestData` (``DV``) query.

    Unlike a :class:`~alicatlib.devices.data_frame.DataFrame`, which returns
    the cached full set of fields, a ``DV`` query targets a specific list
    of statistics (1–13 per call) and reports each with an averaging
    window. Values that come back as the ``--`` sentinel are ``None``.
    """

    unit_id: str
    values: Mapping[Statistic, float | str | None]
    averaging_ms: int
    received_at: datetime


@dataclass(frozen=True, slots=True)
class ManufacturingInfo:
    """Parsed ``??M*`` manufacturing-info table.

    Minimal, honest surface: the raw per-M-code payload keyed by the
    ``M<NN>`` index. The parser pins only what the wire format guarantees
    (``<unit_id> M<NN> <payload>``); the semantic mapping from M-code
    number to named field (``M04`` → model, ``M05`` → serial, etc.) is a
    separate concern handled by the factory, which can adjust per firmware
    version without rewriting the parser.

    Only emitted by :func:`alicatlib.protocol.parser.parse_manufacturing_info`
    when the firmware family and version support ``??M*`` (numeric family,
    ≥ 8v28 per design §5.9). GP and pre-8v28 devices synthesise
    :class:`DeviceInfo` directly from the ``VE`` reply plus a caller-supplied
    ``model_hint``.
    """

    unit_id: str
    by_code: Mapping[int, str]

    def get(self, code: int) -> str | None:
        """Return the payload for ``M<code>``, or ``None`` if not reported."""
        return self.by_code.get(code)


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Everything known about a device after identification.

    Built by :func:`alicatlib.devices.factory.identify_device`. The
    ``probe_report`` preserves per-capability outcomes even when the
    capability is absent from :attr:`capabilities`, so users can tell a
    "device lacks the hardware" situation from a "probe timed out"
    situation (see design §5.9 and :data:`ProbeOutcome`).

    For GP-family or pre-8v28 devices the ``??M*`` manufacturing-info
    table is unavailable; the factory synthesises a :class:`DeviceInfo`
    from the ``VE`` reply plus a caller-supplied ``model_hint``, in which
    case the string-shaped fields may all be ``None`` except ``model``.
    """

    unit_id: str
    manufacturer: str | None
    model: str
    serial: str | None
    manufactured: str | None
    calibrated: str | None
    calibrated_by: str | None
    software: str
    firmware: FirmwareVersion
    firmware_date: date | None
    kind: DeviceKind
    media: Medium
    capabilities: Capability
    probe_report: Mapping[Capability, ProbeOutcome] = field(default_factory=_empty_probe_report)
    full_scale: Mapping[Statistic, FullScaleValue] = field(default_factory=_empty_full_scale)


@dataclass(frozen=True, slots=True)
class SetpointState:
    """Result of a :class:`~alicatlib.commands.setpoint.Setpoint` query or set.

    ``current`` and ``requested`` are reported separately by the device
    (modern ``LS`` reply: ``<uid> <current> <requested> <unit_code>
    <unit_label>``). They diverge briefly on a set while the controller's
    loop closes on the new target; they track to the same value in steady
    state. ``unit`` / ``unit_label`` come straight from the same reply.

    ``frame`` is optional: legacy ``S`` (set-only, pre-9v00) responds
    with a post-op data frame rather than the 5-field LS reply, so the
    facade can attach the parsed frame on the legacy path. On the modern
    LS path ``frame`` is always ``None``.
    """

    unit_id: str
    current: float
    requested: float
    unit: Unit | None
    unit_label: str | None
    frame: DataFrame | None = None


@dataclass(frozen=True, slots=True)
class TareResult:
    """Result of a tare command (flow / gauge pressure / absolute pressure).

    The device responds with a post-tare data frame; that frame is the
    most useful artifact (it reports the new zero-referenced reading),
    so the result surface is intentionally minimal: just the frame.
    """

    frame: DataFrame


@dataclass(frozen=True, slots=True)
class UnitSetting:
    """Result of an engineering-units (``DCU``) query or set.

    ``unit`` is ``None`` when the device reports a code that does not
    map to a known :class:`Unit` — the raw ``label`` is always
    preserved so diagnostics can see the device's exact string.
    ``statistic`` scopes the setting: ``DCU`` applies per-statistic
    (or per-group when ``apply_to_group=True`` is requested at the
    facade).
    """

    unit_id: str
    statistic: Statistic
    unit: Unit | None
    label: str


@dataclass(frozen=True, slots=True)
class LoopControlState:
    """Result of an ``LV`` (loop-control variable) query or set.

    ``variable`` is the typed :class:`LoopControlVariable` the
    controller's loop is tracking. ``label`` preserves the device's
    raw descriptor string for diagnostics.
    """

    unit_id: str
    variable: LoopControlVariable
    label: str


@dataclass(frozen=True, slots=True)
class AutoTareState:
    """Result of an auto-tare (``ZCA``) query or set.

    ``delay_s`` is the configured settling delay in seconds; primer
    constrains this to ``[0.1, 25.5]`` and the command encoder
    validates range pre-I/O (see design §10).
    """

    unit_id: str
    enabled: bool
    delay_s: float


@dataclass(frozen=True, slots=True)
class ValveHoldResult:
    """Result of a valve-hold command (``HP`` / ``HC`` / ``C``).

    All three commands respond with a post-op data frame; the
    discriminator is whether :attr:`DataFrame.status` carries
    :attr:`StatusCode.HLD`. :attr:`held` captures that for convenience
    — ``True`` after ``HP`` or ``HC``, ``False`` after ``C``.

    Per design §9 Tier-2 controller scope.
    """

    frame: DataFrame

    @property
    def held(self) -> bool:
        """``True`` if the post-op frame reports :attr:`StatusCode.HLD`."""
        return StatusCode.HLD in self.frame.status


@dataclass(frozen=True, slots=True)
class ValveDriveState:
    """Result of a ``VD`` (valve-drive query) command.

    ``valves`` carries 1–3 drive percentages in primer-declared order:
    single-valve controllers report one value; dual-valve controllers
    report ``(upstream, downstream)``; tri-valve (exhaust) controllers
    add a third entry. The wire-side shape is not a reliable signal
    of device *capability* — design §9 warns against inferring valve
    count from the reply. Multi-valve-specific logic should gate on
    :attr:`Capability.MULTI_VALVE` / :attr:`Capability.THIRD_VALVE`,
    not on ``len(valves)``.
    """

    unit_id: str
    valves: tuple[float, ...]


class TimeUnit(IntEnum):
    """Time-unit code used by the ``SR`` (max ramp rate) command.

    Primer p. 15 encodes the unit-over-time base for ramping as a
    single integer: ``3..7`` for ms / s / m / hour / day. The enum
    mirrors that encoding so callers write ``TimeUnit.SECOND`` instead
    of a magic literal.
    """

    MILLISECOND = 3
    SECOND = 4
    MINUTE = 5
    HOUR = 6
    DAY = 7


@dataclass(frozen=True, slots=True)
class RampRateSetting:
    """Result of an ``SR`` (max ramp rate) query or set.

    Wire shape: ``<uid> <max_ramp> <setpoint_unit_code> <time_value> <rate_unit_label>``.
    ``max_ramp == 0.0`` means ramping is disabled; the controller
    jumps to the new setpoint instantly on the next write.

    Attributes:
        unit_id: Echoed unit id.
        max_ramp: Ramp step size, in the device's current engineering
            units for the loop-control variable. ``0.0`` disables
            ramping.
        setpoint_unit_code: Raw numeric unit code from primer
            Appendix B. Preserved for diagnostics; the typed
            :attr:`setpoint_unit` is the preferred handle.
        setpoint_unit: Resolved :class:`Unit` member, or ``None`` when
            the wire label doesn't resolve against the registry.
        time_unit: :class:`TimeUnit` encoding the ramp rate's time
            base (ms / s / m / h / d).
        rate_unit_label: Device-reported units-over-time label
            (e.g. ``"SCCM/s"``). Preserved verbatim.
    """

    unit_id: str
    max_ramp: float
    setpoint_unit_code: int
    setpoint_unit: Unit | None
    time_unit: TimeUnit
    rate_unit_label: str


@dataclass(frozen=True, slots=True)
class DeadbandSetting:
    """Result of an ``LCDB`` (deadband limit) query or set.

    Wire shape: ``<uid> <deadband> <unit_code> <unit_label>``.
    Controllers apply the deadband around the setpoint in the
    controlled variable's engineering units — a value of ``0.5`` with
    ``unit_label="PSIA"`` means "allow ±0.5 PSIA drift before
    re-correcting." A value of ``0`` disables the deadband.
    """

    unit_id: str
    deadband: float
    unit_code: int
    unit: Unit | None
    unit_label: str


# ---------------------------------------------------------------------------
# Non-destructive all-device specialty
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZeroBandSetting:
    """Result of a ``DCZ`` (zero band) query or set.

    Zero band is the minimum-reporting threshold expressed as a
    percentage of full scale: values below it are reported as zero.
    Primer constrains the range to ``0..6.38`` (percent); ``0``
    disables the zero band. Device responds with
    ``<uid> 0 <zero_band>`` — the literal ``0`` is the primer's
    placeholder for a statistic argument that ``DCZ`` does not use.
    """

    unit_id: str
    zero_band: float


@dataclass(frozen=True, slots=True)
class AverageTimingSetting:
    """Result of a ``DCA`` (flow/pressure average) query or set.

    Averaging window in milliseconds for a specific statistic code
    (primer p. 18 table: 1 = all pressures, 2 = absolute pressure,
    4 = volumetric flow, 5 = mass flow, 6 = gauge pressure,
    7 = differential pressure, 17 = external volumetric flow,
    344/352/360 = secondary-sensor variants). ``averaging_ms=0``
    reports every-millisecond readings.
    """

    unit_id: str
    statistic_code: int
    averaging_ms: int


class StpNtpMode(StrEnum):
    """Reference mode for ``DCFRP`` / ``DCFRT``.

    Standard conditions (``STP`` → ``"S"``) underpin standard volumetric
    flow units (SLPM / SCFM); normal conditions (``NTP`` → ``"N"``)
    underpin normal volumetric flow units (LPM / CFM). The two are
    separate reference points that the device lets users retune — the
    enum mirrors the primer's single-letter wire encoding.
    """

    STP = "S"
    NTP = "N"


@dataclass(frozen=True, slots=True)
class StpNtpPressureSetting:
    """Result of a ``DCFRP`` (standard / normal pressure reference) query or set.

    Default on Alicat devices is ``14.696 PSIA``. Changing this
    affects the density calculation for every standard / normal
    volumetric flow reading the device reports.
    """

    unit_id: str
    mode: StpNtpMode
    pressure: float
    unit_code: int
    unit: Unit | None
    unit_label: str


@dataclass(frozen=True, slots=True)
class StpNtpTemperatureSetting:
    """Result of a ``DCFRT`` (standard / normal temperature reference) query or set.

    Default on Alicat devices is ``25 °C``. Same density-calculation
    story as :class:`StpNtpPressureSetting`.
    """

    unit_id: str
    mode: StpNtpMode
    temperature: float
    unit_code: int
    unit: Unit | None
    unit_label: str


class AnalogOutputChannel(IntEnum):
    """Analog-output channel selector for ``ASOCV``.

    Devices ship with a primary analog output (``4-20 mA`` or
    ``0-5 V`` per part-number suffix) and optionally a secondary.
    ``ASOCV 0`` targets primary; ``ASOCV 1`` targets secondary.
    """

    PRIMARY = 0
    SECONDARY = 1


@dataclass(frozen=True, slots=True)
class AnalogOutputSourceSetting:
    """Result of an ``ASOCV`` (analog-output-source) query or set.

    ``value`` is the statistic code the output tracks — or the sentinel
    ``0`` (minimum) / ``1`` (maximum), in which case the device emits
    a fixed min / max analog level instead of following a measurement.
    When ``value`` is ``0`` or ``1``, the primer notes that
    ``unit_code=1`` and ``unit_label="---"``.
    """

    unit_id: str
    channel: AnalogOutputChannel
    value: int
    unit_code: int
    unit: Unit | None
    unit_label: str


@dataclass(frozen=True, slots=True)
class BlinkDisplayState:
    """Result of an ``FFP`` (blink display) query or set.

    ``flashing`` is the echo of the primer's ``0``/``1`` binary
    response — ``True`` while the backlight is flashing, ``False``
    otherwise. Gated at the command layer by
    :attr:`Capability.DISPLAY` (probed at :func:`open_device`).
    """

    unit_id: str
    flashing: bool


@dataclass(frozen=True, slots=True)
class DisplayLockResult:
    """Result of an ``L`` / ``U`` (lock / unlock display) command.

    Both commands respond with a data frame — ``L`` sets the
    :attr:`StatusCode.LCK` bit, ``U`` clears it. :attr:`locked`
    exposes that for convenience.
    """

    frame: DataFrame

    @property
    def locked(self) -> bool:
        """``True`` if the post-op frame reports :attr:`StatusCode.LCK`."""
        return StatusCode.LCK in self.frame.status


@dataclass(frozen=True, slots=True)
class UserDataSetting:
    """Result of a ``UD`` (user data) read or write.

    Four slots (``0..3``) each hold up to 32 ASCII characters.
    Encoded binary data (hex / base64) goes through the value field
    unchanged — the library does not interpret user data.
    """

    unit_id: str
    slot: int
    value: str


@dataclass(frozen=True, slots=True)
class PowerUpTareState:
    """Result of a ``ZCP`` (power-up tare) query or set.

    ``True`` means the device performs a 0.25 s tare after sensors
    stabilise on power-up. On controllers, closed-loop control is
    delayed and valves stay closed until the tare completes.
    """

    unit_id: str
    enabled: bool


# ---------------------------------------------------------------------------
# Totalizer
# ---------------------------------------------------------------------------


class TotalizerId(IntEnum):
    """Which totalizer to address — primer supports two (``1`` / ``2``)."""

    FIRST = 1
    SECOND = 2


class TotalizerMode(IntEnum):
    """Totalizer-accumulation mode for ``TC`` (primer p. 23 table).

    The ``-1`` ``KEEP`` sentinel is a set-time "don't change" marker
    the primer admits; it is not a real config state the device ever
    echoes back.
    """

    KEEP = -1
    """Set-only: leave the current mode unchanged."""
    POSITIVE_ONLY = 0
    """Accumulate positive flow only; ignore negative flow."""
    NEGATIVE_ONLY = 1
    """Accumulate negative flow only; ignore positive flow."""
    BIDIRECTIONAL = 2
    """Accumulate positive and subtract negative flow."""
    RESET_ON_STOP = 3
    """Accumulate positive flow, reset to zero when flow stops."""


class TotalizerLimitMode(IntEnum):
    """Totalizer overflow behaviour for ``TC`` (primer p. 23 table).

    ``-1`` is the set-only "keep current" sentinel (same convention
    as :class:`TotalizerMode`).
    """

    KEEP = -1
    """Set-only: leave the current limit mode unchanged."""
    STOP_AT_MAX = 0
    """Stop counting at the maximum value. No ``TOV`` status bit."""
    ROLLOVER = 1
    """Reset to zero and keep counting. No ``TOV`` status bit."""
    STOP_AT_MAX_WITH_TOV = 2
    """Stop counting at the maximum value; set the ``TOV`` status bit."""
    ROLLOVER_WITH_TOV = 3
    """Reset to zero and keep counting; set the ``TOV`` status bit."""


#: ``flow_statistic_code=1`` is the primer's sentinel for a disabled
#: totalizer (a statistic "All pressures" that has no meaning as a flow
#: totalizer target). Kept as an explicit constant so the facade can
#: answer ``TotalizerConfig.enabled`` without magic numbers.
TOTALIZER_DISABLED_CODE: int = 1


@dataclass(frozen=True, slots=True)
class TotalizerConfig:
    """Result of a ``TC`` (configure totalizer) query or set.

    Attributes mirror the primer's wire order
    (``flow_statistic_code mode limit_mode digits decimal_place``) —
    callers inspect :attr:`enabled` rather than reading
    ``flow_statistic_code == TOTALIZER_DISABLED_CODE`` themselves.
    """

    unit_id: str
    totalizer: TotalizerId
    flow_statistic_code: int
    mode: TotalizerMode
    limit_mode: TotalizerLimitMode
    digits: int
    decimal_place: int

    @property
    def enabled(self) -> bool:
        """``True`` when the totalizer is tracking a flow statistic.

        Primer: ``flow_statistic_code == 1`` signals "disabled" — any
        other code means the totalizer is enabled on that statistic.
        """
        return self.flow_statistic_code != TOTALIZER_DISABLED_CODE


@dataclass(frozen=True, slots=True)
class TotalizerResetResult:
    """Wraps the post-op data frame from ``T <n>`` or ``TP <n>``.

    The frame is the useful artifact (it carries the fresh totalizer
    reading); the result object exists so a future addition (observed
    totalizer reading extracted from the frame, timing, …) has a
    stable home.
    """

    frame: DataFrame


@dataclass(frozen=True, slots=True)
class TotalizerSaveState:
    """Result of a ``TCR`` (save totalizer) query or set.

    ``enabled=True`` means the device periodically persists totalizer
    values to EEPROM and restores them at power-on.
    """

    unit_id: str
    enabled: bool
