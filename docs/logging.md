# Logging and acquisition

This page covers everything downstream of a command's reply: the
recorder, the sink Protocol, the first-party sinks, and the
structured log events the library emits on top of the standard
`logging` module.

See the [Design doc](design.md) §5.14 / §5.15 / §5.19 for the
authoritative architecture.

## Recorder

`alicatlib.streaming.record` drives one or more devices at an
absolute-target cadence and publishes each tick as a
`Mapping[device_name, Sample]` batch through an `anyio`
memory-object receive stream.

```python
from alicatlib import AlicatManager
from alicatlib.streaming import record

async with AlicatManager() as mgr:
    await mgr.add("fuel", "/dev/ttyUSB0")
    await mgr.add("air", "/dev/ttyUSB1")
    async with record(mgr, rate_hz=10, duration=60) as stream:
        async for batch in stream:
            print(batch["fuel"].frame.values["Mass_Flow"])
```

Key properties (design §5.14):

- **Absolute-target scheduling.** Target times are computed from
  `anyio.current_time()` at entry; drift never accumulates. An
  overrun skips missed slots and increments `samples_late` rather
  than queuing up ticks.
- **Structured concurrency.** The producer task is strictly nested
  inside the context manager body. Exiting the `async with` (via
  `break`, exception, or natural stream end) cancels and joins the
  producer before `record()` returns.
- **Wall-clock provenance.** Each `Sample` carries
  `requested_at` / `received_at` / `midpoint_at` (all UTC
  `datetime`) plus a `monotonic_ns` for drift analysis and a
  `latency_s` precomputed for convenience.

### Backpressure

`record(..., overflow=OverflowPolicy.BLOCK, buffer_size=64)` sets
the receive-stream capacity and picks how the recorder handles a
slow consumer:

| Policy | Behaviour |
| --- | --- |
| `BLOCK` (default) | Producer awaits queue space. `samples_late` accrues once the consumer catches up. |
| `DROP_NEWEST` | The new batch is discarded; `samples_late` increments. A one-shot WARN fires on the `alicatlib.streaming` logger. |
| `DROP_OLDEST` | Currently raises `NotImplementedError` at call site — use `BLOCK` or `DROP_NEWEST` until the proper eviction lands alongside the first sink benchmark. |

### `AcquisitionSummary`

`record()` logs an `alicatlib.streaming` INFO event on CM exit with
the samples-emitted / samples-late / max-drift counters. The same
shape is returned by `pipe()` below, from the sink side.

## Sinks

Every sink satisfies `alicatlib.sinks.SampleSink`:

```python
class SampleSink(Protocol):
    async def open(self) -> None: ...
    async def write_many(self, samples: Sequence[Sample]) -> None: ...
    async def close(self) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc) -> None: ...
```

First-party sinks ship in the core install:

| Sink | Dependencies | Schema lock |
| --- | --- | --- |
| `InMemorySink` | stdlib | n/a — test-only; collects samples in a list |
| `CsvSink(path)` | stdlib `csv` | locked at first batch; unknown later columns dropped with WARN |
| `JsonlSink(path)` | stdlib `json` | none — one JSON object per line, heterogeneous shapes allowed |

Parquet and Postgres sinks ship behind the `alicatlib[parquet]` and
`alicatlib[postgres]` extras.

### Stable row layout (`sample_to_row`)

Both `CsvSink` and `JsonlSink` flatten each `Sample` into the same
row shape via `sample_to_row`:

| Column | Source |
| --- | --- |
| `device` | `Sample.device` — manager-assigned name |
| `unit_id` | `Sample.unit_id` — bus letter |
| `requested_at` / `received_at` / `midpoint_at` | `Sample.*` wall-clock ISO 8601 |
| `latency_s` | `Sample.latency_s` |
| *frame fields* | `DataFrame.as_dict()` minus its own `received_at` (sample-level wins) |
| `status` | Comma-joined sorted status codes from the data frame |

The frame's own `received_at` is dropped so every row's
`received_at` means the same thing — recorder-observed reply time.

## `pipe()`

`pipe(stream, sink, *, batch_size=64, flush_interval=1.0)` drains
the recorder's stream into a sink, flushing on either threshold:

```python
from alicatlib.sinks import CsvSink, pipe
from alicatlib.streaming import record

async with record(mgr, rate_hz=10, duration=60) as stream, CsvSink("run.csv") as sink:
    summary = await pipe(stream, sink)
    print(summary.samples_emitted, "samples written")
```

Notes:

- Per-device failures under `ErrorPolicy.RETURN` are dropped from
  the batch with a `recorder.device_error` WARN; healthy devices
  still emit.
- Returned `AcquisitionSummary.samples_late` /
  `max_drift_ms` stay zero on the sink side — those are
  recorder-layer concepts. Check the `alicatlib.streaming` logger
  for the recorder's own summary event.

## Logger tree

The library never configures root handlers. Users wire handlers
onto the tree as needed:

```python
import logging

logging.getLogger("alicatlib").setLevel(logging.INFO)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
```

Emitted by layer:

| Logger | Level | Events |
| --- | --- | --- |
| `alicatlib.transport` | DEBUG | Transport open/close plumbing. |
| `alicatlib.protocol` | DEBUG | Client-lock entry, multiline idle fallbacks. |
| `alicatlib.session` | INFO | Setpoint / LSS / LV set-events; capability-probe outcomes; tare preconditions (caller-responsibility notice). |
| `alicatlib.session` | WARN | EEPROM-wear rate exceeded (`AlicatConfig.save_rate_warn_per_min`). |
| `alicatlib.session` | INFO | `manager.add`, `manager.remove` (from the manager). |
| `alicatlib.manager` | WARN | Best-effort shutdown failures on close. |
| `alicatlib.streaming` | INFO | Recorder start / stop with `AcquisitionSummary`. |
| `alicatlib.streaming` | WARN | `recorder.drop_newest`, per-device polling errors. |
| `alicatlib.sinks.<name>` | INFO | `sinks.pipe_done` summary; `sinks.csv.unknown_column` drops (WARN). |

### Set-event INFO schema

Writes that change device state emit one structured INFO event
pre-I/O so operators can trace "what did the library do and why":

| Event | Logger | `extra` fields |
| --- | --- | --- |
| `setpoint_change` | `alicatlib.session` | `unit_id`, `command` (`setpoint` / `setpoint_legacy`), `value`, `path` (`modern` / `legacy`) |
| `setpoint_source_change` | `alicatlib.session` | `unit_id`, `command`, `requested_mode` (`S`/`A`/`U`), `save` |
| `loop_control_variable_change` | `alicatlib.session` | `unit_id`, `command`, `requested_variable` (enum name when available) |
| `probe_capabilities.result` | `alicatlib.session` | `unit_id`, `firmware`, `model`, `resolved`, `present`, `outcomes` (per-flag `"present"` / `"absent"` / `"timeout"` / `"rejected"` / `"parse_error"`) |
| `probe_capabilities.gp_skip` | `alicatlib.session` | `unit_id`, `firmware`, `reason` |

Query-form calls do **not** emit these events — the design
intent is "log every *write*", not every read.

### Safety: never in logs

- Credentials. `PostgresConfig.password` stays out of every log
  line, even at DEBUG.
- Payloads at INFO by default. Raw wire bytes go through
  DEBUG-only channels so an INFO-level deployment doesn't leak
  experimental data into ops logs.
