# Troubleshooting

A checklist for the common failure modes when opening, polling, or
streaming from Alicat hardware. The library raises typed exceptions
for each layer, so most troubleshooting is "read the exception type,
then check the bullet below."

Source: [errors.py](../src/alicatlib/errors.py),
[config.py](../src/alicatlib/config.py),
[devices/discovery.py](../src/alicatlib/devices/discovery.py).

## Finding the port

```python
from alicatlib import list_serial_ports
list_serial_ports()
# ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/serial/by-id/usb-FTDI..."]
```

[`list_serial_ports()`](../src/alicatlib/__init__.py) wraps
`anyserial.list_serial_ports()`. On Linux, prefer the stable
`/dev/serial/by-id/usb-FTDI_...` path over `/dev/ttyUSB0` — the latter
renumbers on reboot if multiple converters are plugged in. The manager
canonicalises both forms to the same port identity (`os.path.realpath`
on POSIX, uppercased `\\.\` prefix strip on Windows), so a port passed
either way shares its protocol client.

If `list_serial_ports()` misses a device that's clearly plugged in,
install the pyserial-backed fallback:

```bash
pip install 'anyserial[discovery-pyserial]'
```

Native `anyserial` enumeration relies on platform-specific APIs; the
pyserial fallback is broader but heavier. Both return the same path
strings.

## Permissions (Linux / macOS)

On Linux the default `/dev/ttyUSB*` device is owned `root:dialout`
with mode `660`. If `open_device` raises
`AlicatConnectionError: [Errno 13] Permission denied: '/dev/ttyUSB0'`:

```bash
sudo usermod -aG dialout $USER
# log out and back in for the group to take effect
```

On some distributions the group is `uucp` (Arch) or `tty`. Check with
`ls -l /dev/ttyUSB0`.

On macOS the default device is `/dev/tty.usbserial-*` and is
world-accessible; no permission changes are needed.

## Discovering devices on a bus

A mixed fleet with unknown baud rates / unit ids:

```python
from alicatlib import find_devices, DEFAULT_DISCOVERY_BAUDRATES
results = await find_devices(
    ports=None,                         # None = all available ports
    unit_ids=("A", "B", "C"),
    baudrates=DEFAULT_DISCOVERY_BAUDRATES,  # (19200, 115200) by default
    timeout=0.2,
)
for r in results:
    if r.ok:
        print(r.info.model, r.info.firmware, r.port, r.baudrate, r.unit_id)
```

[`find_devices`](../src/alicatlib/devices/discovery.py) runs
[`probe`](../src/alicatlib/devices/discovery.py) over the cross-product
of `ports × unit_ids × baudrates`, bounded by a `CapacityLimiter`
(default 8 concurrent opens). Individual probe failures never raise
from `find_devices` — every combination produces a
[`DiscoveryResult`](../src/alicatlib/devices/discovery.py#L71), and
the caller filters on `result.ok`.

Default baudrates are `(19200, 115200)` — Alicat factory default plus
the most common alternative after `NCB`. Widen via the `baudrates`
kwarg when you have devices at other rates.

## Timeouts

`AlicatTimeoutError` means a transport-level I/O timeout expired.
Timeouts are distinct from empty successful responses — a missing
reply is never silently represented as nothing.

Tune defaults via [`AlicatConfig`](../src/alicatlib/config.py) or
per-call `timeout=` kwargs (available on every I/O boundary —
[transport](../src/alicatlib/transport/base.py),
[protocol client](../src/alicatlib/protocol/client.py),
[session](../src/alicatlib/devices/session.py),
[factory](../src/alicatlib/devices/factory.py),
[discovery](../src/alicatlib/devices/discovery.py),
[manager](../src/alicatlib/manager.py)):

| Setting | Default | When to bump |
| --- | --- | --- |
| `default_timeout_s` | 0.5 s | USB-to-RS485 converter with high buffering latency (PL2303, CH340), slow devices on busy RS-485 buses. |
| `multiline_timeout_s` | 1.0 s | `??M*` / `??D*` / gas list on slow devices — the table commands are paced at device speed across 5–20 lines. |
| `write_timeout_s` | 0.5 s | Writes can block on RS-485 hardware flow control, a hung device, or a TCP transport's send buffer. |

```python
from alicatlib import AlicatConfig, open_device
config = AlicatConfig(default_timeout_s=1.0)
async with await open_device("/dev/ttyUSB0", timeout=1.0) as dev:
    ...
```

Timeouts can also be set via environment variables — see
[`config_from_env`](../src/alicatlib/config.py). Recognised keys all
prefix with `ALICATLIB_`: `DEFAULT_TIMEOUT_S`, `MULTILINE_TIMEOUT_S`,
`WRITE_TIMEOUT_S`, `DEFAULT_BAUDRATE`, `DRAIN_BEFORE_WRITE`,
`SAVE_RATE_WARN_PER_MIN`, `EAGER_TASKS`.

### If p50 latency creeps near the timeout

Benchmark the single-line round-trip:

```bash
uv run python scripts/bench_query.py --n 1000
```

See [benchmarks.md](benchmarks.md) for the reference table. The
baseline is p50 ~4 ms on PL2303 / 8v17 / 115200. If you're at
400 ms p50, the timeout needs to go up before it starts swallowing
legitimate replies.

## Stale input on the bus

If a prior process crashed mid-command or left a device streaming,
the bus has stale bytes that confuse the next command.
[`open_device(..., recover_from_stream=True)`](../src/alicatlib/devices/factory.py#L988)
(default) handles the streaming case: the factory passively sniffs
for ~100 ms, and if bytes arrive it writes the stop-stream sequence
and drains before `VE` runs. See [streaming.md](streaming.md#stop-stream-and-recovery).

For non-streaming stale bytes, set
`AlicatConfig.drain_before_write=True`. The protocol client drains
any stale input before each command; this adds latency but
re-synchronises after a timeout or a partial reply.

```python
config = AlicatConfig(drain_before_write=True)
```

## Identification failures

### `AlicatConfigurationError: ... model_hint is required`

The device is GP-family or pre-8v28 numeric firmware, so `??M*`
isn't available. Supply `model_hint`:

```python
await open_device("/dev/ttyUSB0", model_hint="MC-100SCCM-D")
```

See [devices.md §Escape hatches](devices.md#escape-hatches).

### `AlicatMediumMismatchError`

A command's declared medium doesn't intersect the device's configured
medium. Typical cause: calling `.gas(...)` on a CODA device configured
for liquid, or `.fluid(...)` on a gas-only device. The error carries
`ErrorContext.device_media` and `ErrorContext.command_media` so you
can see the exact mismatch.

Remediation: if your device actually supports both media but is
configured for one, narrow via
`assume_media=Medium.GAS | Medium.LIQUID` at open time. The factory
**replaces** (not unions) the prefix-derived media when `assume_media`
is passed.

### `AlicatMissingHardwareError`

The device lacks a capability the command requires. Error message
names the missing `Capability` flag (e.g. `BAROMETER`,
`ANALOG_OUTPUT`, `DISPLAY`). Check `DeviceInfo.capabilities` to see
what the factory probed:

```python
async with await open_device(port) as dev:
    print(dev.info.capabilities)
    print(dev.info.probe_report)   # per-flag outcomes
```

If the probe returned `"timeout"` or `"rejected"` on a capability you
know the device has, opt in via `assume_capabilities=` — see
[devices.md §Escape hatches](devices.md#escape-hatches). This is
common for `TAREABLE_ABSOLUTE_PRESSURE` where no safe probe exists.

### `AlicatFirmwareError`

The firmware is outside the command's supported range. The error
carries `actual`, `required_min`, `required_max`, and
`required_families` so the remediation is obvious — usually "this
device is too old for this command". See
[commands.md](commands.md) for the per-command firmware gates.

## Unit-ID and baud changes

Changing unit id (`ADDR`) or baud (`NCB`) is destructive and leaves
the session explicitly broken on success. The next call on the stale
session raises `AlicatError` with an explanation; re-open with the new
settings:

```python
async with await open_device("/dev/ttyUSB0", unit_id="A") as dev:
    await dev.change_unit_id("B", confirm=True)
# old session is broken here — re-open on the new id
async with await open_device("/dev/ttyUSB0", unit_id="B") as dev:
    ...
```

This is intentional. Pretending the session still works would let a
command interleave with a half-committed reconciliation and produce
silent data corruption.

## Streaming-mode fast-fails

While a `StreamingSession` is active on a port, every
request/response command on any session sharing that client fails
fast with `AlicatStreamingModeError`. Typical cause: trying to
`poll()` from a second task while the first task holds a
`StreamingSession`.

One streamer per port is a hard invariant. Either fold the second
consumer into the streaming session's iterator, or use `record()`
instead of streaming (see [streaming.md §Streaming vs. record()](streaming.md#streaming-vs-record)).

## Sink errors

Sink failures are typed under `AlicatSinkError`:

| Error | Cause |
| --- | --- |
| `AlicatSinkDependencyError` | Optional extra not installed. Error message names the extra (`alicatlib[parquet]` / `alicatlib[postgres]`). |
| `AlicatSinkSchemaError` | Batch shape incompatible with the sink's locked schema. Unknown *optional* columns drop with a WARN log — they don't raise. |
| `AlicatSinkWriteError` | Backing store rejected the write. Wraps the underlying driver exception (sqlite3 / asyncpg / pyarrow) so handlers don't need to import optional deps. |

See [logging.md](logging.md) for sink setup.

## EEPROM-wear warnings

Every command carrying `save=True` (active gas, PID gains, deadband,
batch, valve offset, totalizer save, setpoint source, ...) triggers
an EEPROM write on the device. Repeated saves wear the cell; the
library logs a WARN on the `alicatlib.session` logger when saves
exceed `AlicatConfig.save_rate_warn_per_min` per minute per device
(default: 10).

If you need to loop a setpoint rapidly, pass `save=False` — the
setting still takes effect for the current power cycle but doesn't
hit EEPROM.

## Getting raw wire bytes

The protocol client emits one `tx` and one `rx` DEBUG event per
write / read on the `alicatlib.protocol` logger, with structured
`{direction, raw, len}` extras. Enable at your root handler:

```python
import logging
logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")
logging.getLogger("alicatlib.protocol").setLevel(logging.DEBUG)
```

The `raw` extra carries the bytes verbatim; credentials never appear
in log lines (scrubbed at the `PostgresSink` boundary). See
[logging.md §Logger tree](logging.md#logger-tree) for the full event
schema.

## Capture a fixture from live hardware

Most library bugs are easier to diagnose against a recorded session
than a live one. The fixture format is a plaintext `>` / `<` dialog
readable by both humans and [`parse_fixture`](../src/alicatlib/testing.py);
see [testing.md §Fixture format](testing.md#fixture-format) for the
grammar and how to hand-write one, or replay the session through
`AlicatConfig`-driven DEBUG logs (see
[§Getting raw wire bytes](#getting-raw-wire-bytes) above) and paste the
`tx` / `rx` lines into a `.txt` file.

An automated `record_session(device, scenario)` capture helper is
planned but not shipped yet — follow
[issue #TODO](https://github.com/GraysonBellamy/alicatlib/issues)
or attach the raw DEBUG transcript to your bug report in the
meantime.

## Filing a bug

Collect:

- `DeviceInfo.model`, `.firmware.raw`, `.capabilities`,
  `.probe_report` (`repr(dev.info)` does fine).
- The exception traceback including the typed `ErrorContext` render.
- If available, the fixture capture from the section above or a
  `--log-level=DEBUG` pytest transcript with the `alicatlib.protocol`
  DEBUG lines included.

File at
[github.com/GraysonBellamy/alicatlib/issues](https://github.com/GraysonBellamy/alicatlib/issues).
