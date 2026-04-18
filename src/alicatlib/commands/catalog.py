"""Command catalog — the programmatic entry point for advanced users.

Every command spec from ``commands/*`` registers here. The session / device
facades dispatch through these singletons. Users who want to wrap a
command with middleware (logging, fixture recording, retry) do so by
wrapping :meth:`Session.execute` — not by mutating this catalog (design §5.4
on middleware seams).
"""

from __future__ import annotations

from alicatlib.commands.control import (
    DEADBAND_LIMIT,
    RAMP_RATE,
    DeadbandLimit,
    RampRate,
)
from alicatlib.commands.data_readings import (
    AVERAGE_TIMING,
    STP_NTP_PRESSURE,
    STP_NTP_TEMPERATURE,
    ZERO_BAND,
    AverageTiming,
    StpNtpPressure,
    StpNtpTemperature,
    ZeroBand,
)
from alicatlib.commands.display import (
    BLINK_DISPLAY,
    LOCK_DISPLAY,
    UNLOCK_DISPLAY,
    BlinkDisplay,
    LockDisplay,
    UnlockDisplay,
)
from alicatlib.commands.gas import (
    GAS_LIST,
    GAS_SELECT,
    GAS_SELECT_LEGACY,
    GasList,
    GasSelect,
    GasSelectLegacy,
)
from alicatlib.commands.loop_control import (
    LOOP_CONTROL_VARIABLE,
    LoopControlVariableCommand,
)
from alicatlib.commands.output import ANALOG_OUTPUT_SOURCE, AnalogOutputSource
from alicatlib.commands.polling import POLL_DATA, REQUEST_DATA, PollData, RequestData
from alicatlib.commands.setpoint import (
    SETPOINT,
    SETPOINT_LEGACY,
    SETPOINT_SOURCE,
    Setpoint,
    SetpointLegacy,
    SetpointSource,
)
from alicatlib.commands.streaming import STREAMING_RATE, StreamingRate
from alicatlib.commands.system import (
    DATA_FRAME_FORMAT_QUERY,
    MANUFACTURING_INFO,
    VE_QUERY,
    DataFrameFormatQuery,
    ManufacturingInfoCommand,
    VeCommand,
)
from alicatlib.commands.tare import (
    AUTO_TARE,
    POWER_UP_TARE,
    TARE_ABSOLUTE_PRESSURE,
    TARE_FLOW,
    TARE_GAUGE_PRESSURE,
    AutoTare,
    PowerUpTare,
    TareAbsolutePressure,
    TareFlow,
    TareGaugePressure,
)
from alicatlib.commands.totalizer import (
    TOTALIZER_CONFIG,
    TOTALIZER_RESET,
    TOTALIZER_RESET_PEAK,
    TOTALIZER_SAVE,
    TotalizerConfigCommand,
    TotalizerReset,
    TotalizerResetPeak,
    TotalizerSave,
)
from alicatlib.commands.units import (
    ENGINEERING_UNITS,
    FULL_SCALE_QUERY,
    EngineeringUnits,
    FullScaleQuery,
)
from alicatlib.commands.user_data import USER_DATA, UserData
from alicatlib.commands.valve import (
    CANCEL_VALVE_HOLD,
    HOLD_VALVES,
    HOLD_VALVES_CLOSED,
    VALVE_DRIVE,
    CancelValveHold,
    HoldValves,
    HoldValvesClosed,
    ValveDrive,
)

__all__ = [
    "ANALOG_OUTPUT_SOURCE",
    "AUTO_TARE",
    "AVERAGE_TIMING",
    "BLINK_DISPLAY",
    "CANCEL_VALVE_HOLD",
    "DATA_FRAME_FORMAT_QUERY",
    "DEADBAND_LIMIT",
    "ENGINEERING_UNITS",
    "FULL_SCALE_QUERY",
    "GAS_LIST",
    "GAS_SELECT",
    "GAS_SELECT_LEGACY",
    "HOLD_VALVES",
    "HOLD_VALVES_CLOSED",
    "LOCK_DISPLAY",
    "LOOP_CONTROL_VARIABLE",
    "MANUFACTURING_INFO",
    "POLL_DATA",
    "POWER_UP_TARE",
    "RAMP_RATE",
    "REQUEST_DATA",
    "SETPOINT",
    "SETPOINT_LEGACY",
    "SETPOINT_SOURCE",
    "STP_NTP_PRESSURE",
    "STP_NTP_TEMPERATURE",
    "STREAMING_RATE",
    "TARE_ABSOLUTE_PRESSURE",
    "TARE_FLOW",
    "TARE_GAUGE_PRESSURE",
    "TOTALIZER_CONFIG",
    "TOTALIZER_RESET",
    "TOTALIZER_RESET_PEAK",
    "TOTALIZER_SAVE",
    "UNLOCK_DISPLAY",
    "USER_DATA",
    "VALVE_DRIVE",
    "VE_QUERY",
    "ZERO_BAND",
    "Commands",
]


class Commands:
    """Namespace for command spec singletons.

    Usage::

        from alicatlib.commands import Commands

        await session.execute(Commands.GAS_SELECT, GasSelectRequest(gas="N2"))
    """

    GAS_SELECT: GasSelect = GAS_SELECT
    GAS_SELECT_LEGACY: GasSelectLegacy = GAS_SELECT_LEGACY
    GAS_LIST: GasList = GAS_LIST
    ENGINEERING_UNITS: EngineeringUnits = ENGINEERING_UNITS
    FULL_SCALE_QUERY: FullScaleQuery = FULL_SCALE_QUERY
    TARE_FLOW: TareFlow = TARE_FLOW
    TARE_GAUGE_PRESSURE: TareGaugePressure = TARE_GAUGE_PRESSURE
    TARE_ABSOLUTE_PRESSURE: TareAbsolutePressure = TARE_ABSOLUTE_PRESSURE
    SETPOINT: Setpoint = SETPOINT
    SETPOINT_LEGACY: SetpointLegacy = SETPOINT_LEGACY
    SETPOINT_SOURCE: SetpointSource = SETPOINT_SOURCE
    STREAMING_RATE: StreamingRate = STREAMING_RATE
    HOLD_VALVES: HoldValves = HOLD_VALVES
    HOLD_VALVES_CLOSED: HoldValvesClosed = HOLD_VALVES_CLOSED
    CANCEL_VALVE_HOLD: CancelValveHold = CANCEL_VALVE_HOLD
    VALVE_DRIVE: ValveDrive = VALVE_DRIVE
    RAMP_RATE: RampRate = RAMP_RATE
    DEADBAND_LIMIT: DeadbandLimit = DEADBAND_LIMIT
    ZERO_BAND: ZeroBand = ZERO_BAND
    AVERAGE_TIMING: AverageTiming = AVERAGE_TIMING
    STP_NTP_PRESSURE: StpNtpPressure = STP_NTP_PRESSURE
    STP_NTP_TEMPERATURE: StpNtpTemperature = STP_NTP_TEMPERATURE
    ANALOG_OUTPUT_SOURCE: AnalogOutputSource = ANALOG_OUTPUT_SOURCE
    BLINK_DISPLAY: BlinkDisplay = BLINK_DISPLAY
    LOCK_DISPLAY: LockDisplay = LOCK_DISPLAY
    UNLOCK_DISPLAY: UnlockDisplay = UNLOCK_DISPLAY
    USER_DATA: UserData = USER_DATA
    AUTO_TARE: AutoTare = AUTO_TARE
    POWER_UP_TARE: PowerUpTare = POWER_UP_TARE
    TOTALIZER_CONFIG: TotalizerConfigCommand = TOTALIZER_CONFIG
    TOTALIZER_RESET: TotalizerReset = TOTALIZER_RESET
    TOTALIZER_RESET_PEAK: TotalizerResetPeak = TOTALIZER_RESET_PEAK
    TOTALIZER_SAVE: TotalizerSave = TOTALIZER_SAVE
    LOOP_CONTROL_VARIABLE: LoopControlVariableCommand = LOOP_CONTROL_VARIABLE
    POLL_DATA: PollData = POLL_DATA
    REQUEST_DATA: RequestData = REQUEST_DATA
    VE_QUERY: VeCommand = VE_QUERY
    MANUFACTURING_INFO: ManufacturingInfoCommand = MANUFACTURING_INFO
    DATA_FRAME_FORMAT_QUERY: DataFrameFormatQuery = DATA_FRAME_FORMAT_QUERY
