# Changelog

All notable changes to this project will be documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

#### Transport and protocol

- **Transport layer** (`alicatlib.transport`). `Transport` `Protocol`
  (PEP 544), `SerialTransport` wrapping `anyserial.SerialPort`,
  `FakeTransport` with scripted replies + recorded writes + forced-timeout
  knobs. Every I/O boundary has an explicit timeout; read-phase and
  write-phase timeouts are tagged distinctly in `ErrorContext.extra`.
- **Protocol client** (`alicatlib.protocol.AlicatProtocolClient`). One-in-
  flight `anyio.Lock`; `query_line` / `query_lines` / `write_only` using
  `anyio.fail_after` / `move_on_after` exclusively (`asyncio.wait_for`
  banned repo-wide via Ruff `TID251`). Multiline termination with a
  priority ladder: `is_complete` → `max_lines` → idle-timeout fallback
  (with a metric counter for commands that fall through).
- **Protocol-level DEBUG wire trace.** `AlicatProtocolClient` emits one
  `tx` / `rx` event per write / read under the `alicatlib.protocol`
  logger with structured `{direction, raw, len}` extras. Guarded by
  `isEnabledFor(DEBUG)` so the repr cost is paid only when a handler
  subscribes.
- **Parsers** (`alicatlib.protocol.parser`). `parse_ascii`, `parse_fields`,
  `parse_int`, `parse_float` — each raises `AlicatParseError` with raw
  bytes preserved.
- **Eager-task factory** (`alicatlib._runtime.install_eager_task_factory`).
  Opt-in `asyncio.eager_task_factory` install; no-op on trio.
  `AlicatConfig.eager_tasks` flag + `ALICATLIB_EAGER_TASKS` env var.

#### Registry

- **Registry** (`alicatlib.registry`). Build-time codegen from a
  primer-verified `codes.json`: 98 statistics, 150 gases, 119 units
  across 8 categories (Primer Appendix B-1 through B-8). Three
  singletons (`gas_registry`, `statistic_registry`, `unit_registry`)
  with alias-aware `coerce`, `by_code`, `aliases`, `suggest`.
  Pre-commit + CI idempotency guard on the generated `_codes_gen.py`.
- **`UnitCategory` first-class enum.** Primer unit codes repeat across
  categories (code 7 = `SLPM` / `bar` / `Sm³`), so `UnitRegistry.by_code`
  requires an explicit `category=` kwarg.

#### Device identification and factory

- **Identification.** `VE` (firmware), `??M*` (manufacturing), `??D*`
  (data-frame header). `DeviceInfo` consolidates the three into a typed
  summary. GP07R100 carries its own `??M*` dialect (M0–M8 labels, `\x08`
  padding); detection is by header, not firmware family.
- **Data frames.** `DataFrameFormat` with `DataFrameField` linked to
  `Statistic`. `??D*` DEFAULT and LEGACY dialects; parser strips GP
  padding. `DataFrame.as_dict()` uses a single `status` key.
- **Post-`??D*` `DCU` sweep.** Factory runs `DCU` for every numeric
  `DataFrameField` whose `unit` the `??D*` parser left `None` and
  rebuilds the format with the resolved unit. GP is skipped; the `---`
  placeholder is filtered; per-field failures leave the slot unresolved
  rather than failing the open.
- **Post-`??D*` `FPF` sweep.** Factory issues `FPF` for every numeric
  field and populates `DeviceInfo.full_scale`. `FlowController.setpoint`
  consults `full_scale[lv.statistic]` pre-I/O and raises
  `AlicatValidationError` when a request is outside `[−fs, +fs]`
  (bidirectional) or `[0, +fs]` (unidirectional).
- **Session.** `Session` wraps `AlicatProtocolClient` with
  command-execution gates: firmware, device-kind, media, destructive
  confirmation, and streaming-state. Per-call `timeout` surfaces on the
  public signature.
- **Factory / lifecycle.** `open_device(...)` runs the bootstrap
  identification sequence, applies model rules, and enters a `Device` /
  `FlowMeter` / `FlowController` / `PressureMeter` / `PressureController`
  as appropriate. Model rules route flow / pressure / liquid / CODA /
  BASIS.
- **Discovery.** `list_serial_ports()` (via `anyserial`), `probe(port)`
  (identify without committing), `find_devices(...)` (parallel
  identification across enumerated ports).
- **Stream recovery.** Factory writes raw `@@ {unit_id}\r` to snap a
  stuck-streaming device before a real session exists; caps
  `read_available` at 256 bytes on pre-stop sniff and post-stop drain.
- **Fallback identification.** Pre-VE firmware or devices that don't
  answer `VE` cleanly fall through to `??M*` + `model_hint`.
  `assume_media` / `assume_capabilities` kwargs on `open_device` let
  callers narrow for known hardware.

#### Commands

- **Gas surface.** `GAS_LIST` (`??G`), modern and legacy `GAS_SELECT`,
  `DCU` (unit-code query), `FPF` (full-scale query). Gas list decoded
  against captured 98-entry replies.
- **Tare commands.** `TARE_FLOW` (`T`) and `TARE_GAUGE_PRESSURE` (`TP`,
  on barometer-capable hardware), both facade-timed. Tare preconditions
  documented but not library-enforced (physical state is caller's
  responsibility).
- **Setpoint surface.** Modern `setpoint` (`LS` / `S`) and legacy-path
  setpoint decoding; `SetpointState` response model. Legacy-path decoder
  locates the setpoint column by `*_SETPT` statistic membership first,
  with a name-based fallback for fixtures without statistic codes.
- **Setpoint source.** `LSS` query / set with `SetpointSource` enum.
  Firmware-gated on families without `LSS`.
- **Loop control variable.** `LV` query / set with `Statistic` linkage
  via `LoopControlState`. Controller `LV` prefetch caches
  `Session.loop_control_variable`.
- **Controller facades.** `FlowController` and `PressureController`
  share a private `_ControllerMixin(Device)` with `setpoint`,
  `setpoint_source`, `loop_control_variable`, `hold_valves`,
  `hold_valves_closed`, `cancel_valve_hold`, `valve_drive`, `ramp_rate`,
  `deadband_limit`, and `auto_tare`. EEPROM-wear warning on the
  `alicatlib.session` logger when
  `AlicatConfig.save_rate_warn_per_min` is exceeded.
- **Valve commands.** `HOLD_VALVES` (`HP`), `HOLD_VALVES_CLOSED` (`HC`,
  destructive), `CANCEL_VALVE_HOLD` (`C`), `VALVE_DRIVE` (`VD`).
- **Control commands.** `RAMP_RATE` (`SR`) and `DEADBAND_LIMIT` (`LCDB`).
- **Data readings.** `DCZ` (zero band), `DCA` (averaging timing),
  `DCFRP` (STP/NTP pressure), `DCFRT` (STP/NTP temperature).
- **Analog output** (`ANALOG_OUTPUT`-gated). `ASOCV` with
  `AnalogOutputChannel` / `AnalogOutputSourceSetting` models.
- **Display** (`DISPLAY`-gated, except `unlock_display`). `FFP` (blink),
  `L` (lock), `U` (unlock — intentionally never gated, the safety escape
  for a locked device), `UD` (user data, with ASCII / length / `\r` /
  `\n` validation).
- **Tare control.** `ZCA` (auto-tare, controller-only) and `ZCP`
  (power-up tare, all devices).
- **Totalizer surface.** `TOTALIZER_CONFIG` (`TC`), `TOTALIZER_RESET`
  (`T <n>`, destructive), `TOTALIZER_RESET_PEAK` (`TP <n>`, destructive),
  `TOTALIZER_SAVE` (`TCR`). Reset encoders always emit the numeric
  totalizer argument so the wire form cannot degrade into bare `T\r` /
  `TP\r`, which are reserved for `TARE_FLOW` / `TARE_GAUGE_PRESSURE`.
- **Lifecycle.** Unit-ID change (`ADDR`) and baud change (`NCB`) — both
  destructive, both require `confirm=True`, both leave the session
  explicitly broken on success so the next call cannot interleave with a
  mid-reconciliation device.
- **Polling.** `POLL_DATA` / `Session.poll()`. `POLL_DATA.decode`
  returns a pure `ParsedFrame`; `Session.poll()` wraps timing.
- **`REQUEST_DATA` / `DV`** (`alicatlib.commands.polling`). Full encode /
  decode. Unique wire shape — reply carries no unit-ID prefix; invalid
  statistics return `--` per-slot; zero time rejects pre-I/O. Pre-I/O
  validation on averaging window (1–9999 ms) and statistic count
  (1–13 per call).
- **`Device.request(statistics, *, averaging_ms=1)`** facade on the
  `Device` base class. Zips raw wire values with the requested typed
  `Statistic` list into a `MeasurementSet` with wall-clock `received_at`
  stamped by the facade.

#### Streaming

- **`StreamingSession`** async context manager + iterator
  (`alicatlib.streaming`). Bounded-memory stream, overflow policy
  defaulting to `DROP_OLDEST`, strict / non-strict parse-error handling.
  Producer normalises real hardware frames by prepending the session
  unit ID when streaming drops the leading unit-id letter.
- **`Device.stream(...)` / `SyncDevice.stream(...)` /
  `SyncStreamingSession`.**
- **`AlicatProtocolClient.is_streaming` latch** + `Session._check_streaming`.
  Every request/response command fast-fails with
  `AlicatStreamingModeError` while streaming is active on the same bus.
- **`NCS` rate command** (V10 >= 10v05). Pure-function
  `encode_start_stream` / `encode_stop_stream` helpers.

#### Acquisition and sinks

- **`alicatlib.streaming.Sample`** — frozen dataclass with
  `device`/`unit_id`/`monotonic_ns`/`requested_at`/`received_at`/
  `midpoint_at`/`latency_s`/`frame`.
- **`alicatlib.streaming.record`** — absolute-target async context
  manager at a caller-chosen cadence. Uses `anyio.current_time()` +
  `anyio.sleep_until()` so scheduling drift never accumulates; overruns
  skip slots and bump `samples_late`. `OverflowPolicy.BLOCK` /
  `DROP_NEWEST` / `DROP_OLDEST` implemented.
- **`AcquisitionSummary`** + `alicatlib.streaming.PollSource` Protocol
  so the recorder is testable against a lightweight stub.
- **`alicatlib.manager.AlicatManager`** — multi-device orchestrator.
  Accepts four source shapes: port string, `Device`, `Transport`,
  `AlicatProtocolClient`. Canonicalises port identity via
  `os.path.realpath` on POSIX and uppercased `\\.\` prefix strip on
  Windows so `COM3` / `com3` / `\\\\.\\COM3` share one client.
  Ref-counted port entries so the last `remove()` on a shared bus tears
  down the transport.
- **`ErrorPolicy.{RAISE,RETURN}` + `DeviceResult[T]`**.
  `ErrorPolicy.RAISE` (default) collects every device's result then
  raises an `ExceptionGroup` if any failed. `ErrorPolicy.RETURN` always
  returns per-device `DeviceResult` containers.
- **Port-aware concurrent dispatch.** `AlicatManager.poll` / `.request`
  / `.execute` group devices by port and run different ports in
  parallel; same-port operations serialise on the shared protocol
  client lock.
- **`alicatlib.sinks.SampleSink`** Protocol + `pipe()` driver with
  batch-size and flush-interval thresholds.
- **Sinks.** `InMemorySink`, `CsvSink`, `JsonlSink`, `SqliteSink`
  (stdlib WAL + `synchronous=NORMAL` + `busy_timeout=5000` ms defaults,
  one `BEGIN IMMEDIATE` / `COMMIT` per batch), `ParquetSink` (pyarrow,
  zstd default, one row group per `write_many`, via
  `alicatlib[parquet]`), `PostgresSink` / `PostgresConfig` (asyncpg
  pool, binary `COPY` by default with `executemany` fallback,
  identifier validation, password scrubbing, via `alicatlib[postgres]`).
- **`AlicatSinkError` family** and shared `SchemaLock`. Unknown later
  columns drop with a one-shot WARN per key.
- **Stable row layout (`sample_to_row`)**. Both tabular sinks flatten
  via the same helper: `device`, `unit_id`, three ISO-8601 timestamps,
  `latency_s`, then frame fields, then `status`.
- **Structured observability.** Setpoint / `LSS` / `LV` write-paths each
  emit one pre-I/O `alicatlib.session` INFO event with structured
  `extra={}`; capability probing emits one summary per identification.
  Query-form calls stay silent.
- **`FirmwareVersion.raw`** preserves the `.N-R<NN>` revision suffix on
  every VE reply (`5v12.0-R22`, `8v17.0-R23`, `10v20.0-R24`). Sinks,
  dashboards, and diagnostics see the full string; gating still reads
  only `major` / `minor`.

#### Sync facade

- **`SyncPortal`** over `anyio.from_thread.start_blocking_portal`.
  Unwraps single-member `ExceptionGroup`s.
- **Blocking async-iterator bridge.** The portal drives an `__aiter__`
  without losing cancellation semantics.
- **`Alicat.open`** + full sync device facade tree (`SyncDevice`,
  `SyncFlowMeter`, `SyncFlowController`, `SyncPressureMeter`,
  `SyncPressureController`) wrapping every async method.
- **`SyncAlicatManager`** — one sync context manager over the async
  `AlicatManager`.
- **Sync `record()` context manager** and sync `pipe()` driver.
  `SyncStreamingSession` enters and exits through
  `SyncPortal.wrap_async_context_manager`.
- **`SyncSinkAdapter`** wrappers for every in-tree sink.
- **Sync discovery.** `list_serial_ports`, `probe`, `find_devices`.
- **Parity tests.** Compare every async / sync method pair by
  parameter name, kind, and default. Fails CI if a new async coroutine
  method lands without a sync wrapper.

#### Capabilities and safety

- **Capability split.** `BAROMETER` broken out from
  `TAREABLE_ABSOLUTE_PRESSURE` after hardware showed the two are
  independently present.
- **Typed errors.** `AlicatMissingHardwareError` raised from `Session`
  when a command needs hardware the device lacks (barometer, multi-valve,
  analog input, etc.).

#### Packaging and tooling

- Project scaffolding: `src/alicatlib` package layout with typed subpackages.
- `errors.py` with `AlicatError` hierarchy and typed `ErrorContext`.
- `firmware.py` with `FirmwareVersion` parser and ordering.
- `config.py` with `AlicatConfig` and `config_from_env`.
- `codes.json` shipped under `registry/data/`.
- Hatchling build, `uv`-managed dev env, `ruff` format+lint,
  `mypy --strict`, pyright as secondary type checker.
- GitHub Actions CI (lint, types, tests on 3.13/3.14 × Linux/macOS/Windows,
  build, codegen idempotency), release (trusted PyPI publishing), and
  docs (zensical + mkdocstrings-python deployed to Pages).
- Python floor 3.13 and serial backend
  [`anyserial`](https://pypi.org/project/anyserial/).
- Pre-commit hooks: ruff, mypy, codespell, whitespace, case-conflict,
  private-key detection, `uv-lock` sync.
- Versioning driven by `hatch-vcs` (tags produce versions).
- License declared via PEP 639 (`license = "MIT"` + `license-files`).
- Dev deps split into PEP 735 `dependency-groups`: `lint`, `type`,
  `test`, `docs`.
- Release workflow fires on `release: published`, with sigstore
  attestations and `twine check --strict`.
- `.gitattributes` with LF normalization and sdist `export-ignore` for
  `.github/` and dev-only root files.
- **Benchmarks.** `scripts/bench_query.py` (single-line query round-trip,
  with eager-task-factory A/B) and `scripts/bench_sinks.py` (synthetic
  sink-throughput). See [`docs/benchmarks.md`](docs/benchmarks.md).
- **Docs.** Architectural design at [`docs/design.md`](docs/design.md);
  usage at [`docs/logging.md`](docs/logging.md),
  [`docs/streaming.md`](docs/streaming.md),
  [`docs/commands.md`](docs/commands.md),
  [`docs/devices.md`](docs/devices.md),
  [`docs/safety.md`](docs/safety.md),
  [`docs/testing.md`](docs/testing.md),
  [`docs/troubleshooting.md`](docs/troubleshooting.md).
