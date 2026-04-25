"""Parity tests: every async method has a matching sync wrapper.

This test is table-driven — each async method is paired with its sync
wrapper and the pair is compared with :func:`inspect.signature`. If
someone adds a new async method without a sync counterpart, or the
wrapper drifts in argument order / kind / default, the parity test
fails loudly.

What's checked:

* Every async parameter is present on the sync wrapper.
* Parameter kinds match (positional-only / positional-or-keyword /
  keyword-only). Mixing kinds changes the call shape and is a drift.
* Default values match.
* The sync wrapper may declare *extra* parameters beyond the async
  method — portal-sharing knobs land that way and are expected.

What's deliberately NOT checked:

* Return-type annotations — ``Awaitable[T]`` vs ``T``, async CMs vs
  sync CMs, ``AsyncIterator[T]`` vs ``Iterator[T]`` are the defining
  difference between the layers.
* Parameter *type* annotations — async / sync sometimes needs to widen
  (e.g. ``SyncAlicatManager.add`` also accepts a :class:`SyncDevice`
  source). Checking argument *shape* is what catches drift; annotation
  equality adds false-positive churn.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest

from alicatlib import manager as async_manager
from alicatlib.devices import (
    discovery as async_discovery,
)
from alicatlib.devices import (
    factory as async_factory,
)
from alicatlib.devices.base import Device
from alicatlib.devices.flow_controller import FlowController
from alicatlib.devices.pressure_controller import PressureController
from alicatlib.sinks import base as async_sinks_base
from alicatlib.sinks import csv as async_csv_sink
from alicatlib.sinks import jsonl as async_jsonl_sink
from alicatlib.sinks import memory as async_memory_sink
from alicatlib.sinks import parquet as async_parquet_sink
from alicatlib.sinks import postgres as async_postgres_sink
from alicatlib.sinks import sqlite as async_sqlite_sink
from alicatlib.streaming import recorder as async_recorder
from alicatlib.sync import (
    Alicat,
    SyncAlicatManager,
    SyncCsvSink,
    SyncDevice,
    SyncFlowController,
    SyncInMemorySink,
    SyncJsonlSink,
    SyncParquetSink,
    SyncPostgresSink,
    SyncPressureController,
    SyncSqliteSink,
)
from alicatlib.sync import (
    discovery as sync_discovery,
)
from alicatlib.sync import (
    recording as sync_recording,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Parameters / pairs the sync wrappers are allowed to add on top.
# ---------------------------------------------------------------------------

# Extra params every portal-owning sync wrapper adds — sharing the
# event loop with other sync facades is opt-in.
_PORTAL_KNOBS: frozenset[str] = frozenset({"portal"})


def _strip_self(sig: inspect.Signature) -> list[inspect.Parameter]:
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params = params[1:]
    return params


def _assert_parity(
    async_obj: Callable[..., object],
    sync_obj: Callable[..., object],
    *,
    allow_extra: frozenset[str] = _PORTAL_KNOBS,
    label: str,
) -> None:
    """Fail if ``sync_obj``'s signature doesn't cover ``async_obj``'s."""
    async_sig = inspect.signature(async_obj)
    sync_sig = inspect.signature(sync_obj)

    async_params = _strip_self(async_sig)
    sync_params = _strip_self(sync_sig)

    sync_by_name = {p.name: p for p in sync_params}

    for async_p in async_params:
        assert async_p.name in sync_by_name, (
            f"{label}: sync wrapper is missing parameter {async_p.name!r}"
        )
        sync_p = sync_by_name[async_p.name]
        assert async_p.kind == sync_p.kind, (
            f"{label}: parameter {async_p.name!r} kind mismatch "
            f"(async={async_p.kind.name}, sync={sync_p.kind.name})"
        )
        assert async_p.default == sync_p.default, (
            f"{label}: parameter {async_p.name!r} default mismatch "
            f"(async={async_p.default!r}, sync={sync_p.default!r})"
        )

    # Anything the sync wrapper adds on its own must be an allow-listed
    # knob (portal overrides, ...). This catches accidental extra
    # sync-only params that should have gone into the async side first.
    async_names = {p.name for p in async_params}
    extras = {p.name for p in sync_params} - async_names
    unexpected = extras - allow_extra
    assert not unexpected, (
        f"{label}: sync wrapper has unexpected extra parameters {sorted(unexpected)}"
    )


# ---------------------------------------------------------------------------
# Method parity — Device / FlowController
# ---------------------------------------------------------------------------


_DEVICE_PAIRS: list[tuple[str, Callable[..., object], Callable[..., object]]] = [
    ("Device.poll", Device.poll, SyncDevice.poll),
    ("Device.request", Device.request, SyncDevice.request),
    ("Device.gas", Device.gas, SyncDevice.gas),
    ("Device.gas_list", Device.gas_list, SyncDevice.gas_list),
    ("Device.engineering_units", Device.engineering_units, SyncDevice.engineering_units),
    ("Device.full_scale", Device.full_scale, SyncDevice.full_scale),
    ("Device.tare_flow", Device.tare_flow, SyncDevice.tare_flow),
    (
        "Device.tare_gauge_pressure",
        Device.tare_gauge_pressure,
        SyncDevice.tare_gauge_pressure,
    ),
    (
        "Device.tare_absolute_pressure",
        Device.tare_absolute_pressure,
        SyncDevice.tare_absolute_pressure,
    ),
    # Non-destructive all-device specialty.
    ("Device.zero_band", Device.zero_band, SyncDevice.zero_band),
    ("Device.average_timing", Device.average_timing, SyncDevice.average_timing),
    ("Device.stp_ntp_pressure", Device.stp_ntp_pressure, SyncDevice.stp_ntp_pressure),
    (
        "Device.stp_ntp_temperature",
        Device.stp_ntp_temperature,
        SyncDevice.stp_ntp_temperature,
    ),
    (
        "Device.analog_output_source",
        Device.analog_output_source,
        SyncDevice.analog_output_source,
    ),
    ("Device.blink_display", Device.blink_display, SyncDevice.blink_display),
    ("Device.lock_display", Device.lock_display, SyncDevice.lock_display),
    ("Device.unlock_display", Device.unlock_display, SyncDevice.unlock_display),
    ("Device.user_data", Device.user_data, SyncDevice.user_data),
    ("Device.power_up_tare", Device.power_up_tare, SyncDevice.power_up_tare),
    # Totalizer.
    ("Device.totalizer_config", Device.totalizer_config, SyncDevice.totalizer_config),
    ("Device.totalizer_reset", Device.totalizer_reset, SyncDevice.totalizer_reset),
    (
        "Device.totalizer_reset_peak",
        Device.totalizer_reset_peak,
        SyncDevice.totalizer_reset_peak,
    ),
    ("Device.totalizer_save", Device.totalizer_save, SyncDevice.totalizer_save),
    ("Device.stream", Device.stream, SyncDevice.stream),
    ("Device.execute", Device.execute, SyncDevice.execute),
    ("Device.close", Device.close, SyncDevice.close),
]

_FLOW_CONTROLLER_PAIRS: list[tuple[str, Callable[..., object], Callable[..., object]]] = [
    ("FlowController.setpoint", FlowController.setpoint, SyncFlowController.setpoint),
    (
        "FlowController.setpoint_source",
        FlowController.setpoint_source,
        SyncFlowController.setpoint_source,
    ),
    (
        "FlowController.loop_control_variable",
        FlowController.loop_control_variable,
        SyncFlowController.loop_control_variable,
    ),
    ("FlowController.hold_valves", FlowController.hold_valves, SyncFlowController.hold_valves),
    (
        "FlowController.hold_valves_closed",
        FlowController.hold_valves_closed,
        SyncFlowController.hold_valves_closed,
    ),
    (
        "FlowController.cancel_valve_hold",
        FlowController.cancel_valve_hold,
        SyncFlowController.cancel_valve_hold,
    ),
    ("FlowController.valve_drive", FlowController.valve_drive, SyncFlowController.valve_drive),
    ("FlowController.ramp_rate", FlowController.ramp_rate, SyncFlowController.ramp_rate),
    (
        "FlowController.deadband_limit",
        FlowController.deadband_limit,
        SyncFlowController.deadband_limit,
    ),
    ("FlowController.auto_tare", FlowController.auto_tare, SyncFlowController.auto_tare),
]


# ``PressureController`` shares the controller surface with
# ``FlowController`` via ``_ControllerMixin``. The sync wrappers share
# their impl via ``_SyncControllerMixin``. Parity pins the sync side
# exposes the same three methods with matching signatures on the
# pressure-controller class.
_PRESSURE_CONTROLLER_PAIRS: list[tuple[str, Callable[..., object], Callable[..., object]]] = [
    (
        "PressureController.setpoint",
        PressureController.setpoint,
        SyncPressureController.setpoint,
    ),
    (
        "PressureController.setpoint_source",
        PressureController.setpoint_source,
        SyncPressureController.setpoint_source,
    ),
    (
        "PressureController.loop_control_variable",
        PressureController.loop_control_variable,
        SyncPressureController.loop_control_variable,
    ),
    (
        "PressureController.hold_valves",
        PressureController.hold_valves,
        SyncPressureController.hold_valves,
    ),
    (
        "PressureController.hold_valves_closed",
        PressureController.hold_valves_closed,
        SyncPressureController.hold_valves_closed,
    ),
    (
        "PressureController.cancel_valve_hold",
        PressureController.cancel_valve_hold,
        SyncPressureController.cancel_valve_hold,
    ),
    (
        "PressureController.valve_drive",
        PressureController.valve_drive,
        SyncPressureController.valve_drive,
    ),
    (
        "PressureController.ramp_rate",
        PressureController.ramp_rate,
        SyncPressureController.ramp_rate,
    ),
    (
        "PressureController.deadband_limit",
        PressureController.deadband_limit,
        SyncPressureController.deadband_limit,
    ),
    (
        "PressureController.auto_tare",
        PressureController.auto_tare,
        SyncPressureController.auto_tare,
    ),
]


@pytest.mark.parametrize(("label", "async_m", "sync_m"), _DEVICE_PAIRS)
def test_device_method_parity(
    label: str,
    async_m: Callable[..., object],
    sync_m: Callable[..., object],
) -> None:
    _assert_parity(async_m, sync_m, label=label, allow_extra=frozenset())


@pytest.mark.parametrize(("label", "async_m", "sync_m"), _FLOW_CONTROLLER_PAIRS)
def test_flow_controller_method_parity(
    label: str,
    async_m: Callable[..., object],
    sync_m: Callable[..., object],
) -> None:
    _assert_parity(async_m, sync_m, label=label, allow_extra=frozenset())


@pytest.mark.parametrize(("label", "async_m", "sync_m"), _PRESSURE_CONTROLLER_PAIRS)
def test_pressure_controller_method_parity(
    label: str,
    async_m: Callable[..., object],
    sync_m: Callable[..., object],
) -> None:
    _assert_parity(async_m, sync_m, label=label, allow_extra=frozenset())


# ---------------------------------------------------------------------------
# Method parity — AlicatManager
# ---------------------------------------------------------------------------


_MANAGER_PAIRS: list[tuple[str, Callable[..., object], Callable[..., object]]] = [
    (
        "AlicatManager.__init__",
        async_manager.AlicatManager.__init__,
        SyncAlicatManager.__init__,
    ),
    ("AlicatManager.add", async_manager.AlicatManager.add, SyncAlicatManager.add),
    (
        "AlicatManager.remove",
        async_manager.AlicatManager.remove,
        SyncAlicatManager.remove,
    ),
    ("AlicatManager.get", async_manager.AlicatManager.get, SyncAlicatManager.get),
    ("AlicatManager.close", async_manager.AlicatManager.close, SyncAlicatManager.close),
    ("AlicatManager.poll", async_manager.AlicatManager.poll, SyncAlicatManager.poll),
    (
        "AlicatManager.request",
        async_manager.AlicatManager.request,
        SyncAlicatManager.request,
    ),
    (
        "AlicatManager.execute",
        async_manager.AlicatManager.execute,
        SyncAlicatManager.execute,
    ),
]


@pytest.mark.parametrize(("label", "async_m", "sync_m"), _MANAGER_PAIRS)
def test_manager_method_parity(
    label: str,
    async_m: Callable[..., object],
    sync_m: Callable[..., object],
) -> None:
    # SyncAlicatManager.__init__ and .add add the portal knob; everything
    # else inherits the baseline ``portal`` allow-list.
    _assert_parity(async_m, sync_m, label=label)


# ---------------------------------------------------------------------------
# Top-level function parity — factory / discovery / recording
# ---------------------------------------------------------------------------


_FUNCTION_PAIRS: list[tuple[str, Callable[..., object], Callable[..., object]]] = [
    ("factory.open_device", async_factory.open_device, Alicat.open),
    (
        "discovery.list_serial_ports",
        async_discovery.list_serial_ports,
        sync_discovery.list_serial_ports,
    ),
    ("discovery.probe", async_discovery.probe, sync_discovery.probe),
    (
        "discovery.find_devices",
        async_discovery.find_devices,
        sync_discovery.find_devices,
    ),
    ("recorder.record", async_recorder.record, sync_recording.record),
    ("sinks.pipe", async_sinks_base.pipe, sync_recording.pipe),
]


@pytest.mark.parametrize(("label", "async_fn", "sync_fn"), _FUNCTION_PAIRS)
def test_function_parity(
    label: str,
    async_fn: Callable[..., object],
    sync_fn: Callable[..., object],
) -> None:
    _assert_parity(async_fn, sync_fn, label=label)


# ---------------------------------------------------------------------------
# Sink constructor parity
# ---------------------------------------------------------------------------


_SINK_PAIRS: list[tuple[str, Callable[..., object], Callable[..., object]]] = [
    (
        "InMemorySink.__init__",
        async_memory_sink.InMemorySink.__init__,
        SyncInMemorySink.__init__,
    ),
    ("CsvSink.__init__", async_csv_sink.CsvSink.__init__, SyncCsvSink.__init__),
    (
        "JsonlSink.__init__",
        async_jsonl_sink.JsonlSink.__init__,
        SyncJsonlSink.__init__,
    ),
    (
        "SqliteSink.__init__",
        async_sqlite_sink.SqliteSink.__init__,
        SyncSqliteSink.__init__,
    ),
    (
        "ParquetSink.__init__",
        async_parquet_sink.ParquetSink.__init__,
        SyncParquetSink.__init__,
    ),
    (
        "PostgresSink.__init__",
        async_postgres_sink.PostgresSink.__init__,
        SyncPostgresSink.__init__,
    ),
]


@pytest.mark.parametrize(("label", "async_ctor", "sync_ctor"), _SINK_PAIRS)
def test_sink_constructor_parity(
    label: str,
    async_ctor: Callable[..., object],
    sync_ctor: Callable[..., object],
) -> None:
    _assert_parity(async_ctor, sync_ctor, label=label)


# ---------------------------------------------------------------------------
# Coverage — every coroutine method on the async classes must appear in
# one of the paired tables above. Guards against "someone added a method
# to Device but forgot to add a SyncDevice wrapper."
# ---------------------------------------------------------------------------


def _coroutine_method_names(cls: type, *, exclude: frozenset[str]) -> set[str]:
    """Return every name on ``cls`` whose value is an async function."""
    names: set[str] = set()
    for name in dir(cls):
        if name in exclude or name.startswith("_"):
            continue
        attr = inspect.getattr_static(cls, name, None)
        if inspect.iscoroutinefunction(attr):
            names.add(name)
    return names


# Methods exposed on the async side that intentionally don't have a
# direct sync wrapper. Keep this short and document each exception.
_DEVICE_EXEMPT: frozenset[str] = frozenset()
_FLOW_CONTROLLER_EXEMPT: frozenset[str] = frozenset()
_PRESSURE_CONTROLLER_EXEMPT: frozenset[str] = frozenset()
_MANAGER_EXEMPT: frozenset[str] = frozenset()


def test_device_coroutine_coverage() -> None:
    covered = {label.split(".", 1)[1] for label, *_ in _DEVICE_PAIRS}
    async_names = _coroutine_method_names(Device, exclude=_DEVICE_EXEMPT)
    missing = async_names - covered
    assert not missing, f"Device has coroutine methods without sync wrappers: {sorted(missing)}"


def _own_coroutine_names(cls: type, *, bases: tuple[type, ...]) -> set[str]:
    """Return coroutine-method names declared on ``cls`` or any of ``bases``.

    Used by controller-coverage tests: the shared controller surface
    (setpoint / setpoint_source / loop_control_variable) lives on
    :class:`_ControllerMixin`, not on the concrete ``FlowController`` /
    ``PressureController`` classes themselves. Walking just
    ``vars(cls)`` would miss it and the coverage check would quietly
    pass when one of the concrete classes is missing a sync wrapper.
    """
    names: set[str] = set()
    for scope in (cls, *bases):
        for name, attr in vars(scope).items():
            if inspect.iscoroutinefunction(attr) and not name.startswith("_"):
                names.add(name)
    return names


def test_flow_controller_coroutine_coverage() -> None:
    # Walks FlowController + its mixin parents explicitly so the
    # coverage sees ``setpoint`` / ``setpoint_source`` /
    # ``loop_control_variable`` (which live on the shared mixin).
    # Inherited-from-Device methods are asserted by
    # ``test_device_coroutine_coverage`` and filtered out here.
    from alicatlib.devices._controller import (
        _ControllerMixin,  # pyright: ignore[reportPrivateUsage]
    )

    own = _own_coroutine_names(FlowController, bases=(_ControllerMixin,))
    covered = {label.split(".", 1)[1] for label, *_ in _FLOW_CONTROLLER_PAIRS}
    missing = own - covered - _FLOW_CONTROLLER_EXEMPT
    assert not missing, (
        f"FlowController has coroutine methods without sync wrappers: {sorted(missing)}"
    )


def test_pressure_controller_coroutine_coverage() -> None:
    """PressureController parity — mirror of the flow-controller check.

    Pressure-controller methods live on the shared
    :class:`_ControllerMixin`, same as the flow-controller ones; the
    two concrete classes share signatures by construction. This test
    fails loudly if someone adds an async method on the mixin and
    forgets the ``SyncPressureController`` wrapper.
    """
    from alicatlib.devices._controller import (
        _ControllerMixin,  # pyright: ignore[reportPrivateUsage]
    )

    own = _own_coroutine_names(PressureController, bases=(_ControllerMixin,))
    covered = {label.split(".", 1)[1] for label, *_ in _PRESSURE_CONTROLLER_PAIRS}
    missing = own - covered - _PRESSURE_CONTROLLER_EXEMPT
    assert not missing, (
        f"PressureController has coroutine methods without sync wrappers: {sorted(missing)}"
    )


def test_manager_coroutine_coverage() -> None:
    covered = {label.split(".", 1)[1] for label, *_ in _MANAGER_PAIRS}
    async_names = _coroutine_method_names(async_manager.AlicatManager, exclude=_MANAGER_EXEMPT)
    # AlicatManager.__init__ and .get are sync already — only check
    # that coroutine methods are covered.
    missing = async_names - covered
    assert not missing, (
        f"AlicatManager has coroutine methods without sync wrappers: {sorted(missing)}"
    )


def test_sync_sample_sink_protocol_is_importable_at_runtime() -> None:
    """Regression: ``SyncSampleSink`` is in ``__all__`` so it must be a real
    runtime symbol, not a ``TYPE_CHECKING``-only one.
    """
    from alicatlib.sync.sinks import SyncSampleSink

    assert isinstance(SyncCsvSink("/tmp/_unused.csv"), SyncSampleSink)
