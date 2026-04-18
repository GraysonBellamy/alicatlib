# Sync quickstart

The async core is canonical (see [Async quickstart](quickstart-async.md)).
The sync facade — [`alicatlib.sync`](../src/alicatlib/sync/__init__.py) —
wraps it through a per-context `BlockingPortal` for scripts, notebooks,
and REPL sessions. Every async method has a sync parity; parity tests
fail CI if a new async coroutine lands without its sync wrapper. See
[Design](design.md) §5.16.

## Single device

```python
from alicatlib.sync import Alicat

with Alicat.open("/dev/ttyUSB0") as dev:
    frame = dev.poll()
    print(frame.get_float("Mass_Flow"))
    dev.setpoint(50.0, "SCCM")
```

[`Alicat.open`](../src/alicatlib/sync/device.py) returns whichever
facade subclass the factory identified —
[`SyncFlowController`](../src/alicatlib/sync/device.py),
[`SyncFlowMeter`](../src/alicatlib/sync/device.py),
[`SyncPressureController`](../src/alicatlib/sync/device.py), or
[`SyncPressureMeter`](../src/alicatlib/sync/device.py). Same
identification pipeline as the async `open_device` (see
[devices.md](devices.md)); same escape hatches (`model_hint`,
`assume_media`, `assume_capabilities`).

## Multi-device acquisition

```python
from alicatlib.sync import (
    SyncAlicatManager,
    SyncCsvSink,
    pipe,
    record,
)

with SyncAlicatManager() as mgr:
    mgr.add("fuel", "/dev/ttyUSB0")
    mgr.add("air",  "/dev/ttyUSB1")
    with (
        record(mgr, rate_hz=10, duration=60) as stream,
        SyncCsvSink("run.csv") as sink,
    ):
        summary = pipe(stream, sink)
    print(summary.samples_emitted, "samples written")
```

[`SyncAlicatManager`](../src/alicatlib/sync/manager.py#L45) is a plain
context manager that owns the shared portal and the wrapped async
[`AlicatManager`](../src/alicatlib/manager.py). `mgr.add(...)` takes the
same source shapes as the async side — port string, `SyncDevice`,
`Transport`, or `AlicatProtocolClient`. Port canonicalisation and
ref-counted port sharing are the manager's job, not the caller's.

[`record()`](../src/alicatlib/sync/recording.py#L97) and
[`pipe()`](../src/alicatlib/sync/recording.py#L134) mirror their async
counterparts; the yielded `stream` is a blocking iterator of per-tick
`Mapping[device_name, Sample]` batches. Drift-free absolute-target
scheduling works the same way as the async recorder — see
[logging.md](logging.md).

## Streaming

```python
with Alicat.open("/dev/ttyUSB0") as dev:
    with dev.stream(rate_ms=50) as stream:
        for frame in stream:
            print(frame.get_float("Mass_Flow"))
```

[`SyncDevice.stream(...)`](../src/alicatlib/sync/device.py#L342) returns
a [`SyncStreamingSession`](../src/alicatlib/sync/device.py#L506) — both
a sync CM and a sync iterator. Same semantics as the async variant
(`is_streaming` latch, overflow policy, stop-stream on body exceptions);
see [streaming.md §Sync streaming](streaming.md#sync-streaming) for the
subtle `wrap_async_context_manager` routing that makes it work on real
hardware.

## Discovery

```python
from alicatlib.sync import find_devices, list_serial_ports

print(list_serial_ports())
for result in find_devices(unit_ids=("A", "B"), timeout=0.2):
    if result.ok:
        print(result.info.model, result.port, result.baudrate)
```

[`find_devices`](../src/alicatlib/sync/discovery.py) runs the same
cross-product sweep as the async side and returns
[`DiscoveryResult`](../src/alicatlib/devices/discovery.py#L71) objects —
one per `(port, unit_id, baudrate)` combination tried. Individual
failures never raise; filter on `result.ok`. See
[troubleshooting.md §Discovering devices on a bus](troubleshooting.md#discovering-devices-on-a-bus).

## Using a shared portal

The throwaway-portal default is right for one-off scripts. For code
that holds both a manager and standalone sinks, share a portal so
they run on the same event loop:

```python
from alicatlib.sync import SyncAlicatManager, SyncPortal, SyncSqliteSink, pipe, record

with SyncPortal() as portal:
    with SyncAlicatManager(portal=portal) as mgr:
        mgr.add("fuel", "/dev/ttyUSB0")
        with (
            record(mgr, rate_hz=10, duration=60, portal=portal) as stream,
            SyncSqliteSink("run.db", portal=portal) as sink,
        ):
            pipe(stream, sink, portal=portal)
```

Mixing portals works but costs an extra event-loop hop per method
call. Share when performance matters; don't bother for one-off runs.

## See also

- [Installation](installation.md) — core install and extras.
- [Async quickstart](quickstart-async.md) — the canonical surface.
- [Devices](devices.md) — prefix matrix, identification, escape hatches.
- [Logging and acquisition](logging.md) — recorder, sinks, `pipe()`.
- [Safety](safety.md) — destructive commands and the V1_V7 setpoint-source caveat.
