"""Sync device facade — portal-driven wrapper over :class:`Device`.

Each :class:`SyncDevice` holds a reference to an async
:class:`~alicatlib.devices.base.Device` and a
:class:`~alicatlib.sync.portal.SyncPortal`; every public method is a
one-liner that hands the underlying coroutine to the portal. The
subclass tree mirrors the async side so ``isinstance`` checks work the
same way:

* :class:`SyncDevice` — base, all shared methods.
* :class:`SyncFlowMeter` — tag only (pass-through, design §5.9).
* :class:`SyncFlowController` — adds ``setpoint``,
  ``setpoint_source``, ``loop_control_variable``.
* :class:`SyncPressureMeter` / :class:`SyncPressureController` — tag
  only today (controller-only pressure surface is planned future work).

The :class:`Alicat` namespace exposes a ``Alicat.open(...)`` context
manager that drives the async
:func:`~alicatlib.devices.factory.open_device` through the portal. By
default each ``Alicat.open`` owns its own portal; callers that need
several contexts to share an event loop can pass ``portal=`` to reuse
a long-lived :class:`SyncPortal`.

Design reference: ``docs/design.md`` §5.16.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import TYPE_CHECKING, Self, cast

from alicatlib.commands import Capability
from alicatlib.devices.factory import open_device
from alicatlib.devices.flow_controller import FlowController
from alicatlib.devices.flow_meter import FlowMeter
from alicatlib.devices.models import AnalogOutputChannel as _AnalogOutputChannel
from alicatlib.devices.models import TotalizerId as _TotalizerId
from alicatlib.devices.pressure_controller import PressureController
from alicatlib.devices.pressure_meter import PressureMeter
from alicatlib.sync.portal import SyncPortal

# Module-level enum sentinels so sync wrappers use the same defaults
# as their async counterparts. The sync-parity suite compares
# parameter defaults by equality; binding the enum members at module
# load lets callers override without importing the enum directly.
_ANALOG_OUTPUT_PRIMARY = _AnalogOutputChannel.PRIMARY
_TOTALIZER_FIRST = _TotalizerId.FIRST

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from types import TracebackType

    from alicatlib.commands import Command, GasState
    from alicatlib.devices.base import Device
    from alicatlib.devices.data_frame import DataFrame
    from alicatlib.devices.medium import Medium
    from alicatlib.devices.models import (
        AnalogOutputChannel,
        AnalogOutputSourceSetting,
        AutoTareState,
        AverageTimingSetting,
        BlinkDisplayState,
        DeadbandSetting,
        DeviceInfo,
        DisplayLockResult,
        FullScaleValue,
        LoopControlState,
        MeasurementSet,
        PowerUpTareState,
        RampRateSetting,
        SetpointState,
        StpNtpMode,
        StpNtpPressureSetting,
        StpNtpTemperatureSetting,
        TareResult,
        TimeUnit,
        TotalizerConfig,
        TotalizerId,
        TotalizerLimitMode,
        TotalizerMode,
        TotalizerResetResult,
        TotalizerSaveState,
        UnitSetting,
        UserDataSetting,
        ValveDriveState,
        ValveHoldResult,
        ZeroBandSetting,
    )
    from alicatlib.devices.session import Session
    from alicatlib.devices.streaming import OverflowPolicy, StreamingSession
    from alicatlib.protocol import AlicatProtocolClient
    from alicatlib.registry import Gas, LoopControlVariable, Statistic, Unit
    from alicatlib.transport.base import SerialSettings, Transport

__all__ = [
    "Alicat",
    "SyncDevice",
    "SyncFlowController",
    "SyncFlowMeter",
    "SyncPressureController",
    "SyncPressureMeter",
    "SyncStreamingSession",
]


class SyncDevice:
    """Blocking facade over :class:`alicatlib.devices.base.Device`.

    Instances are produced by :meth:`Alicat.open`; users do not call
    this constructor directly. Every public method delegates to the
    underlying :class:`Device` through a :class:`SyncPortal`.
    """

    def __init__(self, async_device: Device, portal: SyncPortal) -> None:
        self._dev = async_device
        self._portal = portal

    # --------------------------------------------------------------- identity

    @property
    def info(self) -> DeviceInfo:
        """Identity snapshot — identical to :attr:`Device.info`."""
        return self._dev.info

    @property
    def unit_id(self) -> str:
        """Validated single-letter unit id this device is addressed by."""
        return self._dev.unit_id

    @property
    def session(self) -> Session:
        """Underlying async :class:`Session` for advanced escape-hatch use.

        Calling coroutine methods on the returned session requires the
        caller to route them through :attr:`portal`.
        """
        return self._dev.session

    @property
    def portal(self) -> SyncPortal:
        """The :class:`SyncPortal` this device runs its coroutines on."""
        return self._portal

    # --------------------------------------------------------------- polling

    def poll(self) -> DataFrame:
        """Blocking :meth:`Device.poll`."""
        return self._portal.call(self._dev.poll)

    def request(
        self,
        statistics: Sequence[Statistic | str],
        *,
        averaging_ms: int = 1,
    ) -> MeasurementSet:
        """Blocking :meth:`Device.request`."""
        return self._portal.call(
            self._dev.request,
            statistics,
            averaging_ms=averaging_ms,
        )

    # --------------------------------------------------------------- gas

    def gas(
        self,
        gas: Gas | str | None = None,
        *,
        save: bool | None = None,
    ) -> GasState:
        """Blocking :meth:`Device.gas`."""
        return self._portal.call(self._dev.gas, gas, save=save)

    def gas_list(self) -> Mapping[int, str]:
        """Blocking :meth:`Device.gas_list`."""
        return self._portal.call(self._dev.gas_list)

    # --------------------------------------------------------------- units

    def engineering_units(
        self,
        statistic: Statistic | str,
        unit: Unit | int | str | None = None,
        *,
        apply_to_group: bool = False,
        override_special_rules: bool = False,
    ) -> UnitSetting:
        """Blocking :meth:`Device.engineering_units`."""
        return self._portal.call(
            self._dev.engineering_units,
            statistic,
            unit,
            apply_to_group=apply_to_group,
            override_special_rules=override_special_rules,
        )

    def full_scale(self, statistic: Statistic | str) -> FullScaleValue:
        """Blocking :meth:`Device.full_scale`."""
        return self._portal.call(self._dev.full_scale, statistic)

    # --------------------------------------------------------------- tare

    def tare_flow(self) -> TareResult:
        """Blocking :meth:`Device.tare_flow`."""
        return self._portal.call(self._dev.tare_flow)

    def tare_gauge_pressure(self) -> TareResult:
        """Blocking :meth:`Device.tare_gauge_pressure`."""
        return self._portal.call(self._dev.tare_gauge_pressure)

    def tare_absolute_pressure(self) -> TareResult:
        """Blocking :meth:`Device.tare_absolute_pressure`."""
        return self._portal.call(self._dev.tare_absolute_pressure)

    # --------------------------------------------------------------- data readings

    def zero_band(self, zero_band: float | None = None) -> ZeroBandSetting:
        """Blocking :meth:`Device.zero_band`."""
        return self._portal.call(self._dev.zero_band, zero_band)

    def average_timing(
        self,
        statistic_code: int,
        averaging_ms: int | None = None,
    ) -> AverageTimingSetting:
        """Blocking :meth:`Device.average_timing`."""
        return self._portal.call(self._dev.average_timing, statistic_code, averaging_ms)

    def stp_ntp_pressure(
        self,
        mode: StpNtpMode,
        pressure: float | None = None,
        unit_code: int | None = None,
    ) -> StpNtpPressureSetting:
        """Blocking :meth:`Device.stp_ntp_pressure`."""
        return self._portal.call(self._dev.stp_ntp_pressure, mode, pressure, unit_code)

    def stp_ntp_temperature(
        self,
        mode: StpNtpMode,
        temperature: float | None = None,
        unit_code: int | None = None,
    ) -> StpNtpTemperatureSetting:
        """Blocking :meth:`Device.stp_ntp_temperature`."""
        return self._portal.call(self._dev.stp_ntp_temperature, mode, temperature, unit_code)

    # --------------------------------------------------------------- output

    def analog_output_source(
        self,
        channel: AnalogOutputChannel = _ANALOG_OUTPUT_PRIMARY,
        value: int | None = None,
        unit_code: int | None = None,
    ) -> AnalogOutputSourceSetting:
        """Blocking :meth:`Device.analog_output_source`.

        Default channel is :attr:`AnalogOutputChannel.PRIMARY` — same
        default as the async side, bound at module load via the
        module-level alias so sync-parity signature comparison sees
        the same sentinel object.
        """
        return self._portal.call(self._dev.analog_output_source, channel, value, unit_code)

    # --------------------------------------------------------------- display

    def blink_display(self, duration_s: int | None = None) -> BlinkDisplayState:
        """Blocking :meth:`Device.blink_display`."""
        return self._portal.call(self._dev.blink_display, duration_s)

    def lock_display(self) -> DisplayLockResult:
        """Blocking :meth:`Device.lock_display`."""
        return self._portal.call(self._dev.lock_display)

    def unlock_display(self) -> DisplayLockResult:
        """Blocking :meth:`Device.unlock_display`."""
        return self._portal.call(self._dev.unlock_display)

    # --------------------------------------------------------------- user data

    def user_data(self, slot: int, value: str | None = None) -> UserDataSetting:
        """Blocking :meth:`Device.user_data`."""
        return self._portal.call(self._dev.user_data, slot, value)

    # --------------------------------------------------------------- power-up tare

    def power_up_tare(self, enable: bool | None = None) -> PowerUpTareState:
        """Blocking :meth:`Device.power_up_tare`."""
        return self._portal.call(self._dev.power_up_tare, enable)

    # --------------------------------------------------------------- totalizer

    def totalizer_config(
        self,
        totalizer: TotalizerId = _TOTALIZER_FIRST,
        *,
        flow_statistic_code: int | None = None,
        mode: TotalizerMode | None = None,
        limit_mode: TotalizerLimitMode | None = None,
        digits: int | None = None,
        decimal_place: int | None = None,
    ) -> TotalizerConfig:
        """Blocking :meth:`Device.totalizer_config`."""
        return self._portal.call(
            self._dev.totalizer_config,
            totalizer,
            flow_statistic_code=flow_statistic_code,
            mode=mode,
            limit_mode=limit_mode,
            digits=digits,
            decimal_place=decimal_place,
        )

    def totalizer_reset(
        self,
        totalizer: TotalizerId = _TOTALIZER_FIRST,
        *,
        confirm: bool = False,
    ) -> TotalizerResetResult:
        """Blocking :meth:`Device.totalizer_reset` — destructive; requires ``confirm=True``."""
        return self._portal.call(self._dev.totalizer_reset, totalizer, confirm=confirm)

    def totalizer_reset_peak(
        self,
        totalizer: TotalizerId = _TOTALIZER_FIRST,
        *,
        confirm: bool = False,
    ) -> TotalizerResetResult:
        """Blocking :meth:`Device.totalizer_reset_peak` — destructive."""
        return self._portal.call(self._dev.totalizer_reset_peak, totalizer, confirm=confirm)

    def totalizer_save(
        self,
        enable: bool | None = None,
        *,
        save: bool | None = None,
    ) -> TotalizerSaveState:
        """Blocking :meth:`Device.totalizer_save`."""
        return self._portal.call(self._dev.totalizer_save, enable, save=save)

    # --------------------------------------------------------------- streaming

    def stream(
        self,
        *,
        rate_ms: int | None = None,
        strict: bool = False,
        overflow: OverflowPolicy | None = None,
        buffer_size: int = 256,
    ) -> SyncStreamingSession:
        """Blocking :meth:`Device.stream` — returns a sync context manager.

        The returned :class:`SyncStreamingSession` is both a sync
        context manager and a sync iterator; use it as::

            with sync_dev.stream(rate_ms=50) as stream:
                for frame in stream:
                    process(frame)
        """
        async_stream = self._dev.stream(
            rate_ms=rate_ms,
            strict=strict,
            overflow=overflow,
            buffer_size=buffer_size,
        )
        return SyncStreamingSession(async_stream, self._portal)

    # --------------------------------------------------------------- escape hatch

    def execute[Req, Resp](
        self,
        command: Command[Req, Resp],
        request: Req,
    ) -> Resp:
        """Blocking :meth:`Device.execute`."""
        return self._portal.call(self._dev.execute, command, request)

    # --------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Release the underlying session — idempotent.

        Prefer ``with Alicat.open(...) as dev:`` over calling this by
        hand; the outer context manager owns transport lifecycle.
        """
        self._portal.call(self._dev.close)

    def __enter__(self) -> Self:
        """Nesting convenience — matches :meth:`Device.__aenter__`."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the device on exit — session close is idempotent."""
        del exc_type, exc, tb
        self.close()


class SyncFlowMeter(SyncDevice):
    """Flow-meter tag — empty pass-through, mirrors :class:`FlowMeter`."""


class _SyncControllerMixin(SyncDevice):
    """Blocking wrappers for the shared controller surface.

    Mirrors the async
    :class:`~alicatlib.devices._controller._ControllerMixin`; both
    :class:`SyncFlowController` and :class:`SyncPressureController`
    inherit these three methods so the pressure-controller sync
    surface stays in parity with the flow-controller one without
    copy-paste drift.
    """

    def setpoint(
        self,
        value: float | None = None,
        unit: Unit | str | None = None,
    ) -> SetpointState:
        """Blocking :meth:`FlowController.setpoint` / :meth:`PressureController.setpoint`."""
        return self._portal.call(self._controller_dev.setpoint, value, unit)

    def setpoint_source(
        self,
        mode: str | None = None,
        *,
        save: bool | None = None,
    ) -> str:
        """Blocking setpoint-source query/set — see :meth:`FlowController.setpoint_source`."""
        return self._portal.call(self._controller_dev.setpoint_source, mode, save=save)

    def loop_control_variable(
        self,
        variable: LoopControlVariable | Statistic | str | int | None = None,
    ) -> LoopControlState:
        """Blocking LV query/set — see :meth:`FlowController.loop_control_variable`."""
        return self._portal.call(self._controller_dev.loop_control_variable, variable)

    # ------------------------------------------------------------------ valve control

    def hold_valves(self) -> ValveHoldResult:
        """Blocking :meth:`FlowController.hold_valves` / :meth:`PressureController.hold_valves`."""
        return self._portal.call(self._controller_dev.hold_valves)

    def hold_valves_closed(self, *, confirm: bool = False) -> ValveHoldResult:
        """Blocking hold-valves-closed — destructive; see async counterpart for docs."""
        return self._portal.call(self._controller_dev.hold_valves_closed, confirm=confirm)

    def cancel_valve_hold(self) -> ValveHoldResult:
        """Blocking cancel-valve-hold — see :meth:`FlowController.cancel_valve_hold`."""
        return self._portal.call(self._controller_dev.cancel_valve_hold)

    def valve_drive(self) -> ValveDriveState:
        """Blocking :meth:`FlowController.valve_drive` / :meth:`PressureController.valve_drive`."""
        return self._portal.call(self._controller_dev.valve_drive)

    # ------------------------------------------------------------------ control setup

    def ramp_rate(
        self,
        max_ramp: float | None = None,
        time_unit: TimeUnit | None = None,
    ) -> RampRateSetting:
        """Blocking max-ramp-rate query/set — see :meth:`FlowController.ramp_rate`."""
        return self._portal.call(self._controller_dev.ramp_rate, max_ramp, time_unit)

    def deadband_limit(
        self,
        deadband: float | None = None,
        *,
        save: bool | None = None,
    ) -> DeadbandSetting:
        """Blocking deadband-limit query/set — see :meth:`FlowController.deadband_limit`."""
        return self._portal.call(self._controller_dev.deadband_limit, deadband, save=save)

    # ------------------------------------------------------------------ auto-tare

    def auto_tare(
        self,
        enable: bool | None = None,
        delay_s: float | None = None,
    ) -> AutoTareState:
        """Blocking :meth:`FlowController.auto_tare` / :meth:`PressureController.auto_tare`."""
        return self._portal.call(self._controller_dev.auto_tare, enable, delay_s)

    @property
    def _controller_dev(self) -> FlowController | PressureController:
        """Typed view on the wrapped async controller — shared cast helper."""
        return cast("FlowController | PressureController", self._dev)


class SyncFlowController(SyncFlowMeter, _SyncControllerMixin):
    """Flow-controller facade — adds the shared controller surface."""


class SyncPressureMeter(SyncDevice):
    """Pressure-meter tag — empty pass-through, mirrors :class:`PressureMeter`."""


class SyncPressureController(SyncPressureMeter, _SyncControllerMixin):
    """Pressure-controller facade — inherits the shared controller surface."""


class SyncStreamingSession:
    """Blocking view over :class:`StreamingSession`.

    Wraps the async streaming context so sync callers see a plain
    ``with`` / ``for`` loop::

        with sync_dev.stream(rate_ms=50) as stream:
            for frame in stream:
                process(frame)

    Enter/exit and ``next()`` are routed through the device's
    :class:`SyncPortal`; the portal threads one coroutine at a time, so
    the underlying producer task keeps running in the background while
    the sync consumer polls for frames.
    """

    def __init__(
        self,
        async_stream: StreamingSession,
        portal: SyncPortal,
    ) -> None:
        self._async = async_stream
        self._portal = portal
        self._cm: AbstractContextManager[StreamingSession] | None = None

    def __enter__(self) -> Self:
        """Enter streaming mode on the async side.

        Uses :meth:`SyncPortal.wrap_async_context_manager` rather than
        routing ``__aenter__``/``__aexit__`` through ``portal.call``:
        ``portal.call`` wraps each call in a fresh ``CancelScope``,
        which conflicts with :meth:`StreamingSession.__aenter__`
        entering a long-lived task group that outlives the entry call.
        Hardware validation (2026-04-17) surfaced the resulting
        "cancel scope that isn't the current task's" ``RuntimeError``
        on real streaming hardware; ``wrap_async_context_manager``
        lets anyio manage the portal-side scope for the whole CM
        lifetime instead.

        Re-enters are rejected the same way as
        :meth:`StreamingSession.__aenter__` — one streaming context
        per instance.
        """
        self._cm = self._portal.wrap_async_context_manager(self._async)
        self._cm.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit streaming mode — always sends stop-stream via the portal."""
        if self._cm is None:
            return
        try:
            self._cm.__exit__(exc_type, exc, tb)
        finally:
            self._cm = None

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> DataFrame:
        """Block until the next frame, or :class:`StopIteration` on close."""
        try:
            return self._portal.call(self._async.__anext__)
        except StopAsyncIteration:
            raise StopIteration from None

    @property
    def dropped_frames(self) -> int:
        """Mirror of :attr:`StreamingSession.dropped_frames`."""
        return self._async.dropped_frames


def wrap_device(async_device: Device, portal: SyncPortal) -> SyncDevice:
    """Pick the correct :class:`SyncDevice` subclass for ``async_device``.

    Order is most-specific first — :class:`FlowController` is also a
    :class:`FlowMeter`, so the check must precede the meter branch.

    Package-private: consumed by :mod:`alicatlib.sync.manager` and
    :class:`Alicat`. Not part of the public API.
    """
    if isinstance(async_device, FlowController):
        return SyncFlowController(async_device, portal)
    if isinstance(async_device, PressureController):
        return SyncPressureController(async_device, portal)
    if isinstance(async_device, FlowMeter):
        return SyncFlowMeter(async_device, portal)
    if isinstance(async_device, PressureMeter):
        return SyncPressureMeter(async_device, portal)
    return SyncDevice(async_device, portal)


def unwrap_sync_device[T](source: T | SyncDevice) -> T | Device:
    """Return the async :class:`Device` inside ``source`` if it is wrapped.

    Package-private: used by :meth:`SyncAlicatManager.add` so callers
    can hand in a previously-wrapped :class:`SyncDevice`.
    """
    if isinstance(source, SyncDevice):
        return source._dev  # pyright: ignore[reportPrivateUsage]
    return source


class Alicat:
    """Namespace for the sync device entry point.

    Use :meth:`Alicat.open` as a context manager:

    >>> from alicatlib.sync import Alicat
    >>> with Alicat.open("/dev/ttyUSB0") as dev:  # doctest: +SKIP
    ...     print(dev.poll())
    """

    @staticmethod
    @contextmanager
    def open(
        port: str | Transport | AlicatProtocolClient,
        *,
        unit_id: str = "A",
        serial: SerialSettings | None = None,
        timeout: float = 0.5,
        recover_from_stream: bool = True,
        model_hint: str | None = None,
        assume_capabilities: Capability = Capability.NONE,
        assume_media: Medium | None = None,
        portal: SyncPortal | None = None,
    ) -> Iterator[SyncDevice]:
        """Open a sync :class:`SyncDevice` scoped to a ``with`` block.

        Mirrors :func:`alicatlib.devices.factory.open_device` parameter
        for parameter. The returned sync CM drives the async factory
        through a :class:`SyncPortal`; the portal is created per-call
        unless a pre-existing one is passed in via ``portal=``.

        Passing a :class:`Transport` or
        :class:`AlicatProtocolClient` is advanced: the caller is
        responsible for ensuring the object was constructed inside the
        portal's event loop (or is loop-agnostic). The common case —
        passing a ``str`` port path — creates the transport inside the
        portal and avoids that concern.
        """
        with ExitStack() as stack:
            active_portal = portal if portal is not None else stack.enter_context(SyncPortal())
            async_cm = open_device(
                port,
                unit_id=unit_id,
                serial=serial,
                timeout=timeout,
                recover_from_stream=recover_from_stream,
                model_hint=model_hint,
                assume_capabilities=assume_capabilities,
                assume_media=assume_media,
            )
            async_device = stack.enter_context(active_portal.wrap_async_context_manager(async_cm))
            yield wrap_device(async_device, active_portal)
