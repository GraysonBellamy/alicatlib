"""Shared controller surface — ``setpoint`` / ``setpoint_source`` / ``loop_control_variable``.

Both :class:`~alicatlib.devices.flow_controller.FlowController` and
:class:`~alicatlib.devices.pressure_controller.PressureController` expose
the same three controller methods because the underlying command specs
(``LS`` / ``S`` / ``LSS`` / ``LV``) already gate to
``{FLOW_CONTROLLER, PRESSURE_CONTROLLER}``. Duplicating the method
bodies across the two facades would drift; this module hoists them onto
a shared :class:`_ControllerMixin` that both concrete classes inherit.

Design choice — diamond over protocol:

:class:`_ControllerMixin` inherits :class:`~alicatlib.devices.base.Device`.
Concrete controllers inherit from both their meter parent and this
mixin (``class FlowController(FlowMeter, _ControllerMixin): ...``);
Python's C3 MRO collapses the two :class:`Device` bases into one, and
``self._session`` resolves from the single :class:`Device.__init__`.
This keeps the mixin type-checker-friendly (no ``Protocol`` gymnastics)
and preserves ``isinstance(dev, FlowMeter)`` /
``isinstance(dev, PressureMeter)`` semantics.

Design reference: ``docs/design.md`` §5.9, §9 (Tier-2 controller scope).
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic_ns
from typing import TYPE_CHECKING, Any

from alicatlib._logging import get_logger
from alicatlib.commands import (
    AUTO_TARE,
    CANCEL_VALVE_HOLD,
    DEADBAND_LIMIT,
    HOLD_VALVES,
    HOLD_VALVES_CLOSED,
    LOOP_CONTROL_VARIABLE,
    RAMP_RATE,
    SETPOINT,
    SETPOINT_LEGACY,
    SETPOINT_SOURCE,
    VALVE_DRIVE,
    AutoTareRequest,
    CancelValveHoldRequest,
    Capability,
    DeadbandLimitRequest,
    HoldValvesClosedRequest,
    HoldValvesRequest,
    LoopControlVariableRequest,
    RampRateRequest,
    SetpointLegacyRequest,
    SetpointRequest,
    SetpointSourceRequest,
    ValveDriveRequest,
)
from alicatlib.commands._firmware_cutoffs import uses_modern_setpoint
from alicatlib.devices.base import Device
from alicatlib.devices.data_frame import DataFrame
from alicatlib.devices.models import (
    AutoTareState,
    DeadbandSetting,
    LoopControlState,
    RampRateSetting,
    SetpointState,
    TimeUnit,
    ValveDriveState,
    ValveHoldResult,
)
from alicatlib.errors import (
    AlicatUnsupportedCommandError,
    AlicatValidationError,
    ErrorContext,
)
from alicatlib.registry import Statistic

if TYPE_CHECKING:
    from alicatlib.commands import Command
    from alicatlib.devices.data_frame import DataFrameField, ParsedFrame
    from alicatlib.registry import LoopControlVariable, Unit

__all__ = ["_ControllerMixin"]


_logger = get_logger("session")


# The shipped fake-transport fixtures expose the setpoint column as
# ``"Setpoint"``, but real ``??D*`` advertisements name the field after
# the actual controlled variable (``Mass Flow Setpt`` →
# ``"Mass_Flow_Setpt"`` for MFCs, ``Gauge Press Setpt`` →
# ``"Gauge_Press_Setpt"`` for pressure controllers, etc.). Hardware
# validation (2026-04-17) on 7v09 / 8v30 proved a name-based lookup can
# never match on real hardware. The match is semantic: any data-frame
# field whose :class:`Statistic` is one of the ``*_SETPT`` codes.
_SETPOINT_FIELD_NAME = "Setpoint"

_SETPOINT_STATISTICS: frozenset[Statistic] = frozenset(
    {
        Statistic.SETPT,
        Statistic.MASS_FLOW_SETPT,
        Statistic.VOL_FLOW_SETPT,
        Statistic.GAUGE_PRESS_SETPT,
        Statistic.ABS_PRESS_SETPT,
        Statistic.DIFF_PRESS_SETPT,
    }
)


class _ControllerMixin(Device):
    """Controller-kind methods — ``setpoint``, ``setpoint_source``, ``loop_control_variable``.

    Shared between :class:`FlowController` and :class:`PressureController`.
    The mixin is package-private (underscore prefix): users branch on
    the two concrete classes, not the mixin itself.
    """

    async def setpoint(
        self,
        value: float | None = None,
        unit: Unit | str | None = None,
    ) -> SetpointState:
        """Query or set the active setpoint.

        ``value=None`` issues the query form. A numeric ``value``
        dispatches to :data:`SETPOINT` (``LS``) on firmware supporting
        the modern form, or :data:`SETPOINT_LEGACY` (``S``) otherwise —
        selected by
        :func:`~alicatlib.commands._firmware_cutoffs.uses_modern_setpoint`.

        Pre-I/O safety (design §5.20.2):

        - Negative ``value`` requires :attr:`Capability.BIDIRECTIONAL`
          on the device; otherwise :class:`AlicatValidationError`. Most
          common on bidirectional MFCs and vacuum pressure controllers;
          a unidirectional positive-pressure controller rejects
          negative targets.
        - If the session's cached :attr:`Session.setpoint_source` is
          ``"A"`` (analog), a serial setpoint write is silently
          ignored by the device — :class:`AlicatValidationError`
          raises pre-I/O with guidance to call :meth:`setpoint_source`
          to switch to ``"S"`` or ``"U"``. The check is skipped when
          the cache is ``None`` (never probed).

          **V1_V7 / pre-9v00 V8_V9 / GP caveat** (design
          §16.6.10): these firmware families have no ``LSS`` command
          to probe the setpoint source, so the library cannot cache
          it and the analog-source guard never fires. On those
          devices, if the front panel has the setpoint source set to
          Analog or User, the legacy ``S <value>`` write reaches the
          wire cleanly but the device silently ignores it — the
          setpoint follows the analog input instead. The wire bytes
          are correct and the decoder returns a valid
          :class:`SetpointState`, but the post-op frame will reflect
          the analog value, not the commanded one. Users relying on
          serial setpoints on these firmware families must configure
          the device's source to Serial via the front panel before
          opening; the library cannot verify it remotely.
        - Full-scale range check against
          :attr:`DeviceInfo.full_scale` using the cached loop-control
          variable (set by :func:`open_device`'s ``LV`` prefetch for
          firmware that supports it; refreshed on every
          :meth:`loop_control_variable` call).

        Returns:
            :class:`SetpointState`: post-op state with ``current`` and
            ``requested`` populated. The modern ``LS`` reply carries
            both on the wire (5-field reply); the legacy ``S`` path
            derives both from the ``Setpoint`` column of the post-op
            data frame.

        Raises:
            AlicatUnsupportedCommandError: Query form on legacy
                firmware (``S`` has no query form).
            AlicatValidationError: Negative value without
                BIDIRECTIONAL capability, LSS=A cached, or value
                outside the cached full-scale range.
        """
        self._validate_setpoint_preconditions(value)

        firmware = self._session.firmware
        del unit  # Unit conversion awaits DCU-driven binding on the field.
        modern = uses_modern_setpoint(firmware)

        if value is not None:
            # Setpoint-change INFO per design §5.19 / §15.2: a write to
            # setpoint is operationally significant (moves fluid,
            # changes process state) — log before I/O with intent +
            # dispatch path.
            _logger.info(
                "setpoint_change",
                extra={
                    "unit_id": self._session.unit_id,
                    "command": SETPOINT.name if modern else SETPOINT_LEGACY.name,
                    "value": value,
                    "path": "modern" if modern else "legacy",
                    "device_kind": self._session.info.kind.value,
                },
            )

        if modern:
            # Modern LS reply is fully-typed already (5-field reply
            # with current + requested explicitly on the wire — design
            # §16.6).
            return await self._session.execute(
                SETPOINT,
                SetpointRequest(value=value),
            )

        # Legacy S path: pre-9v00 firmware. S is set-only and replies
        # with a full data frame; the facade wraps it into
        # SetpointState using the frame's Setpoint column.
        if value is None:
            raise AlicatUnsupportedCommandError(
                "setpoint() query form requires firmware supporting LS "
                f"(V8_V9 ≥ 9v00 or V10); this device reports {firmware}. "
                "Legacy S is set-only.",
                context=ErrorContext(
                    command_name="setpoint_legacy",
                    unit_id=self._session.unit_id,
                    firmware=firmware,
                ),
            )
        parsed = await self._session.execute(
            SETPOINT_LEGACY,
            SetpointLegacyRequest(value=value),
        )
        fmt = self._session.data_frame_format
        if fmt is None:
            fmt = await self._session.refresh_data_frame_format()
        frame = DataFrame.from_parsed(
            parsed,
            format=fmt,
            received_at=datetime.now(UTC),
            monotonic_ns=monotonic_ns(),
        )
        return _build_setpoint_state(frame)

    async def setpoint_source(
        self,
        mode: str | None = None,
        *,
        save: bool | None = None,
    ) -> str:
        """Query or set the setpoint source (``LSS``).

        Accepts ``"S"`` (serial), ``"A"`` (analog knob / 4–20 mA), or
        ``"U"`` (user-knob / front panel). Every call updates the
        session's cached :attr:`Session.setpoint_source` so
        :meth:`setpoint` can detect the silently-ignored-setpoint
        failure mode (design §5.20 risk table).

        ``save=True`` persists to EEPROM; subject to the
        :attr:`AlicatConfig.save_rate_warn_per_min` rate-warn guard.

        Returns:
            The mode string the device reports — identical shape for
            query and set paths.
        """
        if mode is not None:
            _logger.info(
                "setpoint_source_change",
                extra={
                    "unit_id": self._session.unit_id,
                    "command": SETPOINT_SOURCE.name,
                    "requested_mode": mode,
                    "save": bool(save),
                    "device_kind": self._session.info.kind.value,
                },
            )
        result = await self._session.execute(
            SETPOINT_SOURCE,
            SetpointSourceRequest(mode=mode, save=save),
        )
        self._session.update_setpoint_source(result.mode)
        return result.mode

    async def loop_control_variable(
        self,
        variable: LoopControlVariable | Statistic | str | int | None = None,
    ) -> LoopControlState:
        """Query or set the loop-control variable (``LV``).

        ``variable=None`` issues the query form. Accepts a
        :class:`LoopControlVariable`, a :class:`Statistic` whose code
        is in the LV-eligible subset, an integer wire code, or a
        member-name string (case-insensitive). Ineligible values
        raise :class:`AlicatValidationError` pre-I/O with a listing of
        acceptable statistics.

        Firmware gating (``9v00+`` within V8_V9, all V10) is enforced
        by the session's ``firmware_families`` / ``min_firmware``
        check; older firmware raises :class:`AlicatFirmwareError`
        with a clear reason.
        """
        if variable is not None:
            # Prefer the enum's ``.name`` for readability
            # (``MASS_FLOW_SETPT`` beats ``'37'``); fall back to
            # ``str()`` for int / str / alias inputs that don't carry a
            # symbolic name.
            _requested_repr = getattr(variable, "name", None) or str(variable)
            _logger.info(
                "loop_control_variable_change",
                extra={
                    "unit_id": self._session.unit_id,
                    "command": LOOP_CONTROL_VARIABLE.name,
                    "requested_variable": _requested_repr,
                    "device_kind": self._session.info.kind.value,
                },
            )
        result = await self._session.execute(
            LOOP_CONTROL_VARIABLE,
            LoopControlVariableRequest(variable=variable),
        )
        # Cache so setpoint() can look up the matching FullScaleValue
        # for pre-I/O range validation (design §5.20.2).
        self._session.update_loop_control_variable(result.variable)
        return result

    # ------------------------------------------------------------------ valve control

    async def hold_valves(self) -> ValveHoldResult:
        """Hold valve(s) at their current position (``HP``).

        Pauses closed-loop control; the device keeps the valves at
        their last drive state until :meth:`cancel_valve_hold` is
        called. The reply is a post-op data frame with
        :attr:`StatusCode.HLD` active — reflected on
        :attr:`ValveHoldResult.held`.

        Firmware-gated at ``5v07+`` within V1_V7 (V8_V9, V10
        unconditional) by the underlying :data:`HOLD_VALVES` spec.
        """
        return await self._execute_hold(HOLD_VALVES, HoldValvesRequest())

    async def hold_valves_closed(self, *, confirm: bool = False) -> ValveHoldResult:
        """Force valve(s) closed (``HC``) — destructive.

        Stops flow immediately and interrupts any active closed-loop
        control. ``confirm=True`` is required — the session's
        destructive-confirm gate rejects the call otherwise (design
        §5.4 gating step 5). Use :meth:`cancel_valve_hold` to resume
        normal control.

        Firmware-gated at ``5v07+`` within V1_V7.
        """
        _logger.info(
            "hold_valves_closed",
            extra={
                "unit_id": self._session.unit_id,
                "command": HOLD_VALVES_CLOSED.name,
                "device_kind": self._session.info.kind.value,
            },
        )
        return await self._execute_hold(
            HOLD_VALVES_CLOSED,
            HoldValvesClosedRequest(confirm=confirm),
        )

    async def cancel_valve_hold(self) -> ValveHoldResult:
        """Cancel any active valve hold (``C``) and resume closed-loop control.

        Safe to call when no hold is active — the primer documents
        that the device still responds with a data frame (without the
        ``HLD`` status bit) in that case, and
        :attr:`ValveHoldResult.held` will be ``False``.

        No primer firmware cutoff.
        """
        return await self._execute_hold(CANCEL_VALVE_HOLD, CancelValveHoldRequest())

    async def valve_drive(self) -> ValveDriveState:
        """Query valve drive percentages (``VD``).

        Returns a :class:`ValveDriveState` with 1–3 percentages
        (single / dual / triple-valve controllers). Per design §9,
        multi-valve-specific logic should gate on
        :attr:`Capability.MULTI_VALVE` / :attr:`THIRD_VALVE` rather
        than inferring valve count from ``len(state.valves)`` — the
        capability flags are probed once at ``open_device`` and
        survive firmware quirks the VD reply shape does not.

        Firmware-gated at ``8v18+`` within V8_V9 (V10 unconditional);
        V1_V7 and GP do not support ``VD``.
        """
        return await self._session.execute(VALVE_DRIVE, ValveDriveRequest())

    async def _execute_hold(
        self,
        command: Command[Any, ParsedFrame],
        request: Any,
    ) -> ValveHoldResult:
        """Shared hold-command dispatch: execute → wrap post-op frame.

        Mirrors :meth:`Device._execute_tare`: lazy-probes ``??D*`` if
        the session's cached format is absent, parses the frame, and
        wraps it into a :class:`ValveHoldResult` with facade-level
        timing. Hold operations are a few-ms wire round-trip, not a
        10 Hz loop, so read-site microsecond precision is unwarranted.
        """
        fmt = self._session.data_frame_format
        if fmt is None:
            fmt = await self._session.refresh_data_frame_format()
        parsed = await self._session.execute(command, request)
        return ValveHoldResult(
            frame=DataFrame.from_parsed(
                parsed,
                format=fmt,
                received_at=datetime.now(UTC),
                monotonic_ns=monotonic_ns(),
            ),
        )

    # ------------------------------------------------------------------ control setup

    async def ramp_rate(
        self,
        max_ramp: float | None = None,
        time_unit: TimeUnit | None = None,
    ) -> RampRateSetting:
        """Query or set the max ramp rate (``SR``, 7v11+).

        ``max_ramp=None`` issues the query form. A numeric value
        sets the ramp step size in the current engineering units;
        ``0.0`` disables ramping (the controller jumps to the new
        setpoint instantly on the next write). ``time_unit`` is
        required whenever ``max_ramp`` is not ``None`` — even when
        disabling — because the primer's set form always carries a
        time-unit slot.

        Returns:
            :class:`RampRateSetting` with the typed :attr:`time_unit`,
            resolved :attr:`setpoint_unit` (or ``None`` if the wire
            label doesn't resolve), and the device's verbatim
            ``rate_unit_label`` (e.g. ``"SCCM/s"``).
        """
        if max_ramp is not None:
            _logger.info(
                "ramp_rate_change",
                extra={
                    "unit_id": self._session.unit_id,
                    "command": RAMP_RATE.name,
                    "max_ramp": max_ramp,
                    "time_unit": time_unit.name if time_unit is not None else None,
                    "device_kind": self._session.info.kind.value,
                },
            )
        return await self._session.execute(
            RAMP_RATE,
            RampRateRequest(max_ramp=max_ramp, time_unit=time_unit),
        )

    async def deadband_limit(
        self,
        deadband: float | None = None,
        *,
        save: bool | None = None,
    ) -> DeadbandSetting:
        """Query or set the deadband limit (``LCDB``, 10v05+).

        ``deadband=None`` issues the query form. A numeric value sets
        the allowable drift around the setpoint in the controlled
        variable's engineering units; ``0.0`` disables the deadband.

        ``save=True`` persists the new value to EEPROM — subject to
        :attr:`AlicatConfig.save_rate_warn_per_min` at the
        session's EEPROM-wear monitor (design §5.20.7).

        Returns:
            :class:`DeadbandSetting` with the typed :attr:`unit`
            (or ``None`` if the label doesn't resolve) and the
            device's verbatim ``unit_label``.
        """
        if deadband is not None:
            _logger.info(
                "deadband_limit_change",
                extra={
                    "unit_id": self._session.unit_id,
                    "command": DEADBAND_LIMIT.name,
                    "deadband": deadband,
                    "save": bool(save),
                    "device_kind": self._session.info.kind.value,
                },
            )
        return await self._session.execute(
            DEADBAND_LIMIT,
            DeadbandLimitRequest(deadband=deadband, save=save),
        )

    # ------------------------------------------------------------------ auto-tare

    async def auto_tare(
        self,
        enable: bool | None = None,
        delay_s: float | None = None,
    ) -> AutoTareState:
        """Query or set auto-tare on zero-setpoint (``ZCA``, controllers, V10 10v05+).

        Controllers only. When enabled, the controller tares
        automatically whenever it sees a zero setpoint, waiting
        ``delay_s`` seconds for flow / pressure to settle before the
        tare. Primer constrains ``delay_s`` to ``[0.1, 25.5]`` — the
        encoder validates pre-I/O.

        ``enable=None`` queries. ``enable=True`` requires ``delay_s``.
        ``enable=False`` without ``delay_s`` sends ``0`` in the delay
        slot (primer's wire form requires the slot even when
        disabling).
        """
        if enable is not None:
            _logger.info(
                "auto_tare_change",
                extra={
                    "unit_id": self._session.unit_id,
                    "command": AUTO_TARE.name,
                    "enable": enable,
                    "delay_s": delay_s,
                    "device_kind": self._session.info.kind.value,
                },
            )
        return await self._session.execute(
            AUTO_TARE,
            AutoTareRequest(enable=enable, delay_s=delay_s),
        )

    # ------------------------------------------------------------------ internals

    def _validate_setpoint_preconditions(self, value: float | None) -> None:
        """Pre-I/O safety checks shared by modern / legacy setpoint paths.

        Query form (``value is None``) bypasses the value-dependent
        checks; only the LSS cache gate would apply, and a query on an
        analog-sourced device is still informative (returns the
        device-reported setpoint), so we let it through.
        """
        if value is None:
            return

        caps = self._session.info.capabilities
        if value < 0 and Capability.BIDIRECTIONAL not in caps:
            raise AlicatValidationError(
                f"setpoint value {value!r} is negative but device does not "
                "advertise Capability.BIDIRECTIONAL; pass a non-negative "
                "value or re-identify with assume_capabilities=Capability.BIDIRECTIONAL "
                "if the device supports it.",
                context=ErrorContext(
                    command_name="setpoint",
                    unit_id=self._session.unit_id,
                    extra={
                        "value": value,
                        "capabilities": caps.name or str(caps),
                    },
                ),
            )

        cached_source = self._session.setpoint_source
        if cached_source == "A":
            raise AlicatValidationError(
                "setpoint source is analog (LSS=A); a serial setpoint write "
                "is silently ignored by the device. Switch sources with "
                "setpoint_source('S') or setpoint_source('U') before setting.",
                context=ErrorContext(
                    command_name="setpoint",
                    unit_id=self._session.unit_id,
                    extra={
                        "setpoint_source": cached_source,
                        "requested_value": value,
                    },
                ),
            )

        self._validate_setpoint_full_scale(value)

    def _validate_setpoint_full_scale(self, value: float) -> None:
        """Range-check ``value`` against the cached FPF full-scale (design §5.20.2).

        Requires two pieces of cached state:

        - :attr:`Session.loop_control_variable` — which statistic the
          controller's loop is tracking. Pre-populated by
          :func:`~alicatlib.devices.factory.open_device` and refreshed
          every :meth:`loop_control_variable` call.
        - :attr:`DeviceInfo.full_scale` — ``FPF``-probed per-statistic
          range, cached at identification time.

        Either missing → skip the check. This keeps the facade usable
        on firmware that doesn't support ``LV`` / ``FPF`` (V1_V7, GP)
        and on sessions where the probe was skipped; the device will
        still reject / clamp an out-of-range value, so we're only
        adding a pre-I/O short-circuit here, not a sole defence.
        """
        lv = self._session.loop_control_variable
        if lv is None:
            return
        full_scale = self._session.info.full_scale.get(lv.statistic)
        if full_scale is None:
            return

        caps = self._session.info.capabilities
        bidirectional = Capability.BIDIRECTIONAL in caps
        fs_value = full_scale.value
        lower = -fs_value if bidirectional else 0.0
        upper = fs_value
        if value < lower or value > upper:
            raise AlicatValidationError(
                f"setpoint value {value!r} is outside the device's full-scale "
                f"range [{lower}, {upper}] {full_scale.unit_label} "
                f"(loop-control variable: {lv.name}). The device would "
                "reject or clamp the write; pass a value within range.",
                context=ErrorContext(
                    command_name="setpoint",
                    unit_id=self._session.unit_id,
                    extra={
                        "value": value,
                        "full_scale": fs_value,
                        "full_scale_unit": full_scale.unit_label,
                        "loop_control_variable": lv.name,
                        "bidirectional": bidirectional,
                    },
                ),
            )


def _build_setpoint_state(frame: DataFrame) -> SetpointState:
    """Extract setpoint info from a legacy-path post-op data frame.

    Only the legacy ``S`` path goes through here — the modern ``LS``
    reply is a 5-field typed reply and the decoder builds a
    :class:`SetpointState` directly. Legacy ``S`` replies with a full
    data frame, so we pull ``current`` / ``requested`` from the frame's
    ``Setpoint`` column (same value — the primer's legacy shape
    doesn't distinguish, which is the price of supporting pre-9v00
    firmware).

    ``unit`` / ``unit_label`` come from the matching
    :class:`DataFrameField`: ``??D*`` binds the unit inline when the
    reply carries a recognisable label, and the factory's
    ``DCU`` probe fills the gap on firmware whose ``??D*`` omits it.
    Unresolvable units leave both ``unit`` and ``unit_label`` as
    ``None`` — honest beats guessing the raw field name.
    """
    field = _find_setpoint_field(frame)
    setpoint_value = frame.values.get(field.name) if field is not None else None
    if not isinstance(setpoint_value, int | float):
        raise AlicatValidationError(
            "setpoint reply data frame has no numeric setpoint field "
            f"(searched by *_SETPT statistic and name {_SETPOINT_FIELD_NAME!r}); "
            f"got {setpoint_value!r}. Frame keys: "
            f"{sorted(frame.values.keys())}",
            context=ErrorContext(
                command_name="setpoint",
                unit_id=frame.unit_id,
                extra={"frame_keys": sorted(frame.values.keys())},
            ),
        )
    setpoint_float = float(setpoint_value)

    unit = field.unit if field is not None else None
    unit_label = unit.value if unit is not None else None

    return SetpointState(
        unit_id=frame.unit_id,
        current=setpoint_float,
        requested=setpoint_float,
        unit=unit,
        unit_label=unit_label,
        frame=frame,
    )


def _find_setpoint_field(frame: DataFrame) -> DataFrameField | None:
    """Locate the :class:`DataFrameField` for the active setpoint column.

    Searches in this order:

    1. Any field whose :class:`Statistic` is one of the ``*_SETPT``
       codes — the real ``??D*`` advertisements on every captured
       device use names derived from the controlled variable (e.g.
       ``Mass_Flow_Setpt`` on MFCs, ``Gauge_Press_Setpt`` on pressure
       controllers), not the literal string ``"Setpoint"``.
    2. The literal name ``"Setpoint"`` as a fallback for test
       fixtures that don't carry statistic codes.

    Hardware validation (2026-04-17): switching from name-based to
    statistic-based lookup was necessary to make legacy ``S``
    setpoint writes decode on real hardware.
    """
    for candidate in frame.format.fields:
        if candidate.statistic in _SETPOINT_STATISTICS:
            return candidate
    for candidate in frame.format.fields:
        if candidate.name == _SETPOINT_FIELD_NAME:
            return candidate
    return None
