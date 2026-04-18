"""Device facade base.

:class:`Device` is the public, user-facing object returned by
:func:`alicatlib.devices.factory.open_device`. It is a thin veneer over
:class:`alicatlib.devices.session.Session` — every method delegates to
the session's :meth:`~Session.execute` (or :meth:`~Session.poll` for the
timing-wrapped poll), so all pre-I/O gating lives in one place.

:class:`DeviceKind` lives in its sibling :mod:`alicatlib.devices.kind`
module. That split is what lets :class:`Device` import command specs
(``GAS_SELECT``, ``ENGINEERING_UNITS``, …) for its method bodies
without creating a cycle with :mod:`alicatlib.commands`, which needs
:class:`DeviceKind` at command-spec definition time. See design §15.1.

Subclasses in :mod:`.flow_meter` and :mod:`.flow_controller` add
family-specific methods (setpoint, valve drive, exhaust, ...) without
changing the dispatch model — they just expose additional commands from
the catalog.

Design reference: ``docs/design.md`` §5.9.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic_ns
from typing import TYPE_CHECKING

from alicatlib._logging import get_logger
from alicatlib.commands import (
    ANALOG_OUTPUT_SOURCE,
    AVERAGE_TIMING,
    BLINK_DISPLAY,
    ENGINEERING_UNITS,
    FULL_SCALE_QUERY,
    GAS_LIST,
    GAS_SELECT,
    GAS_SELECT_LEGACY,
    LOCK_DISPLAY,
    POWER_UP_TARE,
    REQUEST_DATA,
    STP_NTP_PRESSURE,
    STP_NTP_TEMPERATURE,
    TARE_ABSOLUTE_PRESSURE,
    TARE_FLOW,
    TARE_GAUGE_PRESSURE,
    TOTALIZER_CONFIG,
    TOTALIZER_RESET,
    TOTALIZER_RESET_PEAK,
    TOTALIZER_SAVE,
    UNLOCK_DISPLAY,
    USER_DATA,
    ZERO_BAND,
    AnalogOutputSourceRequest,
    AverageTimingRequest,
    BlinkDisplayRequest,
    EngineeringUnitsRequest,
    FullScaleQueryRequest,
    GasListRequest,
    GasSelectLegacyRequest,
    GasSelectRequest,
    GasState,
    LockDisplayRequest,
    PowerUpTareRequest,
    RequestDataRequest,
    StpNtpPressureRequest,
    StpNtpTemperatureRequest,
    TareAbsolutePressureRequest,
    TareFlowRequest,
    TareGaugePressureRequest,
    TotalizerConfigRequest,
    TotalizerResetPeakRequest,
    TotalizerResetRequest,
    TotalizerSaveRequest,
    UnlockDisplayRequest,
    UserDataRequest,
    ZeroBandRequest,
)
from alicatlib.commands._firmware_cutoffs import uses_modern_gas_select
from alicatlib.devices.data_frame import DataFrame
from alicatlib.devices.models import (
    AnalogOutputChannel,
    DisplayLockResult,
    MeasurementSet,
    StpNtpMode,
    TareResult,
    TotalizerId,
    TotalizerResetResult,
)
from alicatlib.errors import (
    AlicatUnsupportedCommandError,
    AlicatValidationError,
    ErrorContext,
)
from alicatlib.registry import gas_registry
from alicatlib.registry.statistics import statistic_registry

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType
    from typing import Any

    from alicatlib.commands import Command
    from alicatlib.devices.data_frame import ParsedFrame
    from alicatlib.devices.models import (
        AnalogOutputSourceSetting,
        AverageTimingSetting,
        BlinkDisplayState,
        DeviceInfo,
        FullScaleValue,
        PowerUpTareState,
        StpNtpPressureSetting,
        StpNtpTemperatureSetting,
        TotalizerConfig,
        TotalizerLimitMode,
        TotalizerMode,
        TotalizerSaveState,
        UnitSetting,
        UserDataSetting,
        ZeroBandSetting,
    )
    from alicatlib.devices.session import Session
    from alicatlib.devices.streaming import OverflowPolicy, StreamingSession
    from alicatlib.registry import Gas, Statistic, Unit

__all__ = ["Device"]


_logger = get_logger("session")

_TARE_FLOW_PRECONDITION = (
    "tare_flow: caller must ensure no gas is flowing through the device (library cannot verify)"
)
_TARE_GAUGE_PRECONDITION = (
    "tare_gauge_pressure: caller must ensure the line is depressurised "
    "to atmosphere (library cannot verify)"
)
_TARE_ABSOLUTE_PRECONDITION = (
    "tare_absolute_pressure: gauge pressure should be at atmosphere; "
    "barometer reading is used as the reference"
)


class Device:
    """User-facing façade over a :class:`Session`.

    Constructed by :func:`alicatlib.devices.factory.open_device`. Users do
    not instantiate this class directly (the factory picks the correct
    subclass based on the :class:`DeviceInfo.model` prefix via the
    ``MODEL_RULES`` dispatch table — see design §5.9).

    The device does not own the transport's lifecycle; the context manager
    returned by ``open_device`` does. Entering the device as a context
    manager is a no-op for nesting convenience.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # --------------------------------------------------------------- identity

    @property
    def info(self) -> DeviceInfo:
        """Identity snapshot from the factory's identification pipeline."""
        return self._session.info

    @property
    def unit_id(self) -> str:
        """Validated single-letter unit id this device is addressed by."""
        return self._session.unit_id

    @property
    def session(self) -> Session:
        """Underlying :class:`Session`.

        Exposed for advanced users who need :meth:`Session.execute`
        directly or want to inspect the session's gating state.
        """
        return self._session

    # --------------------------------------------------------------- polling

    async def poll(self) -> DataFrame:
        """Read one data frame.

        Lazy-probes ``??D*`` the first time it's called if the session
        didn't have a cached :class:`DataFrameFormat` yet. Returns a
        :class:`DataFrame` with read-site ``received_at`` and
        ``monotonic_ns`` captured by the session.
        """
        return await self._session.poll()

    async def request(
        self,
        statistics: Sequence[Statistic | str],
        *,
        averaging_ms: int = 1,
    ) -> MeasurementSet:
        """Request a specific list of statistics with an averaging window.

        ``DV`` on the wire. Unlike :meth:`poll`, which returns the
        device's cached data-frame fields, this targets 1–13 caller-chosen
        :class:`~alicatlib.registry.Statistic` members and reports each
        averaged over ``averaging_ms`` milliseconds.

        Per-slot ``--`` sentinels (invalid statistic code for this
        device) map to ``None`` in the returned
        :attr:`MeasurementSet.values`.

        Args:
            statistics: 1–13 :class:`Statistic` members or alias strings.
                Preserved order in the returned mapping.
            averaging_ms: Rolling averaging window in milliseconds,
                1–9999. ``0`` is rejected pre-I/O
                (:class:`AlicatValidationError`) since the device rejects
                it with a generic ``?`` and the stricter message is more
                useful.

        Returns:
            :class:`MeasurementSet` whose ``values`` mapping is keyed by
            the :class:`Statistic` members the caller asked for. If the
            caller repeats a statistic, the last occurrence wins in the
            mapping; the wire still carries every request (the devicce
            still averages over all slots).
        """
        typed_stats = tuple(statistic_registry.coerce(s) for s in statistics)
        raw_values: tuple[float | None, ...] = await self._session.execute(
            REQUEST_DATA,
            RequestDataRequest(statistics=typed_stats, averaging_ms=averaging_ms),
        )
        return MeasurementSet(
            unit_id=self._session.unit_id,
            values=dict(zip(typed_stats, raw_values, strict=True)),
            averaging_ms=averaging_ms,
            received_at=datetime.now(UTC),
        )

    # --------------------------------------------------------------- gas

    async def gas(
        self,
        gas: Gas | str | None = None,
        *,
        save: bool | None = None,
    ) -> GasState:
        """Query or set the active gas.

        ``gas=None`` issues the query form and returns the current
        selection without changing it. Passing a
        :class:`~alicatlib.registry.Gas` (or any registered alias)
        sets the active gas. ``save=True`` persists to EEPROM — beware
        of the rate-warning guard (design §5.20.7) if you call this in
        a loop.

        Dispatch is firmware-aware:

        - V10 ≥ 10v05 → :data:`GAS_SELECT` (``GS``). Supports query,
          set, and save.
        - All other supported firmware (GP, V1_V7, V8_V9, V10 < 10v05)
          → :data:`GAS_SELECT_LEGACY` (``G``). Set only; no ``save``
          flag; the device replies with a post-op data frame rather
          than the modern 4-field form. The facade fabricates a
          :class:`GasState` from the request and the frame's echoed
          unit id; ``label`` / ``long_name`` are resolved from the
          gas registry.

        The command's ``device_kinds`` gate rejects calls on device
        kinds that don't have a selectable active gas (pressure-only
        devices) pre-I/O with :class:`AlicatUnsupportedCommandError`.

        Raises:
            AlicatUnsupportedCommandError: ``gas is None`` (query form)
                on firmware that only supports the legacy set-only path.
            AlicatValidationError: ``save is True`` on legacy firmware,
                which has no persist flag.
        """
        firmware = self._session.firmware
        if uses_modern_gas_select(firmware):
            return await self._session.execute(
                GAS_SELECT,
                GasSelectRequest(gas=gas, save=save),
            )

        # Legacy path — set only, no ``save`` flag.
        if gas is None:
            raise AlicatUnsupportedCommandError(
                "gas() query form requires firmware supporting GS (V10 ≥ 10v05); "
                f"this device reports {firmware}. Legacy G is set-only.",
                context=ErrorContext(
                    command_name="gas_select_legacy",
                    unit_id=self._session.unit_id,
                    firmware=firmware,
                ),
            )
        if save is True:
            raise AlicatValidationError(
                "save=True requires firmware supporting GS (V10 ≥ 10v05); "
                f"this device reports {firmware}. Legacy G has no persist flag.",
                context=ErrorContext(
                    command_name="gas_select_legacy",
                    unit_id=self._session.unit_id,
                    firmware=firmware,
                    extra={"save_requested": True},
                ),
            )

        typed_gas = gas_registry.coerce(gas)
        frame = await self._session.execute(
            GAS_SELECT_LEGACY,
            GasSelectLegacyRequest(gas=typed_gas),
        )
        # The legacy device replies with a data frame, not ``<uid> code
        # short long``. Fabricate a ``GasState`` from the request + the
        # frame's echoed unit id so the facade's return shape matches
        # the modern path.
        return GasState(
            unit_id=frame.unit_id,
            code=typed_gas.code,
            gas=typed_gas,
            label=typed_gas.value,
            long_name=typed_gas.value,
        )

    async def gas_list(self) -> Mapping[int, str]:
        """Enumerate gases available on the device (``??G*``).

        Returns a mapping from Alicat gas code (primer Appendix C) to
        the raw label the device reports. Codes in the ``236``..``255``
        range correspond to custom-mixture slots; an empty / absent
        slot is simply not included in the mapping.

        Callers that want typed :class:`Gas` members should feed each
        code through :func:`alicatlib.registry.gas_registry.by_code`;
        unknown codes are preserved as labels so diagnostics still see
        the device's exact string.
        """
        return await self._session.execute(GAS_LIST, GasListRequest())

    # --------------------------------------------------------------- units

    async def engineering_units(
        self,
        statistic: Statistic | str,
        unit: Unit | int | str | None = None,
        *,
        apply_to_group: bool = False,
        override_special_rules: bool = False,
    ) -> UnitSetting:
        """Query or set the engineering unit for ``statistic`` (``DCU``).

        ``unit=None`` issues the query form. Passing a :class:`Unit`,
        its alias, or an explicit integer wire code sets the unit.
        ``apply_to_group=True`` broadcasts the change to every
        statistic in the target's group; ``override_special_rules=True``
        bypasses device-side restrictions on unusual statistic/unit
        pairings.

        A successful SET invalidates the session's cached
        :class:`DataFrameFormat`: units affect display in the data
        frame, so the next :meth:`poll` re-probes ``??D*`` lazily via
        :meth:`Session.invalidate_data_frame_format`. Query form is a
        no-op for the cache.

        Raises:
            AlicatValidationError: Ambiguous :class:`Unit` (member that
                maps to multiple codes across categories) — pass the
                raw integer code to disambiguate.
        """
        result = await self._session.execute(
            ENGINEERING_UNITS,
            EngineeringUnitsRequest(
                statistic=statistic,
                unit=unit,
                apply_to_group=apply_to_group,
                override_special_rules=override_special_rules,
            ),
        )
        if unit is not None:
            # Only a SET reshapes the data frame; query returns the same
            # wire format the session cached at startup.
            self._session.invalidate_data_frame_format()
        # The DCU reply doesn't echo the requested statistic (verified on
        # V10 hardware 2026-04-17 — see design §16.6); fill it from the
        # request so the caller sees the typed enum, not Statistic.NONE.
        # Statistic is a StrEnum so coerce() accepts either form.
        return replace(result, statistic=statistic_registry.coerce(statistic))

    async def full_scale(self, statistic: Statistic | str) -> FullScaleValue:
        """Query the full-scale value for ``statistic`` (``FPF``).

        Used by setpoint range validation (design §5.20.2) and as a
        capability-probe signal (``FPF`` on stat 15 → barometer
        present). The session's factory-level probe populates
        :attr:`DeviceInfo.full_scale` for common statistics at startup;
        this method exposes the same command for ad-hoc queries.
        """
        result = await self._session.execute(
            FULL_SCALE_QUERY,
            FullScaleQueryRequest(statistic=statistic),
        )
        # FPF reply doesn't echo the requested statistic — fill it from
        # the request (design §16.6).
        return replace(result, statistic=statistic_registry.coerce(statistic))

    # --------------------------------------------------------------- tare

    async def tare_flow(self) -> TareResult:
        """Zero the flow reading (``T``).

        Caller's precondition: no gas flowing through the device. The
        library cannot verify this — an INFO log records the
        expectation on every call so the precondition is auditable
        after the fact (design §5.18 pt 6). The device replies with a
        post-op data frame; the returned :class:`TareResult` wraps it
        as a :class:`DataFrame` with read-site timing.
        """
        _logger.info(
            _TARE_FLOW_PRECONDITION,
            extra={
                "unit_id": self._session.unit_id,
                "command": TARE_FLOW.name,
            },
        )
        return await self._execute_tare(TARE_FLOW, TareFlowRequest())

    async def tare_gauge_pressure(self) -> TareResult:
        """Zero the gauge-pressure reading (``TP``).

        Caller's precondition: line depressurised to atmosphere.
        Same INFO-log + data-frame-wrap semantics as :meth:`tare_flow`.
        """
        _logger.info(
            _TARE_GAUGE_PRECONDITION,
            extra={
                "unit_id": self._session.unit_id,
                "command": TARE_GAUGE_PRESSURE.name,
            },
        )
        return await self._execute_tare(
            TARE_GAUGE_PRESSURE,
            TareGaugePressureRequest(),
        )

    async def tare_absolute_pressure(self) -> TareResult:
        """Calibrate absolute pressure against the onboard barometer (``PC``).

        Gated on :attr:`Capability.TAREABLE_ABSOLUTE_PRESSURE` — NOT
        on :attr:`Capability.BAROMETER`. The two dissociate in practice
        (design §16.6.7): flow controllers expose a firmware-computed
        barometer reading but lack a tareable process-port abs sensor.
        Users with a pressure meter/controller that supports ``PC``
        opt in via ``assume_capabilities`` on
        :func:`~alicatlib.devices.factory.open_device`; devices without
        the capability raise :class:`AlicatMissingHardwareError`
        pre-I/O. Same INFO-log + data-frame-wrap semantics as
        :meth:`tare_flow`.
        """
        _logger.info(
            _TARE_ABSOLUTE_PRECONDITION,
            extra={
                "unit_id": self._session.unit_id,
                "command": TARE_ABSOLUTE_PRESSURE.name,
            },
        )
        return await self._execute_tare(
            TARE_ABSOLUTE_PRESSURE,
            TareAbsolutePressureRequest(),
        )

    async def _execute_tare(
        self,
        command: Command[Any, ParsedFrame],
        request: Any,
    ) -> TareResult:
        """Shared tare dispatch: execute → wrap ``ParsedFrame`` → :class:`TareResult`.

        Lazy-probes ``??D*`` if the session's cached format is absent,
        mirroring :meth:`Session.poll`. Timing is captured after the
        command returns — the tare I/O itself takes hundreds of
        milliseconds on hardware, so microsecond-precision read-site
        timing is not warranted here (contrast with :meth:`poll`,
        which rides a 10 Hz recorder loop).
        """
        fmt = self._session.data_frame_format
        if fmt is None:
            fmt = await self._session.refresh_data_frame_format()
        parsed = await self._session.execute(command, request)
        return TareResult(
            frame=DataFrame.from_parsed(
                parsed,
                format=fmt,
                received_at=datetime.now(UTC),
                monotonic_ns=monotonic_ns(),
            ),
        )

    # --------------------------------------------------------------- data readings

    async def zero_band(
        self,
        zero_band: float | None = None,
    ) -> ZeroBandSetting:
        """Query or set the zero band (``DCZ``, V10 10v05+).

        Zero band is a percent-of-full-scale threshold: readings below
        it are reported as zero. ``zero_band=None`` issues the query
        form; a value in ``0..6.38`` sets it (``0`` disables).
        """
        return await self._session.execute(
            ZERO_BAND,
            ZeroBandRequest(zero_band=zero_band),
        )

    async def average_timing(
        self,
        statistic_code: int,
        averaging_ms: int | None = None,
    ) -> AverageTimingSetting:
        """Query or set the per-statistic averaging window (``DCA``, V10 10v05+).

        ``averaging_ms=None`` issues the query form; a value in
        ``0..9999`` sets the window (``0`` → update every millisecond).
        ``statistic_code`` is the primer's numeric code (see
        :data:`DCA_ALLOWED_STATISTIC_CODES`) — arbitrary
        :class:`Statistic` codes are rejected pre-I/O because the
        device only averages pressure / flow primary + secondary
        readings.
        """
        result = await self._session.execute(
            AVERAGE_TIMING,
            AverageTimingRequest(
                statistic_code=statistic_code,
                averaging_ms=averaging_ms,
            ),
        )
        # Hardware-validation finding: real 10v20 firmware drops the echoed
        # statistic code on the DCA reply, so the decoder's shorter-form
        # path leaves statistic_code == 0. Re-populate from the request
        # so callers can trust the returned setting.
        if result.statistic_code == 0 != statistic_code:
            return replace(result, statistic_code=statistic_code)
        return result

    async def stp_ntp_pressure(
        self,
        mode: StpNtpMode,
        pressure: float | None = None,
        unit_code: int | None = None,
    ) -> StpNtpPressureSetting:
        """Query or set the standard / normal pressure reference (``DCFRP``).

        Mass-flow devices only (V10 10v05+). ``mode`` selects STP vs
        NTP reference. ``pressure=None`` issues the query form;
        ``unit_code=None`` or ``0`` on set leaves the engineering
        unit unchanged. The device doesn't echo ``mode`` so the
        facade fills it from the request.
        """
        result = await self._session.execute(
            STP_NTP_PRESSURE,
            StpNtpPressureRequest(
                mode=mode,
                pressure=pressure,
                unit_code=unit_code,
            ),
        )
        return replace(result, mode=mode)

    async def stp_ntp_temperature(
        self,
        mode: StpNtpMode,
        temperature: float | None = None,
        unit_code: int | None = None,
    ) -> StpNtpTemperatureSetting:
        """Query or set the standard / normal temperature reference (``DCFRT``).

        Mirror of :meth:`stp_ntp_pressure` for temperature.
        """
        result = await self._session.execute(
            STP_NTP_TEMPERATURE,
            StpNtpTemperatureRequest(
                mode=mode,
                temperature=temperature,
                unit_code=unit_code,
            ),
        )
        return replace(result, mode=mode)

    # --------------------------------------------------------------- output

    async def analog_output_source(
        self,
        channel: AnalogOutputChannel = AnalogOutputChannel.PRIMARY,
        value: int | None = None,
        unit_code: int | None = None,
    ) -> AnalogOutputSourceSetting:
        """Query or set the analog-output source (``ASOCV``, V10 10v05+).

        Gated on :attr:`Capability.ANALOG_OUTPUT`. ``channel`` selects
        primary vs. secondary. ``value=None`` queries; ``value=0`` /
        ``value=1`` set fixed min / max output; ``value>=2`` pins the
        output to a statistic.
        """
        result = await self._session.execute(
            ANALOG_OUTPUT_SOURCE,
            AnalogOutputSourceRequest(
                channel=channel,
                value=value,
                unit_code=unit_code,
            ),
        )
        return replace(result, channel=channel)

    # --------------------------------------------------------------- display

    async def blink_display(
        self,
        duration_s: int | None = None,
    ) -> BlinkDisplayState:
        """Query or trigger a display blink (``FFP``, 8v28+).

        Gated on :attr:`Capability.DISPLAY`. ``None`` queries the
        current flash state; a positive value flashes for that many
        seconds; ``0`` stops an active flash; ``-1`` flashes
        indefinitely.
        """
        return await self._session.execute(
            BLINK_DISPLAY,
            BlinkDisplayRequest(duration_s=duration_s),
        )

    async def lock_display(self) -> DisplayLockResult:
        """Lock the front-panel display (``L``); reply is a post-op data frame.

        Gated on :attr:`Capability.DISPLAY`. The result's
        :attr:`DisplayLockResult.locked` is ``True`` after a successful
        lock.
        """
        return await self._execute_display_lock(LOCK_DISPLAY, LockDisplayRequest())

    async def unlock_display(self) -> DisplayLockResult:
        """Unlock the front-panel display (``U``); reply is a post-op data frame.

        Intentionally not gated on :attr:`Capability.DISPLAY`: this is
        the safety escape for a device that got into a locked state.
        Always callable. Hardware validation (2026-04-17) verified ``AU``
        works on V1_V7 (7v09), V8_V9, and V10. On a device without a
        display, the command is a harmless no-op; on a locked device
        it clears the ``LCK`` status bit.
        """
        return await self._execute_display_lock(UNLOCK_DISPLAY, UnlockDisplayRequest())

    async def _execute_display_lock(
        self,
        command: Command[Any, ParsedFrame],
        request: Any,
    ) -> DisplayLockResult:
        """Shared dispatch for L / U — execute, wrap post-op frame into DisplayLockResult.

        Same pattern as :meth:`_execute_tare` and the valve-hold
        helper on :class:`_ControllerMixin`: lazy-probe ``??D*`` if
        missing, then build the typed wrapper with facade-level timing.
        """
        fmt = self._session.data_frame_format
        if fmt is None:
            fmt = await self._session.refresh_data_frame_format()
        parsed = await self._session.execute(command, request)
        return DisplayLockResult(
            frame=DataFrame.from_parsed(
                parsed,
                format=fmt,
                received_at=datetime.now(UTC),
                monotonic_ns=monotonic_ns(),
            ),
        )

    # --------------------------------------------------------------- user data

    async def user_data(
        self,
        slot: int,
        value: str | None = None,
    ) -> UserDataSetting:
        r"""Read or write a user-data slot (``UD``, 8v24+).

        Four slots (``0..3``), 32 ASCII characters each. ``value=None``
        reads the slot; a string writes it. Values are validated
        pre-I/O: ASCII-only, ≤ 32 characters, no ``\r`` / ``\n``
        (those would truncate the wire write).
        """
        result = await self._session.execute(
            USER_DATA,
            UserDataRequest(slot=slot, value=value),
        )
        # Hardware-validation finding: real 10v20 firmware returns only the
        # unit id when the slot is empty; the decoder's short-form path
        # marks ``slot`` as ``-1`` sentinel. Re-populate from the
        # request so the returned ``UserDataSetting`` always round-trips.
        if result.slot == -1:
            return replace(result, slot=slot)
        return result

    # --------------------------------------------------------------- totalizer

    async def totalizer_config(
        self,
        totalizer: TotalizerId = TotalizerId.FIRST,
        *,
        flow_statistic_code: int | None = None,
        mode: TotalizerMode | None = None,
        limit_mode: TotalizerLimitMode | None = None,
        digits: int | None = None,
        decimal_place: int | None = None,
    ) -> TotalizerConfig:
        """Query or set a totalizer's configuration (``TC``, V10 10v00+).

        ``flow_statistic_code=None`` issues the query form. ``1``
        disables the totalizer (other fields stay ``None``). Any
        other value enables / reconfigures — ``mode`` /
        ``limit_mode`` / ``digits`` / ``decimal_place`` are required
        together in that case. Use :attr:`TotalizerMode.KEEP` /
        :attr:`TotalizerLimitMode.KEEP` (``-1``) to preserve the
        current value of one field while changing others.

        Returns:
            :class:`TotalizerConfig` — the facade fills
            :attr:`TotalizerConfig.totalizer` from the request since
            the wire reply does not echo the id.
        """
        result = await self._session.execute(
            TOTALIZER_CONFIG,
            TotalizerConfigRequest(
                totalizer=totalizer,
                flow_statistic_code=flow_statistic_code,
                mode=mode,
                limit_mode=limit_mode,
                digits=digits,
                decimal_place=decimal_place,
            ),
        )
        return replace(result, totalizer=totalizer)

    async def totalizer_reset(
        self,
        totalizer: TotalizerId = TotalizerId.FIRST,
        *,
        confirm: bool = False,
    ) -> TotalizerResetResult:
        r"""Reset a totalizer's count (``T <n>``, 8v00+) — destructive.

        Token-collision note: the command spec always emits the
        numeric totalizer argument on the wire, so it can never
        accidentally produce the flow-tare form (bare ``T\r``). The
        destructive-confirm gate on the session requires the caller
        to pass ``confirm=True`` explicitly.
        """
        return await self._execute_totalizer_reset(
            TOTALIZER_RESET,
            TotalizerResetRequest(totalizer=totalizer, confirm=confirm),
        )

    async def totalizer_reset_peak(
        self,
        totalizer: TotalizerId = TotalizerId.FIRST,
        *,
        confirm: bool = False,
    ) -> TotalizerResetResult:
        r"""Reset a totalizer's peak reading (``TP <n>``, 8v00+) — destructive.

        Same token-collision protection as :meth:`totalizer_reset` —
        the spec always emits the numeric argument so ``TP\r``
        (gauge-pressure tare) is unreachable from this path.
        """
        return await self._execute_totalizer_reset(
            TOTALIZER_RESET_PEAK,
            TotalizerResetPeakRequest(totalizer=totalizer, confirm=confirm),
        )

    async def totalizer_save(
        self,
        enable: bool | None = None,
        *,
        save: bool | None = None,
    ) -> TotalizerSaveState:
        """Query or set persist-totalizer-on-power-cycle (``TCR``, V10 10v05+).

        ``enable=None`` queries; ``True`` / ``False`` sets. ``save=True``
        persists the ``TCR`` config itself to EEPROM and feeds through
        the session's EEPROM-wear monitor (design §5.20.7).
        """
        return await self._session.execute(
            TOTALIZER_SAVE,
            TotalizerSaveRequest(enable=enable, save=save),
        )

    async def _execute_totalizer_reset(
        self,
        command: Command[Any, ParsedFrame],
        request: Any,
    ) -> TotalizerResetResult:
        """Shared dispatch for T <n> / TP <n> — wrap post-op frame into TotalizerResetResult.

        Same pattern as :meth:`_execute_tare` / :meth:`_execute_display_lock`:
        lazy-probe ``??D*`` if missing, then build the typed wrapper
        with facade-level timing.
        """
        fmt = self._session.data_frame_format
        if fmt is None:
            fmt = await self._session.refresh_data_frame_format()
        parsed = await self._session.execute(command, request)
        return TotalizerResetResult(
            frame=DataFrame.from_parsed(
                parsed,
                format=fmt,
                received_at=datetime.now(UTC),
                monotonic_ns=monotonic_ns(),
            ),
        )

    # --------------------------------------------------------------- auto/power-up tare

    async def power_up_tare(
        self,
        enable: bool | None = None,
    ) -> PowerUpTareState:
        """Query or set the power-up tare (``ZCP``, V10 10v05+).

        ``None`` queries; ``True`` / ``False`` sets. On a controller
        that enables this, closed-loop control is delayed and valves
        stay closed until the ~0.25 s tare completes at power-on.
        """
        return await self._session.execute(
            POWER_UP_TARE,
            PowerUpTareRequest(enable=enable),
        )

    # --------------------------------------------------------------- streaming

    def stream(
        self,
        *,
        rate_ms: int | None = None,
        strict: bool = False,
        overflow: OverflowPolicy | None = None,
        buffer_size: int = 256,
    ) -> StreamingSession:
        """Open a streaming-mode context for this device.

        Returns a :class:`StreamingSession` — an async context manager
        *and* an async iterator::

            async with dev.stream(rate_ms=50) as stream:
                async for frame in stream:
                    process(frame)

        Streaming is a port-level state transition (design §5.8); while
        the context is active, every other :meth:`execute` / :meth:`poll`
        / etc. on sessions sharing this client's port fails fast with
        :class:`~alicatlib.errors.AlicatStreamingModeError`. One
        streamer per port.

        Args:
            rate_ms: If not ``None``, configures ``NCS`` (streaming
                rate) before entering streaming mode. V10 >= 10v05
                only; older firmware lacks the rate command and keeps
                its 50 ms default. ``None`` leaves the device's current
                rate alone; ``0`` is the device's "as-fast-as-possible"
                setting.
            strict: If ``True``, a malformed frame from the device
                propagates out of ``__anext__`` and tears down the
                stream. Default ``False`` logs and skips.
            overflow: Back-pressure policy when the producer's buffer
                fills. Defaults to
                :attr:`OverflowPolicy.DROP_OLDEST` — latest-data-wins
                is the right default for high-rate telemetry.
            buffer_size: Producer/consumer buffer depth. Default 256
                frames; at the default 50 ms rate that's ~13 s of
                backlog.
        """
        from alicatlib.devices.streaming import (  # noqa: PLC0415 — lazy to avoid import cycle
            OverflowPolicy as _OverflowPolicy,
        )
        from alicatlib.devices.streaming import (  # noqa: PLC0415
            StreamingSession as _StreamingSession,
        )

        return _StreamingSession(
            self._session,
            rate_ms=rate_ms,
            strict=strict,
            overflow=overflow if overflow is not None else _OverflowPolicy.DROP_OLDEST,
            buffer_size=buffer_size,
        )

    # --------------------------------------------------------------- escape hatch

    async def execute[Req, Resp](
        self,
        command: Command[Req, Resp],
        request: Req,
    ) -> Resp:
        """Dispatch a catalog command directly.

        Exposed for advanced users who want to reach commands that don't
        yet have a facade method, or to wrap session.execute with
        middleware. Same gating, same error context, same result types.
        """
        return await self._session.execute(command, request)

    # --------------------------------------------------------------- lifecycle

    async def close(self) -> None:
        """Release the session — idempotent.

        The underlying transport is owned by the async context manager
        returned from :func:`open_device`; closing the device only marks
        the session as closed. Users should prefer
        ``async with open_device(...) as dev:`` over calling ``close()``
        by hand.
        """
        await self._session.close()

    async def __aenter__(self) -> Device:
        """Support ``async with device: ...`` nesting — returns ``self``."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the device on exit — aligns with the factory context manager."""
        del exc_type, exc, tb
        await self.close()
