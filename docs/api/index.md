# API reference

Auto-generated from source docstrings via
[mkdocstrings-python](https://mkdocstrings.github.io/python/). Every
public name on the guide pages ([Devices](../devices.md),
[Commands](../commands.md), [Streaming](../streaming.md), …) links
back to the relevant reference section here.

## Top-level

- [`alicatlib`](alicatlib.md) — top-level re-exports
  (`open_device`, `AlicatManager`, `AlicatConfig`, errors, registries).

## Subpackages

- [`alicatlib.commands`](commands.md) — declarative command specs,
  request/response models, `Capability` / `ResponseMode` /
  `DecodeContext`.
- [`alicatlib.devices`](devices.md) — device facades
  (`Device`, `FlowMeter`, `FlowController`, `PressureMeter`,
  `PressureController`), `DeviceKind`, `Medium`, data-frame models,
  `open_device`, discovery helpers.
- [`alicatlib.manager`](manager.md) — `AlicatManager`, `DeviceResult`,
  `ErrorPolicy`.
- [`alicatlib.streaming`](streaming.md) — `Sample`, `record()`,
  `OverflowPolicy`, `AcquisitionSummary`, `PollSource`.
- [`alicatlib.sinks`](sinks.md) — `SampleSink` protocol, `pipe()`,
  first-party sinks (InMemory / CSV / JSONL / SQLite / Parquet /
  Postgres).
- [`alicatlib.sync`](sync.md) — sync facade over the async core.
- [`alicatlib.transport`](transport.md) — `Transport` protocol,
  `SerialTransport`, `FakeTransport`, serial settings.
- [`alicatlib.protocol`](protocol.md) — protocol client, parsers,
  framing.
- [`alicatlib.registry`](registry.md) — `Gas`, `Unit`, `Statistic`,
  `LoopControlVariable` registries.
- [`alicatlib.testing`](testing.md) — `FakeTransport`,
  `FakeTransportFromFixture`, `parse_fixture`.
- [`alicatlib.errors`](errors.md) — typed exception hierarchy.
- [`alicatlib.firmware`](firmware.md) — `FirmwareVersion`,
  `FirmwareFamily`.
- [`alicatlib.config`](config.md) — `AlicatConfig`, `config_from_env`.
