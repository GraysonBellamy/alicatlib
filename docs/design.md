# alicatlib Design Document

**Status:** v1 architecture specification with hardware-validation
evidence incorporated.
**Scope:** the `alicatlib` Python package, its public API, runtime model,
testing strategy, and hardware assumptions.

This document is organized as a design specification first and an evidence
record second:

- Sections 1-4 describe purpose, scope, principles, supported devices, and the
  package layout.
- Section 5 is the core architecture. It defines the transport, protocol,
  command, session, device, acquisition, sink, sync, error, configuration,
  observability, and safety models.
- Sections 6-14 cover testing, packaging, examples, command coverage,
  future work, risks, documentation, open questions, and release criteria.
- Sections 15-16 are appendices for implementation notes and hardware evidence.
  They preserve the details that informed the design without interrupting the
  main specification.

---

## 1. Purpose and Scope

`alicatlib` provides a Python API for Alicat instruments over Alicat's ASCII
serial protocol. The package covers the Alicat device matrix:

- Function: flow and pressure.
- Form: meter and controller.
- Medium: gas and liquid.

The initial package targets:

- Interactive scripts and notebooks through a synchronous facade.
- Long-running acquisition services through an asynchronous core.
- Scientific experiments where timing fidelity, typed data, and provenance
  matter.

### Supported Families

Supported model families are resolved through an explicit prefix registry:

| Family | Prefixes | Kind | Medium |
| --- | --- | --- | --- |
| Thermal gas flow meters | `M-`, `MS-`, `MQ-`, `MW-`, `MB-`, `MBS-`, `MWB-` | Flow meter | Gas |
| Thermal gas flow controllers | `MC-`, `MCS-`, `MCQ-`, `MCW-`, `MCD-`, `MCV-`, `MCE-`, `MCH-`, `MCP-`, `MCR-`, `MCT-`, compound `MCR*`/`MCDW-` prefixes, `SFF-` | Flow controller | Gas |
| BASIS | `B-`, `BC-` | Flow meter/controller | Gas |
| Pressure meters | `P-`, `PB-`, `PS-`, `EP-` | Pressure meter | Gas for now |
| Pressure controllers | `PC-`, `PCS-`, `PCD-`, `PCDS-`, `PCRD-`, `PCRDS-`, `PCRD3-`, `PCRD3S-`, `PCD3-`, `PCPD-`, `PCH-`, `PCP-`, `PCR-`, `PCR3-`, `PC3-`, `PCAS-`, `EPC-`, `EPCD-`, `IVC-` | Pressure controller | Gas, except PCD `S` variants can be gas or liquid |
| Liquid flow meters | `L-`, `LB-` | Flow meter | Liquid |
| Liquid flow controllers | `LC-`, `LCR-` | Flow controller | Liquid |
| CODA Coriolis | `K-`, `KM-`, `KC-`, `KF-`, `KG-` | Flow meter/controller by prefix | Gas and liquid by default |

CODA model names start with K-family prefixes, not `CODA-`. The CODA part
number decoder encodes kind in the first field: `K-` and `KM-` are meters;
`KC-`, `KF-`, and `KG-` are controller variants. The decoder does not encode
medium, so K-family rules default to `Medium.GAS | Medium.LIQUID`; users narrow
a specific unit with `assume_media=` when needed.

The closed-volume PCD pressure-controller family has a known medium split. Per
the 2024 PCD-Series spec sheet, `PCDS-`, `PCRDS-`, and `PCRD3S-` support gas
and liquid. Non-`S` closed-volume siblings remain gas-only. Other `P-*` and
flowing-process `PC-*` devices remain gas-only in the registry until
fluid-select behavior is verified on hardware.

## 2. Goals and Non-Goals

### Goals

1. **Correctness first.** Reliable over hours or days with one device and with
   multiple devices driven concurrently from one process.
2. **Robust I/O.** Every read and write has an explicit timeout. Unexpected
   responses raise typed exceptions. Partial data is never returned as success.
3. **Performance.** Open a serial port once. Avoid per-command lookups into
   large JSON blobs at runtime. Use absolute-target scheduling for acquisition.
4. **Extensibility.** Keep transports swappable, commands declarative, and
   device families isolated behind prefix rules and facades.
5. **Maintainability.** Favor typed models, small modules, generated registries,
   Ruff formatting, and `mypy --strict`.
6. **Discoverable API.** Expose typed arguments such as `Gas.N2` and typed
   responses such as `DataFrame`, `GasState`, and `SetpointState`.
7. **Async-first, sync-available.** Async is canonical. Sync wraps async without
   reimplementing command logic.
8. **Data out, not sinks in.** The library emits typed samples. Consumers choose
   where they go. First-party sinks live behind extras.
9. **Safety.** Dangerous operations require explicit confirmation. Setpoints and
   numeric command arguments are validated before I/O.
10. **Lean core.** `pip install alicatlib` depends only on `anyio` and the
    selected serial backend. No database, data-science, validation-framework, or
    ORM dependency is imported by core modules.

### Non-Goals

- No GUI or web server.
- No multi-process RPC. One process owns a serial port.
- No Modbus or TCP implementation in v1. Interfaces exist for later transports.
- No client-side re-derivation of CODA density or temperature.
- No gas-mix composition editing on liquid-only devices.
- No ORM. Sinks are thin wrappers.
- No built-in `pint` integration, though field names and unit metadata leave room
  for user integration.

## 3. Design Principles

1. **One layer, one job.** Transport moves bytes. Protocol frames commands.
   Commands encode and parse. Sessions serialize I/O and gate capability.
   Devices expose user-facing methods. Recorders produce samples. Sinks store
   samples.
2. **Async core, sync wrapper.** Alicat operations are I/O-bound and benefit from
   cooperative concurrency.
3. **Transport is separate from protocol.** Serial, fake, TCP, and future
   transports satisfy the same interface.
4. **Protocol is separate from product API.** Framing and parsing are testable
   without device classes.
5. **Commands are declarative.** A `Command` object owns encoding, decoding,
   firmware support, device support, medium support, capabilities, and response
   shape.
6. **Typed models at boundaries.** Public returns are frozen dataclasses with
   `slots=True`; `.as_dict()` exists where serialization needs a flat view.
7. **Explicit capability model.** Firmware families, device kinds, media, and
   hardware capabilities are metadata, not scattered ad hoc branches.
8. **Optional means optional.** Core control does not import Postgres, pandas,
   pyarrow, pydantic, or numpy.
9. **Hardware-free tests by default.** Fake transports and fixtures cover most
   behavior in CI.
10. **Fail loudly and specifically.** Timeouts, malformed replies, rejected
    commands, unknown gases, medium mismatches, and unsupported firmware are
    distinct error classes.
11. **Safety is part of the API.** The library refuses unsafe requests before
    bytes hit the wire whenever it has enough information to do so.

## 4. Package Layout

The package uses `src/` layout and ships `py.typed`.

```text
src/
  alicatlib/
    __init__.py
    py.typed
    errors.py
    firmware.py
    config.py
    _logging.py

    transport/
      base.py
      serial.py
      fake.py

    protocol/
      framing.py
      client.py
      parser.py
      streaming.py
      raw.py

    registry/
      _codes_gen.py
      aliases.py
      gases.py
      fluids.py
      units.py
      statistics.py
      reference_conditions.py
      data/codes.json

    commands/
      base.py
      catalog.py
      gas.py
      setpoint.py
      tare.py
      valve.py
      units.py
      totalizer.py
      output.py
      system.py
      diagnostics.py

    devices/
      kind.py
      medium.py
      models.py
      base.py
      factory.py
      session.py
      data_frame.py
      flow_meter.py
      flow_controller.py
      pressure_meter.py
      pressure_controller.py
      discovery.py

    streaming/
      sample.py
      recorder.py

    sinks/
      base.py
      _schema.py
      csv.py
      jsonl.py
      memory.py
      sqlite.py
      parquet.py
      postgres.py

    manager.py
    sync/
      portal.py
      __init__.py
    testing.py
```

Tests mirror these layers:

- Unit tests for commands, parsers, registries, firmware, configuration, device
  factory, sessions, and fake transport.
- Integration tests against `FakeTransport`.
- Hardware tests marked by safety tier: read-only, stateful, destructive, and
  GP-specific.
- Fixtures under `tests/fixtures/responses/` and empirical behavior in
  `tests/fixtures/device_matrix.yaml`.

## 5. Architecture

### 5.1 Transport Layer

Purpose: move bytes. The transport knows nothing about Alicat commands.

```python
class Transport(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def write(self, data: bytes, *, timeout: float) -> None: ...
    async def read_until(self, separator: bytes, timeout: float) -> bytes: ...
    async def read_available(
        self, idle_timeout: float, max_bytes: int | None = None
    ) -> bytes: ...
    async def drain_input(self) -> None: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def label(self) -> str: ...
```

Transport invariants:

- `open()` and `close()` are lifecycle operations, not per-command operations.
- Every I/O boundary is timeout-bounded. A hung write and a hung read both raise
  `AlicatTimeoutError`, with phase information in the context.
- Empty or partial bytes are not success values.
- Backend exceptions normalize to `AlicatTransportError` with `__cause__`
  preserved.

`SerialTransport` wraps `anyserial.SerialPort` and takes a frozen settings
dataclass:

```python
@dataclass(frozen=True, slots=True)
class SerialSettings:
    port: str
    baudrate: int = 19200
    bytesize: int = 8
    parity: Parity = Parity.NONE
    stopbits: StopBits = StopBits.ONE
    rtscts: bool = False
    xonxoff: bool = False
    exclusive: bool = True
```

The committed serial backend is `anyserial`, pinned tightly while it remains
0.1.x. It is anyio-native, avoids `to_thread` for normal serial I/O, and ships
testing primitives for byte-level transport tests. `pyserial` is not a core
dependency; it is reachable only through optional discovery fallback support.

`FakeTransport` scripts request/response exchanges for tests. It records every
write, supports callables for dynamic replies, and can simulate timeouts,
malformed replies, and latency.

### 5.2 Protocol Client

Purpose: one request/response path for Alicat wire traffic.

```python
class AlicatProtocolClient:
    def __init__(
        self,
        transport: Transport,
        *,
        eol: bytes = b"\r",
        default_timeout: float = 0.5,
        multiline_timeout: float = 1.0,
        drain_before_write: bool = False,
    ) -> None: ...

    async def query_line(
        self,
        command: bytes,
        *,
        timeout: float | None = None,
        write_timeout: float | None = None,
    ) -> bytes: ...

    async def query_lines(
        self,
        command: bytes,
        *,
        first_timeout: float | None = None,
        idle_timeout: float | None = None,
        max_lines: int | None = None,
        is_complete: Callable[[Sequence[bytes]], bool] | None = None,
        write_timeout: float | None = None,
    ) -> tuple[bytes, ...]: ...

    async def write_only(
        self, command: bytes, *, timeout: float | None = None
    ) -> None: ...
```

Rules:

- Exactly one in-flight command per client. Concurrent callers serialize on an
  `anyio.Lock`.
- Commands are ASCII and terminated with `\r` exactly once.
- `query_line` waits for one terminator.
- `query_lines` reads the first line with a normal timeout, then stops on the
  first of: `is_complete(lines)`, `max_lines`, or `idle_timeout`. Commands with
  known multiline shapes must declare either an expected line count or an
  `is_complete` predicate.
- A bare `?` reply becomes `AlicatCommandRejectedError`.
- Empty responses are errors unless a command explicitly declares them valid.
- After protocol or command-rejection errors, the client drains residual bytes
  so a multi-part reject cannot contaminate the next command.

Timeouts use `anyio.fail_after` and `anyio.move_on_after`. `asyncio.wait_for` is
blocked in source via the banned-API lint configuration.

Default timeouts are 0.5 s for single-line commands and 1.0 s for multiline
tables such as `??M*`, `??D*`, and gas lists. Per-command overrides remain
available.

`asyncio.eager_task_factory` is an opt-in optimization through
`AlicatConfig.eager_tasks`. Hardware benchmarking on 2026-04-17 showed no
meaningful improvement on the tested PL2303 adapter, so eager execution is not
part of the architecture.

### 5.3 Registry

`registry/data/codes.json` is the source of truth for:

- Gas codes.
- Fluid codes.
- Unit codes and unit categories.
- Statistic codes.
- Aliases and display names.

Build-time generation emits `_codes_gen.py` with typed enums and lookup tables.
The generated file is committed, and CI checks that regeneration is
byte-identical.

Gas and fluid are separate namespaces:

```python
class Gas(StrEnum):
    N2 = "N2"
    AIR = "Air"
    @property
    def code(self) -> int: ...
    @property
    def display_name(self) -> str: ...

class Fluid(StrEnum):
    WATER = "H2O"
    IPA = "IPA"
    ETHANOL = "EtOH"
    @property
    def code(self) -> int: ...
    @property
    def display_name(self) -> str: ...
```

The split avoids accepting gas names on liquid-only devices or fluid names on
gas-only devices. CODA and other dual-medium devices can expose both APIs when
their `DeviceInfo.media` includes both flags.

Unit lookup is category-aware because Alicat unit codes repeat across categories.
For example, code `7` can mean `SLPM`, `bar`, or `Sm3` depending on the
statistic being configured.

```python
class UnitCategory(StrEnum):
    STD_NORM_FLOW = "std_norm_flow"
    TRUE_MASS_FLOW = "true_mass_flow"
    TOTAL_STD_NORM_VOLUME = "total_std_norm_volume"
    VOLUMETRIC_FLOW = "volumetric_flow"
    TOTAL_VOLUME = "total_volume"
    PRESSURE = "pressure"
    TEMPERATURE = "temperature"
    TIME_INTERVAL = "time_interval"

class UnitRegistry:
    def coerce(self, value: Unit | str) -> Unit: ...
    def by_code(self, code: int, *, category: UnitCategory) -> Unit: ...
    def categories(self, unit: Unit) -> frozenset[UnitCategory]: ...
```

Alias registries provide coercion, reverse lookup, aliases, and suggestions for
`Gas`, `Fluid`, `Statistic`, and `Unit`. Unknown values raise specific errors
such as `UnknownGasError`, `UnknownFluidError`, and `UnknownUnitError`.

Reference conditions live outside `Unit` because they depend on medium and,
for liquids, fluid identity:

```python
@dataclass(frozen=True, slots=True)
class ReferenceConditions:
    medium: Medium
    temperature_c: float
    pressure_atm: float | None
    density_kg_m3: float | None
```

Gas standard flow uses 0 C / 1 atm. Liquid reference conditions use per-fluid
reference density at 20 C when known.

Generation and validation rules:

- `scripts/gen_codes.py` is deterministic and supports `--check`.
- Pre-commit and CI both run the codegen check.
- Duplicate enum codes, duplicate aliases, duplicate member names, and
  gas/fluid namespace collisions are data errors.
- Every enum member round-trips through the relevant registry lookup.
- Every fluid either has reference conditions or an explicit opt-out.

### 5.4 Command Layer

Every Alicat command is represented by an immutable `Command` object.

```python
class ResponseMode(Enum):
    NONE = "none"
    LINE = "line"
    LINES = "lines"
    STREAM = "stream"

class Capability(Flag):
    NONE = 0
    BAROMETER = auto()
    TAREABLE_ABSOLUTE_PRESSURE = auto()
    SECONDARY_PRESSURE = auto()
    ANALOG_INPUT = auto()
    ANALOG_OUTPUT = auto()
    SECONDARY_ANALOG_OUTPUT = auto()
    REMOTE_TARE_PIN = auto()
    MULTI_VALVE = auto()
    THIRD_VALVE = auto()
    BIDIRECTIONAL = auto()
    TOTALIZER = auto()
    DISPLAY = auto()

@dataclass(frozen=True, slots=True)
class DecodeContext:
    unit_id: str
    firmware: FirmwareVersion
    capabilities: Capability
    command_prefix: bytes = b""
    data_frame_format: DataFrameFormat | None = None

@dataclass(frozen=True, slots=True)
class Command[Req, Resp]:
    name: str
    token: str
    response_mode: ResponseMode
    device_kinds: frozenset[DeviceKind]
    media: Medium = Medium.GAS | Medium.LIQUID
    required_capabilities: Capability = Capability.NONE
    min_firmware: FirmwareVersion | None = None
    max_firmware: FirmwareVersion | None = None
    firmware_families: frozenset[FirmwareFamily] = frozenset()
    destructive: bool = False
    experimental: bool = False
    case_sensitive: bool = False
    prefix_less: bool = False
    expected_lines: int | None = None
    is_complete: Callable[[Sequence[bytes]], bool] | None = None

    def encode(self, ctx: DecodeContext, request: Req) -> bytes: ...
    def decode(self, response: bytes | tuple[bytes, ...], ctx: DecodeContext) -> Resp: ...
```

`Session.execute` applies all gates before I/O, in this order:

1. Device kind.
2. Medium.
3. Firmware family and version.
4. Required capabilities.
5. Destructive-confirm.

The order gives the clearest error first. A gas command on a liquid-only device
is a medium mismatch; a flow-controller command on a meter is a kind mismatch;
a command that requires a process-port absolute-pressure sensor raises a
capability error before the device can silently ignore it.

Important capability split:

- `Capability.BAROMETER` means `FPF 15` reports a plausible barometric reading.
- `Capability.TAREABLE_ABSOLUTE_PRESSURE` means `PC` can re-zero a process-port
  absolute-pressure sensor.

Hardware showed these are not equivalent. Several flow controllers report a
barometer but reject or ignore `PC`, so `tare_absolute_pressure` is gated on
`TAREABLE_ABSOLUTE_PRESSURE`, not on `BAROMETER`.

GP command-prefix rule:

- GP writes generally require `$$` after the unit ID.
- GP reads observed on GP07R100 are prefix-less: poll, `??M*`, `??D*`, and
  `??G*` accept prefix-less forms and reject or time out on `$$`.
- Commands express this with `prefix_less=True`; the session sets
  `DecodeContext.command_prefix` to `b"$$"` on GP only when the command has not
  opted out.

Legacy pairs are normal command specs selected by the facade:

- Modern gas select: `GS` on V10 >= 10v05.
- Legacy gas select: `G <code>` on GP, V1_V7, V8_V9, and V10 < 10v05.
- Modern setpoint: `LS` on V8/V9 >= 9v00 and V10.
- Legacy setpoint: `S` on V1_V7 and older V8/V9.

Encoders distinguish `None`, `0`, `False`, and empty strings. `None` means
omitted/query form; `0` and `False` are valid values where the command allows
them.

Range validation belongs to command specs, not only facades. Documented ranges
include:

- `DV`: 1-13 statistics; averaging window must be positive.
- `DCA`: 0-9999 ms.
- `DCZ`: 0.0-6.38 percent.
- PID/PDF gains: 0-65535.
- `ZCA`: 0.1-25.5 s delay.
- Custom gas mixtures: mix `0` or `236..255`; percentages sum to 100.00 +/- 0.01.
- User data: slots `0..3`; ASCII value <= 32 chars.
- Baud: one of `2400`, `9600`, `19200`, `38400`, `57600`, `115200`.
- Loop-control variable: restricted enum, not arbitrary `Statistic`.

The command catalog is immutable. Cross-cutting behavior such as retry,
logging, fixture recording, and EEPROM-save rate monitoring wraps
`Session.execute` rather than mutating command singletons.

### 5.5 Typed Models

Public return values are frozen dataclasses with `slots=True`.

```python
ProbeOutcome: TypeAlias = Literal[
    "present", "absent", "timeout", "rejected", "parse_error"
]

@dataclass(frozen=True, slots=True)
class DeviceInfo:
    unit_id: str
    manufacturer: str | None
    model: str
    serial: str | None
    manufactured: str | None
    calibrated: str | None
    calibrated_by: str | None
    software: str
    firmware: FirmwareVersion
    firmware_date: date | None
    kind: DeviceKind
    media: Medium
    capabilities: Capability
    probe_report: Mapping[Capability, ProbeOutcome]
    full_scale: Mapping[Statistic, FullScaleValue]
    gas_state: GasState | None
    fluid_state: FluidState | None

@dataclass(frozen=True, slots=True)
class DataFrame:
    unit_id: str
    format: DataFrameFormat
    values: Mapping[str, float | str | None]
    values_by_statistic: Mapping[Statistic, float | str | None]
    status: frozenset[StatusCode]
    received_at: datetime
    monotonic_ns: int
    def as_dict(self) -> dict[str, float | str | None]: ...
    def get_float(self, name: str) -> float | None: ...
    def get_statistic(self, stat: Statistic) -> float | str | None: ...
```

`DataFrame.values` is keyed by wire-level field names. `values_by_statistic` is
keyed by typed `Statistic` values when the field can be linked to the registry.
Fields with no statistic mapping appear only in `values`.

`DataFrame.as_dict()` emits a flat, sink-friendly representation. It uses a
single stable `status` field rather than sparse per-status boolean columns,
because first-batch CSV schema inference would otherwise miss status flags that
appear later.

### 5.6 Data Frame Format

The poll frame returned by `A\r` is dynamic. The session discovers the shape at
startup with `??D*` and caches a `DataFrameFormat`.

```python
@dataclass(frozen=True, slots=True)
class DataFrameField:
    name: str
    raw_name: str
    type_name: str
    statistic: Statistic | None
    unit: Unit | None
    conditional: bool
    parser: Callable[[str], float | str | None]

class DataFrameFormatFlavor(Enum):
    DEFAULT = 0
    SIGNED = 1          # reserved
    VARIABLE_V8 = 2     # reserved
    LEGACY = 3

@dataclass(frozen=True, slots=True)
class ParsedFrame:
    unit_id: str
    values: Mapping[str, float | str | None]
    values_by_statistic: Mapping[Statistic, float | str | None]
    status: frozenset[StatusCode]

@dataclass(frozen=True, slots=True)
class DataFrameFormat:
    fields: tuple[DataFrameField, ...]
    flavor: DataFrameFormatFlavor
    def names(self) -> tuple[str, ...]: ...
    def parse(self, raw: bytes) -> ParsedFrame: ...
```

`DataFrameFormat.parse` is pure: bytes in, `ParsedFrame` out. The session wraps
the parsed frame with timing metadata to create `DataFrame`.

Observed `??D*` dialects:

- `DEFAULT`: canonical layout observed on 6v21, 7v09, 8v17, 8v30, 10v03,
  10v04, and 10v20. Header includes `ID_ NAME TYPE WIDTH NOTES`; field rows
  carry statistic codes and `*` conditional markers.
- `LEGACY`: older layout observed on 5v12 and GP07R100. Header includes
  `NAME TYPE MinVal MaxVal UNITS`; field rows have no statistic-code column and
  no `*` marker.

The dialect transition is not family-correlated: 5v12 is `LEGACY`, while 6v21
and 7v09 are also V1_V7-family but use `DEFAULT`.

Parsing strategy:

1. Split the poll frame on whitespace.
2. Match required fields left to right.
3. Interpret surplus tokens as status codes or conditional-field values.
4. Raise `AlicatParseError` if required fields are missing.

Engineering units are not transmitted in poll frames. `DataFrameField.unit` is
bound from startup probes where possible (`DCU`, `FPF`) and remains `None` when
unknown.

### 5.7 Session

The session is the only object that sends commands for a device.

```python
class Session:
    def __init__(
        self,
        client: AlicatProtocolClient,
        *,
        unit_id: str,
        info: DeviceInfo,
        data_frame_format: DataFrameFormat,
        command_lock: anyio.Lock,
        default_timeout: float = 0.5,
    ) -> None: ...

    async def execute(
        self,
        command: Command[Req, Resp],
        request: Req,
        *,
        timeout: float | None = None,
    ) -> Resp: ...

    async def refresh_data_frame_format(self) -> DataFrameFormat: ...
    async def refresh_firmware(self) -> FirmwareVersion: ...
    async def refresh_capabilities(self) -> Capability: ...
    async def change_unit_id(self, new_unit_id: str, *, confirm: bool = False) -> None: ...
    async def change_baud_rate(self, new_baud: int, *, confirm: bool = False) -> None: ...
    async def close(self) -> None: ...
```

Responsibilities:

- Validate unit IDs (`A`-`Z`, plus `@` only in streaming context).
- Apply command gates before writing.
- Build `DecodeContext`.
- Dispatch via the protocol client.
- Decode responses.
- Enrich `AlicatError` instances with command, unit ID, firmware, port, raw
  bytes, and elapsed time.
- Emit structured log events.

Lifecycle operations need special handling because they invalidate session state:

- `change_unit_id` writes `<old>@ <new>`, waits a grace period, verifies the new
  ID, and updates cached state only after verification. V1_V7 devices can emit a
  data-frame acknowledgement, so the grace window is 200 ms before verification
  drains.
- `change_baud_rate` writes `NCB`, reads the acknowledgement at the old baud,
  reopens the transport at the new baud, verifies with the new settings, and
  marks the session broken if reconciliation fails.
- Stream entry and exit are handled by streaming state, not generic command
  execution.

Critical reconciliation uses bounded cancellation shields. External cancellation
is delayed only while the session is making device and client state consistent;
the shield has a timeout so a wedged adapter cannot hang forever.

Multiple units on one RS-485 port share one protocol client and one lock. Calls
on the same physical port serialize; calls on different ports can run
concurrently.

### 5.8 Streaming Mode

Streaming mode is a state change: the device pushes frames continuously and its
unit ID becomes `@`. Request/response traffic on the same bus becomes unsafe.

Rules:

- One streamer per physical port.
- Entering stream mode marks the shared client as streaming; all normal
  `Session.execute` calls on that port fail fast with `AlicatStreamingModeError`.
- `open_device` passively checks for stale streaming frames and, by default,
  sends raw `@@ {unit_id}\r` before identification if a prior process left the
  device streaming.
- Stop-stream uses the literal prefix-less form `@@ {new_unit_id}\r`.
- Exiting a streaming context sends stop-stream even when the body raises.
- Parsing errors are logged and skipped unless `strict=True`.
- An internal producer task reads frames into a bounded memory stream with an
  overflow policy.

`StreamingSession(rate_ms=...)` configures the device's `NCS` interval before
entering streaming. This is distinct from `record(..., rate_hz=...)`, which is a
software polling loop across one or more devices.

### 5.9 Device Factory and Facades

Device construction has three jobs: open the transport, identify the hardware,
and return the narrowest useful facade for that device kind. Opening is
context-manager-first:

```python
@asynccontextmanager
async def open_device(
    port: str | Transport | AlicatProtocolClient,
    *,
    unit_id: str = "A",
    serial: SerialSettings | None = None,
    timeout: float = 0.5,
    recover_from_stream: bool = True,
    model_hint: str | None = None,
    assume_capabilities: Capability = Capability.NONE,
    assume_media: Medium | None = None,
) -> AsyncIterator[Device]: ...
```

Common usage:

```python
async with open_device("/dev/ttyUSB0") as dev:
    frame = await dev.poll()
```

Inputs can be a port string, a pre-built `Transport`, or a pre-built
`AlicatProtocolClient`.

#### Identification Pipeline

1. Optional stale-stream recovery.
2. Try `VE` to parse firmware and firmware date. GP devices may not respond.
3. Try `??M*` on supported paths. Hardware shows `??M*` works broadly:
   V1_V7, V8/V9, V10, and GP, with a distinct GP dialect.
4. If both `VE` and `??M*` cannot provide enough information, require
   `model_hint` and synthesize minimal `DeviceInfo`.
5. Probe capabilities best-effort. Timeouts, rejections, and parse failures
   leave the capability absent and record a `probe_report` outcome.
6. Query `??D*` and cache `DataFrameFormat`.
7. For each numeric `DataFrameField` whose unit the `??D*` parser left
   `None`, probe `DCU` and bind the unit if the reply resolves against the
   registry. Best-effort per field — rejections, timeouts, and firmware
   gates (e.g. `DCU` requires V10 10v05+) leave the slot unresolved.
8. For each numeric `DataFrameField`, probe `FPF` and populate
   `DeviceInfo.full_scale`. The `A <zero> <code> ---` absent-statistic
   reply is treated as a rejection. GP is skipped (no `FPF`).
9. For controller kinds, pre-cache the loop-control variable via `LV` so
   `setpoint` can range-check pre-I/O without a round-trip. Firmware
   that rejects `LV` leaves the cache `None` and the range check is
   skipped at the facade.
10. Classify the model through `MODEL_RULES` and return the facade class.

#### User Assertions

`assume_capabilities` unions user-supplied capabilities onto the probed set.
This lets users assert hardware that cannot be safely probed, such as
`TAREABLE_ABSOLUTE_PRESSURE`.

`assume_media` replaces the prefix-derived medium. This lets users narrow a
CODA or other ambiguous-prefix device from `GAS | LIQUID` to a known
single-medium configuration.

#### Facade Tree

Facade classes are shaped by device kind, not by medium. Medium is runtime data
on `DeviceInfo`; command dispatch uses that data to gate gas-only,
liquid-only, and dual-medium operations.

```python
class Device:
    # Core I/O
    async def poll(self) -> DataFrame: ...
    async def request(self, stats, *, averaging_ms: int = 1) -> MeasurementSet: ...
    def stream(self, *, rate_ms: int | None = None, ...) -> StreamingSession: ...
    async def execute(self, command, request) -> Resp: ...

    # Gas / units / tare — all-device
    async def gas(self, gas=None, *, save=None) -> GasState: ...
    async def gas_list(self) -> Mapping[int, str]: ...
    async def engineering_units(self, statistic, unit=None, *, apply_to_group=False, override_special_rules=False) -> UnitSetting: ...
    async def full_scale(self, statistic) -> FullScaleValue: ...
    async def tare_flow(self) -> TareResult: ...
    async def tare_gauge_pressure(self) -> TareResult: ...
    async def tare_absolute_pressure(self) -> TareResult: ...

    # Tier-2 all-device
    async def zero_band(self, zero_band=None) -> ZeroBandSetting: ...
    async def average_timing(self, statistic_code, averaging_ms=None) -> AverageTimingSetting: ...
    async def stp_ntp_pressure(self, mode, pressure=None, unit_code=None) -> StpNtpPressureSetting: ...
    async def stp_ntp_temperature(self, mode, temperature=None, unit_code=None) -> StpNtpTemperatureSetting: ...
    async def analog_output_source(self, channel=AnalogOutputChannel.PRIMARY, value=None, unit_code=None) -> AnalogOutputSourceSetting: ...
    async def blink_display(self, duration_s=None) -> BlinkDisplayState: ...
    async def lock_display(self) -> DisplayLockResult: ...
    async def unlock_display(self) -> DisplayLockResult: ...
    async def user_data(self, slot, value=None) -> UserDataSetting: ...
    async def power_up_tare(self, enable=None) -> PowerUpTareState: ...
    async def totalizer_config(self, totalizer=TotalizerId.FIRST, *, flow_statistic_code=None, ...) -> TotalizerConfig: ...
    async def totalizer_reset(self, totalizer=TotalizerId.FIRST, *, confirm=False) -> TotalizerResetResult: ...
    async def totalizer_reset_peak(self, totalizer=TotalizerId.FIRST, *, confirm=False) -> TotalizerResetResult: ...
    async def totalizer_save(self, enable=None, *, save=None) -> TotalizerSaveState: ...

class FlowMeter(Device): ...
class PressureMeter(Device): ...

class FlowController(FlowMeter, _ControllerMixin): ...
class PressureController(PressureMeter, _ControllerMixin): ...
```

Controller-kind methods are implemented once on the private
`_ControllerMixin` and shared by `FlowController` and `PressureController`.
The mixin contributes `setpoint`, `setpoint_source`, `loop_control_variable`,
`hold_valves`, `hold_valves_closed`, `cancel_valve_hold`, `valve_drive`,
`ramp_rate`, `deadband_limit`, and `auto_tare`. The same pattern is mirrored on
the sync facade.

A liquid flow controller and a gas flow controller share facade methods.
Individual command specs decide which methods are legal for the current medium.

Setpoint guardrails:

- Analog or user-knob setpoint source blocks serial setpoint writes.
- Negative values require `Capability.BIDIRECTIONAL`.
- `0` is a valid setpoint.
- Legacy firmware dispatches to `S`; modern firmware dispatches to `LS`.
- Full-scale range checks land when the controlled-statistic cache is available.

Liquid-specific methods — `fluid()`, `fluid_list()`, liquid flow tare,
`LC-` / `LCR-` setpoint semantics, per-fluid reference density — require a
liquid-device hardware capture to pin wire forms and are tracked as
future work (see §9, §10, §13).

### 5.9a Medium Model

`Medium` is a `Flag`:

```python
class Medium(Flag):
    NONE = 0
    GAS = auto()
    LIQUID = auto()
```

Bitwise checks keep medium gates simple:

```python
if not (device.info.media & command.media):
    raise AlicatMediumMismatchError(...)
```

Attachment sites:

| Site | Purpose |
| --- | --- |
| `DeviceInfo.media` | Resolved during identification from `MODEL_RULES`, then optionally replaced by `assume_media`. |
| `Command.media` | Defaults to gas-or-liquid; gas-specific and liquid-specific commands narrow it. |
| `Gas` / `Fluid` enums | Separate typed namespaces for medium-specific arguments. |
| `ReferenceConditions` | Medium-specific standard/reference conditions. |

`assume_media` replaces because the user is asserting the configured medium of a
specific unit. `assume_capabilities` unions because the user is asserting extra
hardware that probing may have missed.

### 5.10 Firmware Model

Alicat firmware has family-scoped versioning:

```python
class FirmwareFamily(Enum):
    GP = "GP"
    V1_V7 = "1v-7v"
    V8_V9 = "8v-9v"
    V10 = "10v"

NUMERIC_FAMILIES = {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10}

@dataclass(frozen=True, slots=True)
class FirmwareVersion:
    family: FirmwareFamily
    major: int
    minor: int
    raw: str
```

Ordering is defined only within a family. Comparing `GP` to `10v05`, or `7v99`
to `8v00`, raises `TypeError`. `Session.execute` checks family before version
ranges and surfaces unsupported families as `AlicatFirmwareError`.

Observed firmware behavior:

- GP devices may not respond to `VE`.
- GP manufacturing info exists via a distinct `??M*` dialect on the captured
  GP07R100.
- Numeric firmware generally reports month-name VE dates such as
  `Aug  2 2022,14:29:06`.
- `??M*` reports full revision strings such as `10v20.0-R24`; the parsed
  version keeps family/major/minor for gates and preserves raw text for
  diagnostics.
- `DCU`, `LSS`, `NCB`, `NCS`, `ZCA`, and `ZCP` are V10 10v05+ command surfaces.
- `FPF` is numeric-family only; GP is gated out. 5v12 rejects it even though
  6v+ devices implement it, so per-device rejection remains possible.

### 5.11 Response Parsing Helpers

Shared helpers live in `protocol/parser.py`:

```python
def parse_ascii(raw: bytes) -> str: ...
def parse_fields(raw: str, *, expected_count: int | None = None, command: str) -> list[str]: ...
def parse_float(value: str, *, field: str) -> float: ...
def parse_int(value: str, *, field: str) -> int: ...
def parse_optional_float(value: str, *, field: str) -> float | None: ...
def parse_bool_code(value: str, *, field: str, mapping: Mapping[str, bool]) -> bool: ...
def parse_data_frame(raw: bytes, fmt: DataFrameFormat) -> ParsedFrame: ...
def parse_data_frame_table(lines: Sequence[bytes]) -> DataFrameFormat: ...
def parse_manufacturing_info(lines: Sequence[bytes]) -> ManufacturingInfo: ...
def parse_ve_response(raw: bytes) -> tuple[FirmwareVersion, date | None]: ...
def parse_valve_drive(raw: bytes) -> ValveDrive: ...
def parse_status_codes(tokens: Sequence[str]) -> frozenset[StatusCode]: ...
def parse_gas_list(lines: Sequence[bytes]) -> dict[int, str]: ...
```

Rules:

- `--` normalizes to `None`.
- Missing or extra fields raise `AlicatParseError` unless a parser explicitly
  allows the shape.
- Unit-ID mismatches raise `AlicatUnitIdMismatchError`.
- Raw responses are preserved in error context.
- GP `\x08` padding is stripped before line-shape parsing.
- GP single-line gas lists are split into pseudo-lines before normal gas-list
  parsing.

### 5.12 Discovery

```python
@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    port: str
    unit_id: str
    baudrate: int
    info: DeviceInfo | None
    error: AlicatError | None

async def list_serial_ports() -> list[str]: ...
async def probe(port: str, *, unit_id: str = "A", baudrate: int = 19200, timeout: float = 0.2) -> DiscoveryResult: ...
async def find_devices(
    ports: Iterable[str] | None = None,
    *,
    unit_ids: Sequence[str] = ("A",),
    baudrates: Sequence[int] = (19200, 115200),
    timeout: float = 0.2,
    max_concurrency: int = 8,
) -> tuple[DiscoveryResult, ...]: ...
```

Discovery tries multiple baud rates because hardware captures showed real units
configured at both 19200 and 115200. Results return successes and failures; the
library does not print diagnostics.

### 5.13 Multi-Device Manager

`AlicatManager` owns multiple devices and coordinates concurrent calls.

```python
class AlicatManager:
    async def add(self, name: str, source: Device | str | Transport | AlicatProtocolClient, *, unit_id: str = "A", serial: SerialSettings | None = None) -> Device: ...
    async def remove(self, name: str) -> None: ...
    def get(self, name: str) -> Device: ...
    async def poll(self, names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[DataFrame]]: ...
    async def request(self, stats: Sequence[Statistic | str], names: Sequence[str] | None = None) -> Mapping[str, DeviceResult[MeasurementSet]]: ...
    async def execute(self, command: Command[Req, Resp], requests_by_name: Mapping[str, Req]) -> Mapping[str, DeviceResult[Resp]]: ...
```

Concurrency rules:

- Different physical ports run concurrently.
- Same-port devices share the port lock.
- `ErrorPolicy.RAISE` raises an `ExceptionGroup` after collecting failures.
- `ErrorPolicy.RETURN` returns `DeviceResult` for every device.

The manager uses `AsyncExitStack` for resource lifecycle. Port-sharing keys are
canonicalized so symlinked POSIX paths and differently cased Windows COM names
do not accidentally create two clients for one physical port.

### 5.14 Acquisition and Samples

The recorder emits timed batches and does not own storage.

```python
@dataclass(frozen=True, slots=True)
class Sample:
    device: str
    unit_id: str
    monotonic_ns: int
    requested_at: datetime
    received_at: datetime
    midpoint_at: datetime
    latency_s: float
    frame: DataFrame

@asynccontextmanager
async def record(
    manager: AlicatManager,
    *,
    stats: Sequence[Statistic | str] | None = None,
    rate_hz: float,
    duration: float | None = None,
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
) -> AsyncIterator[AsyncIterator[Mapping[str, Sample]]]: ...
```

Scheduling uses `anyio.current_time()` and `anyio.sleep_until()` with absolute
targets. Wall-clock timestamps are captured separately for provenance. If a
cycle overruns, the recorder increments late-sample counters and skips missed
slots rather than drifting.

`record()` is an async context manager that yields a receive stream. Its task
group is strictly nested inside the context manager to preserve AnyIO
structural-concurrency rules.

### 5.15 Sinks

Sinks split into two buckets: **stdlib-backed** (included with the core
install) and **extras-backed** (optional dependencies behind
`alicatlib[parquet]` / `alicatlib[postgres]`).

```python
class SampleSink(Protocol):
    async def open(self) -> None: ...
    async def write_many(self, samples: Sequence[Sample]) -> None: ...
    async def close(self) -> None: ...

async def pipe(
    stream: AsyncIterator[Mapping[str, Sample]],
    sink: SampleSink,
    *,
    batch_size: int = 64,
    flush_interval: float = 1.0,
) -> AcquisitionSummary: ...
```

First-party sinks:

- `InMemorySink` for tests.
- `CsvSink` using stdlib CSV.
- `JsonlSink` using stdlib JSON.
- `SqliteSink` using stdlib `sqlite3` (WAL + `synchronous=NORMAL` by default).
- `ParquetSink` through `alicatlib[parquet]`; `zstd` is the default codec.
- `PostgresSink` through `alicatlib[postgres]`; binary COPY by default,
  prepared `executemany` as an opt-out fallback.

Cross-cutting contract for all tabular sinks:

- **First-batch schema lock.** Column set and order are inferred from the
  first `write_many`; subsequent unknown columns are dropped with a
  one-shot WARN. Missing columns materialise as `NULL`. Shared helper
  lives at `alicatlib.sinks._schema.SchemaLock`.
- **Identifier validation.** SQL identifiers (SQLite table name,
  Postgres `schema.table`) must match `^[A-Za-z_][A-Za-z0-9_]{0,62}$`;
  SQL values always pass through placeholders (`?` / `$N`), never
  string formatting.
- **Credentials never logged.** `PostgresConfig` scrubs passwords from
  DSNs before emitting any log record (unit-tested).
- **Optional extras stay optional.** `ParquetSink` and `PostgresSink`
  defer their backing-library import to `open()`, so `from
  alicatlib.sinks import ParquetSink` succeeds on a bare-core install
  and raises `AlicatSinkDependencyError` only when a connection is
  actually needed.

Durability caveat on `ParquetSink`: Parquet files are not readable
until the footer is flushed on `close()`. The recommended shutdown
path is the recorder's structured exit (which always reaches the
sink's async-context-manager `__aexit__`); an unclean termination
leaves a zero-byte-footer file.

### 5.16 Sync Facade

Async is canonical. Sync uses `anyio.from_thread.BlockingPortal`.

```python
from alicatlib.sync import Alicat

with Alicat.open("/dev/ttyUSB0") as dev:
    print(dev.poll())
    dev.setpoint(50.0, "SCCM")
```

Each sync device or manager owns a portal scoped to its context manager by
default. Shared portals are opt-in for advanced users. Sync wrappers are
explicit, hand-written methods; parity tests compare sync and async signatures.

### 5.17 Errors

```python
class AlicatError(Exception): ...
class AlicatConfigurationError(AlicatError): ...
class UnknownGasError(AlicatConfigurationError): ...
class UnknownFluidError(AlicatConfigurationError): ...
class UnknownUnitError(AlicatConfigurationError): ...
class UnknownStatisticError(AlicatConfigurationError): ...
class InvalidUnitIdError(AlicatConfigurationError): ...
class AlicatValidationError(AlicatConfigurationError): ...
class AlicatMediumMismatchError(AlicatConfigurationError): ...

class AlicatTransportError(AlicatError): ...
class AlicatTimeoutError(AlicatTransportError): ...
class AlicatConnectionError(AlicatTransportError): ...

class AlicatProtocolError(AlicatError): ...
class AlicatParseError(AlicatProtocolError): ...
class AlicatCommandRejectedError(AlicatProtocolError): ...
class AlicatStreamingModeError(AlicatProtocolError): ...
class AlicatUnitIdMismatchError(AlicatProtocolError): ...

class AlicatCapabilityError(AlicatError): ...
class AlicatUnsupportedCommandError(AlicatCapabilityError): ...
class AlicatFirmwareError(AlicatCapabilityError): ...
class AlicatMissingHardwareError(AlicatCapabilityError): ...

class AlicatSinkError(AlicatError): ...
class AlicatSinkDependencyError(AlicatSinkError, AlicatConfigurationError): ...
class AlicatSinkSchemaError(AlicatSinkError): ...
class AlicatSinkWriteError(AlicatSinkError): ...
```

`AlicatSinkDependencyError` multi-inherits `AlicatConfigurationError`
so callers who already branch on configuration errors keep working
when an extra is missing (that is a configuration problem, from the
caller's perspective).

Every error carries an `ErrorContext` with fields for command name, raw command,
raw response, unit ID, port, firmware, device kind, device medium, command
medium, and elapsed time where available.

### 5.18 Configuration

Core config is a plain dataclass:

```python
@dataclass(frozen=True, slots=True)
class AlicatConfig:
    default_timeout_s: float = 0.5
    multiline_timeout_s: float = 1.0
    write_timeout_s: float = 0.5
    default_baudrate: int = 19200
    drain_before_write: bool = False
    save_rate_warn_per_min: int = 10
    eager_tasks: bool = False
```

`config_from_env(prefix="ALICATLIB_")` reads only known keys.

Postgres config is also a plain dataclass and is imported only with the
Postgres extra.

### 5.19 Observability

Logger tree:

- `alicatlib`
- `alicatlib.transport`
- `alicatlib.protocol`
- `alicatlib.session`
- `alicatlib.commands`
- `alicatlib.streaming`
- `alicatlib.sinks.<name>`

Rules:

- No `print` in library code.
- No operational `warnings.warn`; warnings are for deprecations.
- The library never configures root handlers.
- Debug logs may include raw bytes.
- Info logs avoid payload dumps by default.
- Structured `extra` fields include device name, unit ID, port, command, elapsed
  time, and raw bytes where appropriate.

### 5.20 Safety

Safety rules:

1. Destructive operations require `confirm=True`.
2. Setpoint writes validate source mode, sign, and known range constraints
   before I/O.
3. Commands declare required capabilities; missing capability raises before I/O.
4. Commands declare medium; mismatches raise before I/O.
5. Command encoders validate documented numeric ranges.
6. Unsupported firmware or kind paths do not fall back silently.
7. Tare methods document physical preconditions that the library cannot verify.
8. Commands with `save=True` are monitored for EEPROM-wear patterns and warn
   above the configured per-minute threshold.
9. Stream mode refuses to run on a multi-device port.
10. Credentials are never logged or embedded.
11. SQL values are parameterized and identifiers are validated.
12. Hardware tests are tiered so destructive tests require explicit opt-in.

Medium override note: `assume_media` can narrow or widen the medium flags for a
specific session because it replaces the prefix-derived value. It should be used
only when the caller knows the device's configured medium.

## 6. Testing Strategy

### 6.1 Layers

1. Pure unit tests:
   - Command encode/decode, including `0`, `False`, and omitted arguments.
   - Registry coercion and suggestions.
   - Firmware parsing and comparison.
   - Parser helpers and malformed-input paths.
   - Registry invariants and codegen idempotency.

2. FakeTransport integration:
   - Session serialization and port sharing.
   - Firmware, kind, medium, capability, and safety gates.
   - Timeouts and error context.
   - Multiline reads and drain-on-error behavior.
   - Manager policies and recorder scheduling.
   - CSV/JSONL sinks.

3. Hardware-in-the-loop:

   ```python
   @pytest.mark.hardware
   @pytest.mark.hardware_stateful
   @pytest.mark.hardware_destructive
   @pytest.mark.hardware_gp
   ```

   Hardware tests are skipped by default in CI. Read-only tests open, identify,
   poll, and close. Stateful tests restore gas, units, setpoint, and unit ID in
   teardown. Destructive tests require explicit environment opt-in.

4. Property-based tests:
   - Parser fuzzing.
   - Encode/decode round-trips where the protocol shape permits.

### 6.2 Fixtures

Fixtures under `tests/fixtures/responses/` are readable send/receive scripts:

```text
# scenario: identify-flow-controller
> A??M*
< A M00 Alicat Scientific
< A M01 ...
```

The testing helpers load these into `FakeTransport` and can record new captures
from hardware runs.

### 6.3 Performance Suite

Performance tests are non-default:

- Single-device poll latency p50, p95, p99.
- Multi-device poll latency.
- Recorder jitter at 1, 10, 25, and 50 Hz.
- Sink throughput (CSV, JSONL, SQLite, Parquet; `scripts/bench_sinks.py`).
- Optional Postgres throughput against a real server.

### 6.4 CI Checks

- Ruff format and lint.
- `mypy --strict`.
- Non-hardware pytest on Python 3.13 and 3.14.
- AnyIO pytest plugin with asyncio and trio backends.
- Coverage threshold: 90% overall, 95% for protocol, registry, and commands.
- Build and metadata checks.
- Documentation build.
- Codegen idempotency.

## 7. Tooling and Packaging

- Build backend: `hatchling`.
- Environment and lock: `uv`.
- Python floor: 3.13, required by `anyserial` and parser typing choices.
- Core runtime dependencies: `anyio>=4.13`, `anyserial>=0.1,<0.2`.
- Optional extras:

```toml
[project.optional-dependencies]
postgres = ["asyncpg>=0.30"]
parquet = ["pyarrow>=16"]
docs = ["zensical", "mkdocstrings-python"]
dev = ["pytest", "pytest-cov", "hypothesis", "ruff", "mypy", "pre-commit"]
```

Avoided in core: `asyncpg`, `pandas`, `scipy`, `numpy`, `pyarrow`, `pydantic`,
and `pydantic-settings`.

## 8. Example Usage

### Polling

```python
async with open_device("/dev/ttyUSB0") as dev:
    frame = await dev.poll()
    mass = frame.get_statistic(Statistic.MASS_FLOW)
```

### Gas Selection

```python
await dev.gas(Gas.N2, save=True)
await dev.gas("N2", save=True)
```

### CSV Logging

```python
from alicatlib import AlicatManager
from alicatlib.sinks.base import pipe
from alicatlib.sinks.csv import CsvSink
from alicatlib.streaming import record

async with AlicatManager() as mgr:
    await mgr.add("fuel", "/dev/ttyUSB0")
    await mgr.add("air", "/dev/ttyUSB1")
    async with CsvSink("run.csv") as sink, record(mgr, rate_hz=10, duration=60) as stream:
        await pipe(stream, sink)
```

### Custom Consumer

```python
async with record(mgr, rate_hz=10, duration=60) as stream:
    async for batch in stream:
        await kafka.send(
            "flows",
            json.dumps({name: sample.frame.as_dict() for name, sample in batch.items()}),
        )
```

### Adding a Command

Add one command spec, one request dataclass, one response dataclass, one facade
method if needed, and one fixture-backed test. Firmware, kind, medium,
capability, and safety gates live on the command spec.

## 9. Command Coverage Tiers

### Tier 1: v1 Gas-Flow Path and Common Infrastructure

All kinds and media:

- Identification: `VE`, `??M*`, GP/pre-VE fallback.
- Firmware parsing.
- Data frame format query/cache: `??D*`.
- Polling.
- `request()` / `DV` once finalized.
- Engineering units.
- Unit ID and baud lifecycle.
- Manager poll/request.
- CSV and JSONL sinks.
- Fake transport and fixtures.

Gas flow:

- Gas query/set/list: modern `GS`, legacy `G`, `??G*`.
- Flow tare, gauge-pressure tare, absolute-pressure tare with capability gates.
- Setpoint query/set on controllers: modern `LS`, legacy `S`.
- Loop-control variable.

### Tier 2: Pressure, Liquid, and Non-Destructive Specialty

Pressure devices:

- Pressure-controller setpoint.
- Valve hold / cancel.
- Standard pressure and temperature references.

All devices:

- Totalizer.
- Zero band.
- Analog output.
- Streaming mode.
- Valve drive query.
- Ramp rate.
- Deadband limit.
- Display commands (blink / lock / unlock).
- User data.
- Auto-tare and power-up tare.

Liquid flow (future work, pending hardware capture — see §10 and
§13 Q2/Q4):

- Fluid query/set/list.
- Liquid flow tare.
- `LC-` / `LCR-` setpoint behavior.
- Per-fluid reference-density lookup.

Deadband mode (`LDM`/`LCDM`) and loop-control algorithm (`LCA`) are
also future work: the primer text disagrees with the quick-reference
table on the `LDM`/`LCDM` token, and changing the controller's loop
algorithm in-flight is operationally closer to Tier-3 "gain-tuning"
territory than to non-destructive specialty.

### Tier 3: Advanced, Destructive, and Ambiguous Hardware

- CODA K-family refinements.
- Pump-controller and pump-system behavior for `KF-` / `KG-`.
- Custom gas mix editing.
- Remote tare action maps.
- Factory restore.
- Controller gains.
- Batch mode.
- Overpressure limit.
- Power-up setpoint.
- Exhaust.
- Intrinsically safe product-line operational constraints.

Tier 3 commands carry `destructive=True` and/or `experimental=True` until
hardware-validated.

## 10. Future Work

The library ships the full transport, protocol, registry, device
identification, Tier-1 and Tier-2 command surfaces, multi-device
manager and recorder, all first-party sinks (CSV, JSONL, InMemory,
SQLite, Parquet, Postgres), the sync facade, and streaming mode.
Documentation completion and release prep are in progress.

The following items remain open and are tracked here rather than on the
main specification (§1–§9, which describes the design as-built).

### Liquid / fluid surface

`fluid()`, `fluid_list()`, liquid flow tare, `LC-` / `LCR-` setpoint
behavior, and per-fluid reference density all require a liquid-device
hardware capture to pin wire forms. Adding placeholder methods now
would commit a signature; deleting methods that never existed is
cleaner than changing ones that have. No scaffolding for the liquid
surface ships today. See §13 Q2 and Q4 for the unblock conditions.

### Deadband mode and loop-control algorithm

`LDM` / `LCDM` (deadband mode) and `LCA` (loop-control algorithm) are
deferred pending a primer reconciliation — the primer text disagrees
with the quick-reference table on the `LDM` / `LCDM` token. `LCA` is
operationally closer to Tier-3 gain-tuning than to non-destructive
specialty and will ship behind `experimental=True` once wire shapes are
captured.

### Tier-3 command surface

Tier-3 (advanced, destructive, and ambiguous hardware) is listed in §9.
These commands carry `destructive=True` and/or `experimental=True`
until hardware-validated. They are not in scope for v1.0.

### Hardware validation gaps

- **Pressure-controller parity** on real `PC-*` / `PCD-*` / `EPC-*` /
  `IVC-*` hardware. `_ControllerMixin` passes through to
  `FlowController` on the current bench; pressure-side behavior is
  unvalidated until a pressure controller arrives.
- **`ANALOG_OUTPUT`-advertising and `DISPLAY`-advertising hardware** for
  `ASOCV` / `FFP` / `L` / `U` capability-gate positive round-trips.
  Current bench devices hit the capability gate cleanly but a positive
  round-trip is untested.
- **Live Postgres soak** on production infrastructure. `PostgresSink`
  is currently covered by a protocol-satisfying asyncpg fake.

## 11. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| `anyserial` API instability | Pin `<0.2`, test through `SerialTransport`, and keep the transport interface isolated. |
| Scope creep delays v1 | Command coverage tiers separate v1 gas-flow path from later pressure/liquid/destructive work. |
| Wrong-medium command reaches hardware | `Command.media` and `DeviceInfo.media` gate pre-I/O. |
| CODA medium classification wrong | Default widest; user narrows with `assume_media`; hardware verification remains open. |
| Pressure-device liquid support underclassified | Only PCD `S` closed-volume variants are widened today; flowing-process PC/P devices await fluid-select verification. |
| Typed frames surprise users | `as_dict()`, `get_float()`, and `get_statistic()` provide raw and typed access. |
| `codes.json` drift | Deterministic generator and CI check. |
| Streaming disrupts polling bus | Client state machine and one-streamer-per-port rule. |
| Hardware capability is absent or ambiguous | Capability gates, probe reports, and `assume_capabilities` for user-known hardware. |
| Setpoint source ignores serial writes | `LSS` state blocks serial setpoint writes when source is analog/user knob. |
| Baud or unit-ID changes orphan state | Bounded reconciliation, verification, and broken-session state. |
| Multiline replies bleed into next command | Completion predicates and drain-on-error behavior. |
| GP command prefix assumptions are wrong | Prefix is metadata per command; reads can opt out via `prefix_less=True`. |

## 12. Documentation Plan

Pages under `docs/`:

- `index.md`: purpose and quickstart.
- `installation.md`: core install and extras.
- `quickstart-async.md`: open, poll, request, setpoint.
- `quickstart-sync.md`: sync facade.
- `devices.md`: supported models, media, firmware notes.
- `commands.md`: command groups and return models from the catalog.
- `data-frames.md`: dynamic data-frame formats and `Statistic` linkage.
- `logging.md`: recorder, sinks, and backpressure.
- `streaming.md`: streaming mode and state transitions.
- `testing.md`: fake transport, fixtures, hardware test tiers.
- `safety.md`: destructive commands, confirmation, tare preconditions.
- `troubleshooting.md`: ports, baud rates, timeouts, stale input, permissions.
- `api/`: generated reference.

## 13. Open Questions

Open or partially open:

1. CODA medium semantics: do all K-family devices support both media, and is
   there a device-readable configured-medium signal?
2. Flowing-process `PC-*` and `P-*` liquid operation: do these devices expose
   fluid-select commands or only liquid-compatible wetted materials?
   Resolution gates the liquid surface (see §10); together with Q4, these
   are the liquid-surface unblock conditions.
3. BASIS auxiliary fields: capture enough variants to pin part-number fields and
   data-frame behavior.
4. Fluid registry: capture liquid-fluid lists, per-fluid reference densities,
   and the wire shape of fluid-select / fluid-list commands. This is the
   primary gate on the liquid surface — without a capture, signatures for
   `fluid()` / `fluid_list()` would be guesses (see §10).
5. Capability probes: finalize safe probes for secondary pressure, totalizer,
   display, analog I/O, bidirectional behavior, and multi-valve hardware.
6. ✓ Resolved: `DV` works on V8/V9 with the same wire shape as V10, and
   the absent-value sentinel is a run of dashes of arbitrary column
   width (encoded in the `REQUEST_DATA` decoder, see §15.3).
7. ✓ Resolved: the 5-field `LS` reply (current / requested / unit_code /
   unit_label) is a V10/9v00+ feature. On V8/V9 below 9v00 and on
   V1_V7/GP the library correctly gates `LS` pre-I/O and routes
   `setpoint()` through the legacy `S` path; the post-op data-frame
   decoder finds the setpoint column by `*_SETPT` statistic rather than
   the primer's placeholder name "Setpoint" (§15.3).
8. ✓ Resolved: streaming ships with v1. `StreamingSession` is the
   state-transition runtime; `record()` remains the software-polling
   primitive. Both compose with sinks independently.
9. Acquisition rates and fleet sizes: use real targets to choose recorder
   buffer defaults.
10. ✓ Resolved: plain Postgres for v1. `PostgresSink` uses
    stock `CREATE TABLE` / `INSERT` / binary `COPY`. Timescale
    hypertable helpers are deferred post-v1 — creating a hypertable
    requires operational opinions (chunk interval, retention policy,
    compression schedule) that a library shouldn't pick. The sink's
    identifier/column layout is compatible with a later
    Timescale-aware wrapper, so no migration is implied.
11. CLI scope: decide whether `python -m alicatlib discover` and `poll` belong in
    v1 or post-v1.

## 14. Success Criteria

The v1 implementation is done when:

- Adding a command is localized to a command spec, request/response models,
  optional facade method, and fixture-backed tests.
- At least 95% of tests run without hardware.
- Timeouts, malformed replies, rejected commands, firmware errors, medium
  mismatches, and missing hardware are distinct typed exceptions.
- Same-port requests cannot interleave bytes.
- A 10 Hz recorder can run for an hour without drift exceeding one sample
  period under default backpressure.
- `0`, `False`, and `None` remain distinct in every encoder.
- Multiline responses cannot leave stale bytes for the next command.
- Data-frame format is cached and linked to `Statistic`.
- Optional dependencies stay optional.
- CI enforces formatting, linting, typing, tests, docs, wheel build, and codegen.
- Destructive operations require `confirm=True`.
- Hardware tests are safety-tiered.
- Firmware comparisons across families are rejected.
- GP devices route through prefix rules automatically.
- Pre-VE / pre-standard identification works through `??M*` or `model_hint`.
- Unit ID and baud changes succeed coherently or leave the session explicitly
  broken.
- Stale streaming recovery runs by default.
- Conditional data-frame fields parse correctly.
- Hardware-required commands raise before I/O when capability is absent.

---

## 15. Appendix: Implementation Notes

This appendix captures implementation choices that are too detailed for the
main architecture but important enough to preserve. The main specification above
is authoritative; these notes explain why certain boundaries, gates, and parser
tolerances exist.

### 15.1 Module Boundaries

- `DeviceKind` lives in `devices/kind.py` so commands can import it without
  importing device facades.
- `Medium` lives in `devices/medium.py` for the same reason.
- `devices/__init__.py` stays minimal to avoid load-order cycles.
- ASCII framing helpers live below parser/data-frame modules to avoid circular
  imports.
- ASYNC109 is disabled per-file where explicit `timeout` parameters are part of
  the public protocol contract.

### 15.2 Implementation History and Deferrals

- `REQUEST_DATA` / `DV`. Implementation against
  `tests/fixtures/responses/request_data_dv.txt`; the wire shape is
  `<uid>DV <time_ms> <stat1> [stat2...]` out, `<val1> [val2...]` back with no
  unit-ID prefix (unique in the catalog). Invalid statistics map to `None`
  per slot via the `--` sentinel; zero time rejects pre-I/O with an
  `AlicatValidationError`.
- Structured observability for set-events. Setpoint, `LSS`, and `LV`
  write paths emit a single `alicatlib.session` INFO per call with
  `{unit_id, command, value/mode/variable}`. Capability probing emits
  one INFO summary per identification (GP skip or per-flag outcomes).
  Query forms stay silent.
- `FirmwareVersion.raw` preserves the `.N-R<NN>` revision suffix across
  VE replies (5v12.0-R22, 8v17.0-R23, 10v20.0-R24 and similar). Sinks
  surface the full string to downstream observability; gating still
  reads only `major` / `minor`.
- `probe_capabilities`: partially implemented; barometer and secondary
  pressure probe via `FPF`, other flags still fail-closed pending a
  hardware-validated probe strategy.
- Protocol-level DEBUG raw-bytes trace. `AlicatProtocolClient` emits
  one `tx` event per write and one `rx` event per read under the
  `alicatlib.protocol` logger, each carrying structured
  `{direction, raw, len}` extras. Both sites are guarded by
  `isEnabledFor(DEBUG)` so the repr cost is paid only when a handler
  subscribes.
- Optional Parquet and Postgres sinks. `ParquetSink` uses pyarrow with
  zstd compression by default (codec A/B in `docs/benchmarks.md`
  showed zstd beats snappy on size by ~16 % at ~10 % throughput cost;
  acquisition rates are well below either codec's ceiling, so size
  wins). `PostgresSink` pools asyncpg connections, defaults to binary
  `COPY`, validates identifiers, and scrubs passwords before any log
  line. Schema-lock behaviour is shared across all tabular sinks via
  `alicatlib.sinks._schema`.
- Stdlib SQLite sink. `SqliteSink` runs stdlib `sqlite3` via
  `anyio.to_thread.run_sync` with WAL + `synchronous=NORMAL` +
  `busy_timeout=5000` ms defaults, one `BEGIN IMMEDIATE` / `COMMIT`
  per batch, and the same schema-lock contract as the other tabular
  sinks. Zero-dep complement to CSV/JSONL; no extra is required.
- Automatic `DataFrameField.unit` binding. The `??D*` parser binds a
  unit inline when the reply carries a recognisable label; the factory
  then runs a `DCU` sweep for every numeric field whose unit is still
  `None`, rebuilding the format with the resolved unit where one comes
  back. Per-field failures (firmware gate, rejection, timeout) leave
  the slot unresolved rather than failing the open.
- Setpoint full-scale range validation. The factory issues `FPF` for
  every numeric `DataFrameField` and populates `DeviceInfo.full_scale`;
  controller-kind sessions also pre-cache the loop-control variable
  via `LV`. `FlowController.setpoint` consults
  `full_scale[lv.statistic]` pre-I/O and raises
  `AlicatValidationError` when the request is outside `[−fs, +fs]`
  (bidirectional) or `[0, +fs]` (unidirectional). Either cache
  missing → the range check is skipped; the `BIDIRECTIONAL` gate still
  fires first for negative values on unidirectional hardware.
- Streaming runtime. `StreamingSession` owns the state transition, the
  client carries an `is_streaming` latch that the dispatch gate reads,
  and the stop-stream bytes match the factory's stale-stream recovery
  wire form by construction. Parse errors log + skip under
  `strict=False` and propagate under `strict=True`; `__aexit__` always
  writes stop-stream, even on body raise.
- Pressure-controller parity. The controller-kind surface (`setpoint`
  / `setpoint_source` / `loop_control_variable` / `hold_valves` /
  `hold_valves_closed` / `cancel_valve_hold` / `valve_drive` /
  `ramp_rate` / `deadband_limit`) lives on a private
  `_ControllerMixin(Device)`; `FlowController` and `PressureController`
  inherit `(MeterParent, _ControllerMixin)` and share every method
  body. Sync side follows the same pattern via
  `_SyncControllerMixin`, so the 12+ method pairs cost one
  implementation each.
- Non-destructive all-device specialty. `DCZ` / `DCA` / `DCFRP` /
  `DCFRT` (data_readings), `ASOCV` (analog output, capability-gated),
  `FFP` / `L` / `U` (display, capability-gated), `UD` (user data, with
  ASCII / length / `\r` / `\n` validation), `ZCA` (auto-tare on
  controllers) and `ZCP` (power-up tare, all devices). All ten facade
  methods have sync wrappers and parity entries.
- Totalizer surface. `TC` (config), `T <n>` (reset, destructive),
  `TP <n>` (reset peak, destructive), `TCR` (save). Token-collision
  protection is load-bearing: the two reset encoders always emit the
  numeric totalizer argument so the wire form can never degrade into
  bare `T\r` / `TP\r`, which are reserved for `TARE_FLOW` /
  `TARE_GAUGE_PRESSURE` respectively. The invariant is pinned by
  dedicated unit tests.
- Future work: liquid / fluid surface. `fluid()`, `fluid_list()`,
  liquid flow tare, `LC-` / `LCR-` setpoint semantics, and per-fluid
  reference density all require a liquid-device hardware capture to
  pin wire forms. See §10 for the no-scaffolding rationale.

### 15.3 Hardware-Corrected Design Choices

- `??M*` uses canonical M00-M09 lines on numeric families and a GP-specific
  M0-M8 dialect on GP07R100.
- `??D*` has `DEFAULT` and `LEGACY` dialects; detection is by header, not
  firmware family.
- `DataFrame.as_dict()` uses one `status` key.
- `POLL_DATA.decode` returns `ParsedFrame`; `Session.poll()` wraps timing.
- Manufacturing-info parsing keeps raw code mappings; named field extraction is
  factory-owned.
- Stream recovery writes raw `@@ {unit_id}\r` before a session exists.
- The bootstrap session uses a permissive placeholder only long enough to run
  identification commands, then replaces it with real firmware.
- Fixture payloads are ASCII even when comments are UTF-8.
- Streamed data frames drop the leading unit-id letter (empirically a
  space on 10v20; the primer's "unit id becomes `@`" text allows a
  literal `@` in the same slot). The `StreamingSession` producer
  prepends the session's unit id before `DataFrameFormat.parse` runs
  so the single parse path handles both the request/response and
  streaming shapes.
- `DV` encodes an absent-value sentinel as a pure run of dashes
  sized to the statistic's display-column width; the decoder treats
  any pure-dash token (length ≥ 2) as the absent marker, not only
  the primer's `--`.
- `VD` reply is a fixed-width four-column line on captured V10 /
  V8+ controllers (extra columns zero on single-valve hardware); the
  decoder accepts 1–4 percentages. Physical valve count is a
  capability question, not a column-count one.
- `DCA` reply is `<uid> <averaging_ms>` (2 fields) on real 10v20; the
  primer documents a 3-field shape with an echoed statistic code. The
  decoder accepts both; the facade re-populates the statistic from the
  request.
- `UD` reply on an empty slot is just `<uid>` (1 field after
  whitespace split) on real 10v20; the decoder returns `slot=-1` as a
  sentinel and the facade refills `slot` from the request.
- `TC` reply echoes the totalizer id as the second token on real 10v20
  (7 fields total); primer documents 6. The decoder accepts both shapes
  and drops the echoed id.
- The setpoint column in a post-op data frame is named after the
  controlled variable (`Mass_Flow_Setpt`, `Gauge_Press_Setpt`, …), not
  the placeholder `Setpoint` used in fake-transport fixtures. Legacy
  `S` decoding locates the column by `*_SETPT` statistic membership
  first, with a name-based fallback for fixtures without statistic
  codes.
- `sample_to_row` drops the frame's unit-id echo (any casing of
  `unit_id` / `Unit_ID`) before merging frame values onto the row;
  `sample.unit_id` is authoritative and SQLite's case-insensitive
  column matching rejects the duplicate otherwise.
- Factory stream-recovery (`_recover_from_stream`) caps its passive
  `read_available` sniff and post-stop drain at 256 bytes. The
  uncapped form deadlocks `open_device` when the device is
  continuously streaming at its 50 ms default rate — the bus
  never goes idle for the 100 ms window `read_available` waits
  for. The sniff result is telemetry-only; the cap is cheap
  insurance.
- `SyncStreamingSession` enters/exits the underlying async
  `StreamingSession` via `SyncPortal.wrap_async_context_manager`,
  not `portal.call(__aenter__)`. `portal.call` wraps each call in
  its own `CancelScope`; `StreamingSession.__aenter__` enters a
  long-lived task group that outlives the entry call, so the
  nested scope hierarchy becomes inconsistent at exit and raises
  `RuntimeError: Attempted to exit a cancel scope that isn't the
  current task's current cancel scope` on real hardware.
  `wrap_async_context_manager` lets anyio own the portal-side
  scope for the full CM lifetime.
- `AUTO_TARE` (``ZCA``) disable form emits ``ZCA 0`` with no delay
  field on the wire, not the primer's ``ZCA 0 0``. Confirmed on two
  10v20 units that the primer form rejects with ``?``; the wire-form
  probe found ``ZCA 0`` is the shortest accepted shape. The reply is
  always 3-field (``<uid> 0 0.0``) regardless of which disable form
  was sent.
- `UNLOCK_DISPLAY` is intentionally NOT gated on
  `Capability.DISPLAY`, unlike `LOCK_DISPLAY` and `BLINK_DISPLAY`.
  It is the safety escape for a locked device, and must always be
  callable. V1_V7 firmware parses any command starting with
  `AL<X>` (including `ALS` / `ALSS` / `ALV`, which the library
  firmware-gates pre-I/O under normal use) as "lock display with
  argument X" and sets the `LCK` status bit; third-party code or
  direct `session.execute` can trip this, so `dev.unlock_display()`
  must not be blocked by a probe that failed closed on `DISPLAY`.
  The library already confirmed `AU` works on V1_V7 (7v09) / V8/V9
  / V10; on a device without a physical display it is a harmless
  no-op.

### 15.4 Remaining Deferrals

- Tare result timing is facade-captured, not read-site captured.
  Exact-to-the-byte timing would require plumbing callbacks into
  the protocol client; deferred until a real need surfaces
  (design §5.6). `ValveHoldResult`, `DisplayLockResult`, and
  `TotalizerResetResult` land on the same pattern — any future
  read-site-timing overhaul lifts them together.
- Hardware fixture replacement is ongoing as new devices are captured.

## 16. Appendix: Hardware Validation

Hardware evidence is kept here so the main design can stay readable. The
appendix records the devices, sessions, wire-shape findings, and remaining
coverage gaps that back the architecture decisions above.

### 16.1 Verification TODOs

Still to verify:

1. CODA K-family medium behavior and CODA-specific statistics.
2. Flowing-process `PC-*` and `P-*` fluid-select behavior.
3. BASIS auxiliary fields and data-frame variants.
4. Liquid fluid-list command and fluid registry contents — gates the
   liquid surface (see §10).
5. Medium-mismatch pre-I/O behavior on gas-only, liquid-only, and dual-capable
   devices.
6. Safe capability probes beyond barometer/full-scale — in particular
   `ANALOG_OUTPUT` (ASOCV) and `DISPLAY` (FFP / L / U) round-trips on
   advertising hardware.
7. Pressure-controller parity on real `PC-*` / `PCD-*` / `EPC-*` /
   `IVC-*` hardware (`_ControllerMixin` is shared with
   `FlowController`; the pressure side is not yet validated against
   real pressure-controller hardware).
8. ✓ Resolved: primer's ``ZCA <uid> 0 0`` disable form rejects with
   ``?`` on real 10v20 (both captured units). The wire-form probe
   found ``ZCA 0`` with no delay field is the shortest accepted
   disable shape; the encoder emits that form for ``enable=False``
   (§15.3).
9. V1_V7 / pre-9v00 V8_V9 / GP setpoint-source state: no `LSS`
   command exists on these firmware families, so the library cannot
   probe or cache the setpoint source. The analog-source
   silent-no-op risk is documented on `setpoint()` and in
   `docs/safety.md`; hardware validation confirmed there is no
   passive discriminator signal in the reply shape or status bits,
   only a destructive behavioral probe (write + read-back + compare),
   which is not worth baking into the open path.

Resolved on 2026-04-17 hardware sessions (see §16.6):

- VE month-name date format.
- `??M*` dialects.
- `??D*` `DEFAULT` and `LEGACY` dialects.
- GP read/write prefix split.
- Barometer vs tareable absolute pressure split.
- Unit labels such as `PSIA`, `PSIG`, `PSID`, and backtick temperature labels.
- `DV` across V10 (pre- and post-10v05), V8/V9, V1_V7 (DEFAULT + LEGACY
  dialects), and meter — including the arbitrary-width dash sentinel.
- Five-field `LS` is a V10 / 9v00+ feature; older firmware routes
  through the legacy `S` set-only path.
- Every Tier-2 command on real hardware (streaming, valve, control
  setup, data readings, analog output capability gate, display
  capability gate, user data, automated tare, totalizer).

### 16.2 Hardware Test Commands

Read-only smoke:

```bash
ALICATLIB_TEST_PORT=/dev/ttyUSB0 \
ALICATLIB_TEST_BAUD=115200 \
uv run pytest -m hardware tests/integration/ -v -s
```

GP smoke:

```bash
ALICATLIB_TEST_PORT=/dev/ttyUSB2 \
ALICATLIB_TEST_MODEL_HINT=MC-100SCCM-D \
uv run pytest -m hardware_gp tests/integration/ -v -s
```

Quick poll loop:

```bash
ALICATLIB_TEST_POLL_COUNT=10 \
ALICATLIB_TEST_PORT=/dev/ttyUSB0 \
uv run pytest -m hardware tests/integration/test_hardware_read_only.py -v -s
```

Stateful and destructive tests require explicit environment opt-ins documented
in `docs/testing.md`.

### 16.3 Commands Awaiting More Hardware

- `ASOCV` (analog output) and `FFP` / `L` / `U` (display) round-trip
  against hardware that actually advertises `ANALOG_OUTPUT` /
  `DISPLAY`. The bench-wide capability gate fires cleanly on every
  captured device, but a positive acknowledgement path is untested.
- Pressure-controller parity: `setpoint` / `setpoint_source` /
  `loop_control_variable` / `ramp_rate` / `deadband_limit` /
  `valve_drive` / `hold_valves` on real `PC-*` / `PCD-*` / `EPC-*` /
  `IVC-*` hardware. `_ControllerMixin` is shared with
  `FlowController`, so the facade compiles; the wire-level reply
  shapes for pressure setpoints (`Gauge_Press_Setpt` /
  `Abs_Press_Setpt` columns) have not been seen on real frames.
- Secondary-pressure, totalizer, remote-tare, and bidirectional
  capability probes — currently fail-closed.
- Positive `SR` on V1_V7 ≥ 7v11 — every captured V1_V7 device is
  pre-7v11 so the firmware gate is exercised cleanly but the
  family-admission path is not.
- Data-frame flavor detection on any future non-DEFAULT/non-LEGACY dialect.
- Liquid commands and fluid registry (blocks the liquid surface — see §10).
- CODA/pump-controller refinements.

### 16.4 Fixture Refresh Checklist

Keep filenames stable where possible:

- `ve_v10.txt`, `ve_v8.txt`, `ve_gp.txt`.
- `manufacturing_info_*.txt`.
- `dataframe_format_*.txt`.
- `poll_*.txt`.
- `gas_select_n2.txt`, `gas_select_legacy_n2.txt`.
- `gas_list_*.txt`.
- `engineering_units_*.txt`.
- `full_scale_*.txt`.
- `tare_flow_*.txt`, `tare_gauge_pressure_*.txt`, `tare_absolute_pressure_*.txt`.
- `setpoint_query_*.txt`, `setpoint_set_*.txt`, `setpoint_legacy_set_*.txt`.
- `setpoint_source_*.txt`.
- `loop_control_variable_*.txt`.
- `request_data_dv.txt`.
- `identify_*_happy.txt`.

Each fixture should include device model, firmware, baud rate, unit ID, adapter,
and capture date.

### 16.5 Known-Good Invariants

Preserve these when hardware findings require parser or command changes:

- Pre-I/O gates produce no transport writes.
- Error context is populated at the session layer.
- `0`, `None`, and `False` remain distinct.
- Frozen dataclasses are replaced, not mutated.
- Stream recovery writes stop-stream before identification only when buffered
  bytes indicate stale streaming.
- Same-port requests serialize.
- GP read commands that are prefix-less remain prefix-less.

### 16.6 Hardware-Day Findings

Ten devices were exercised across four sessions on 2026-04-17
(§16.6.1–§16.6.9) covering identification, command, and parser
validation; a fifth session on the same date (§16.6.10) covered the
acquisition, sink, sync-facade, streaming, and Tier-2 specialty
surface on real hardware. The table below summarises the
identification and command-surface findings; later sessions extend
the same devices with acquisition, sink, streaming, and Tier-2
observations.

| Device | Firmware | Family | Notes |
| --- | --- | --- | --- |
| MC-100SCCM-D | GP07R100 | GP | GP dialects; writes require `$$`; reads are prefix-less. |
| MC-500SCCM-D | 5v12.0-R22 | V1_V7 | `LEGACY` `??D*`; `??M*` works; FPF rejects. |
| MCR-775SLPM-D | 6v21.0-R22 | V1_V7 | `DEFAULT` `??D*`; FPF works; `??G*` rejects; rename ack race fixed. |
| MCP-50SLPM-D | 7v09.0-R22 | V1_V7 | `DEFAULT` `??D*`; `??G*` works; barometer present. |
| MCR-200SLPM-D | 8v17.0-R23 | V8_V9 | 115200 baud; `??M*` works below documented floor; PC rejects despite barometer. |
| MCR-500SLPM-D | 8v30.0-R23 | V8_V9 | Confirms `DEFAULT` dialect and FPF behavior. |
| MC-5SLPM-D | 10v03.0-R24 | V10 | First pre-10v05 V10 controller; `LS`/`LV` work, 10v05+ commands gate. |
| MW-10SLPM-D | 10v04.0-R24 | V10 | Meter; pre-10v05 lacks modern gas/unit/source commands. |
| MC-500SCCM-D | 10v20.0-R24 | V10 | Full modern Tier-1 surface validated; DV and LS divergence captured. |
| MC-5SLPM-D | 10v20.0-R24 | V10 | Regression pass after fixes. |

Consolidated findings:

| Finding | Confidence | Design impact |
| --- | --- | --- |
| `??M*` canonical numeric dialect is M00-M09 with embedded labels. | High | Parser and factory map named fields from code table. |
| `??M*` works on V1_V7, V8/V9, V10, and GP. | High, with GP dialect split | Identification tries it more broadly than the primer suggests. |
| GP `??M*` uses M0-M8, shorter labels, and `\x08` padding. | GP07R100 only but supported | Parser strips padding and factory detects GP dialect. |
| `??D*` `DEFAULT` dialect is used from 6v21 onward in captured devices. | High | Header sniffing selects parser. |
| `??D*` `LEGACY` dialect appears on 5v12 and GP. | Medium | Separate parser flavor. |
| VE dates use month-name format in captures. | High | Parser accepts month-name and ISO. |
| Firmware strings carry `.0-RNN` revision suffixes. | High | Preserve raw firmware for diagnostics. |
| GP writes require `$$`, but GP reads are prefix-less. | GP07R100 only but load-bearing | `Command.prefix_less` controls prefix behavior. |
| Pre-10v05 `DCU` is not unit query; it returns ADC-like counts or rejects. | High | Gate `DCU` to V10 >= 10v05. |
| `LSS`, `NCB`, `NCS`, `ZCA`, `ZCP` are V10 >= 10v05. | High | Firmware gates. |
| Unsupported commands can reject, time out, emit data frames, emit placeholders, or report "Feature Not Enabled". | High | Rely on pre-I/O gates, not runtime rejection patterns. |
| `FPF` absent statistic returns value 0 and label `---`. | High | Capability probes check value and label. |
| Barometer does not imply `PC` support. | High for flow controllers | Split `BAROMETER` and `TAREABLE_ABSOLUTE_PRESSURE`. |
| `VD` returns four columns even on meter/no-valve cases. | Medium | Do not infer valve count from column count. |
| `LS` reply has five fields: unit ID, current, requested, unit code, unit label. | Medium | Populate `SetpointState.current` and `.requested` directly. |
| `DV` reply has no unit-ID prefix. | Medium | `REQUEST_DATA` decoder is special-case. |
| V1_V7 rename can emit a data-frame ack. | Medium/high | 200 ms rename grace before verification drain. |
| Default baud is not reliable. | High | Discovery tries multiple rates; tests accept configured baud. |

### 16.6.7 Second Hardware Session: 8v17 Redux

Key outcomes:

- The original identification-stage smoke failure was a test baud
  mismatch, not a library drain bug.
- `DCU` and `LSS` needed 10v05+ firmware gates.
- `FPF 15` can report a barometer on flow controllers that still cannot execute
  `PC`. This finding was later promoted into the `TAREABLE_ABSOLUTE_PRESSURE`
  capability split.

### 16.6.8 GP07R100 Capture

The GP07R100 `MC-100SCCM-D` capture established:

- `VE` does not respond.
- `??M*`, `??D*`, `??G*`, and poll work prefix-less.
- `??M*` uses M0-M8, short labels, and `\x08` padding.
- `??D*` uses the legacy data-frame table dialect with padding.
- `??G*` can return the entire gas list on one line.
- Legacy gas set `G <code>` requires `$$`.
- Modern commands are silent and are gated out of GP.
- Unit rename did not commit on the captured unit.

After fixes, the GP hardware suite produced 4 passes, 26 expected skips, and 0
failures. The passes were read-only poll, gas list, and gas set/restore.

### 16.6.9 Fourth Hardware Session: End-to-End Sweep

The fourth session ran the full read-only and stateful suite against 5v12,
6v21, 7v09, 8v30, 10v04, 10v03, and 10v20 devices.

| Device | Firmware | Pass | Skip | Fail | Fixes |
| --- | --- | ---: | ---: | ---: | --- |
| MC-500SCCM-D | 5v12 | 10 | 20 | 0 | Test skip for FPF rejection. |
| MCR-775SLPM-D | 6v21 | 12 | 18 | 0 | Drain-on-error and rename grace. |
| MCP-50SLPM-D | 7v09 | 12 | 18 | 0 | Covered by 6v21 fixes. |
| MCR-500SLPM-D | 8v30 | 12 | 18 | 0 | None. |
| MW-10SLPM-D | 10v04 | 12 | 18 | 0 | None. |
| MC-5SLPM-D | 10v03 | 18 | 12 | 0 | Test skip for pre-10v05 GS. |
| MC-5SLPM-D | 10v20 | 26 | 4 | 0 | Regression pass. |

Library fixes from this session:

- Drain residual bytes after protocol/rejection errors to handle multi-part
  rejects such as blank line followed by `?`.
- Increase rename grace from 50 ms to 200 ms before verification drain.
- Split `BAROMETER` from `TAREABLE_ABSOLUTE_PRESSURE`.

Additional captured behavior:

- `DV` request/reply shape.
- `LS` current/requested divergence.
- `tests/fixtures/device_matrix.yaml` now records per-device command behavior
  for `t`, `tp`, `rename`, and `pc`.

### 16.6.10 Fifth Hardware Session: Real-Device Validation

Session scope: every surface beyond identification and Tier-1 commands
on real hardware. The earlier sessions focused on identification,
parsing, and Tier-1 commands; this session covered acquisition, sinks,
the sync facade, and the Tier-2 / streaming / valve / control-setup /
non-destructive-specialty / totalizer surface. Every device in the
§16.6 table was re-exercised.

Canonical wire-shape findings promoted into §15.3:

- Streamed data frames drop the leading unit-id letter; the
  `StreamingSession` producer normalises space- and `@`-prefixed
  frames before parse.
- The `DV` absent-value sentinel is a pure dash run sized to the
  statistic's column width (saw `-------`, not only `--`).
- `VD` reply is a fixed-width 4-column line; decoder accepts 1–4.
- `DCA` reply is 2 fields on 10v20 (no statistic echo); decoder
  accepts both shapes with the facade refilling the request side.
- `UD` empty-slot reply is just `<uid>` (1 field); decoder sentinels
  `slot=-1` and the facade refills from the request.
- `TC` reply echoes the totalizer id on 10v20 (7 fields); decoder
  accepts 6 or 7.
- Legacy `S` setpoint decoder locates the post-op column by
  `*_SETPT` statistic, not by the primer's placeholder name.
- `sample_to_row` drops the frame's unit-id echo (case-insensitive)
  so SQLite's duplicate-column check accepts the schema.

Acquisition and timing baselines captured on real hardware:

| Observation | Value |
| --- | --- |
| 10v20 open-time median (full DCU + FPF + LV probe sweep) | 1507 ms |
| 10v20 poll p50 / p95 / p99 (19200 baud, 1000 samples) | 27.3 / 27.7 / 28.1 ms |
| 8v17 poll p50 / p95 / p99 (115200 baud, 500 samples) | 5.8 / 10.2 / 12.8 ms |
| 10v20 `DV` with 7 statistics | 39 ms |
| 10v20 streaming `rate_ms=50` for 60 s, interval p99 | 51 ms |
| Concurrent-command fast-fail while streaming | 0.088 ms (zero tx) |
| 10-minute 10 Hz multi-sink soak (10v20) | 6000/6000 rows match across InMemory/CSV/JSONL/SQLite/Parquet |

Open questions resolved: §13 Q6 (`DV` on V8/V9) and Q7 (five-field `LS`
scope). See §13 for the updated status.

Token-collision invariant verification: wire trace on 10v20 proved
`T 1` and `TP 1` resets emit the numeric argument (`b'AT 1\r'` /
`b'ATP 1\r'`) and never the bare `T` / `TP` forms that would collide
with tare.

Known-device-quirk follow-ups (not library bugs):

- ✓ Resolved: 10v20 rejects the primer's `ZCA <uid> 0 0` disable
  form with `?`. The follow-up pass on a second 10v20 unit
  reproduced the rejection (firmware-wide, not per-device); the
  wire-form probe found `ZCA 0` (no delay field) is the shortest
  accepted shape. Encoder updated (§15.3); `dev.auto_tare(enable=False)`
  round-trip verified on real 10v20.
- V1_V7 serial setpoint writes no-op silently when the device's
  setpoint source is on Analog. The library cannot guard pre-I/O
  because `LSS` is V10-only, so there is no way to cache the source
  on V1_V7. The follow-up discriminator experiment (toggling
  front-panel source between Serial and Analog while capturing every
  plausible probe token) found no passive signal that differs between
  the two states — only a destructive write+readback behaviour.
  Documented on `setpoint()` and in
  `docs/safety.md`.
- V1_V7 firmware interprets any command starting with ``AL<X>`` as
  "display lock with argument X" (confirmed on 7v09: `ALS`, `ALSS`,
  `ALV`, `AL` all set the ``LCK`` status bit). The library's
  firmware gates protect normal use — those tokens never reach V1_V7
  hardware. Third-party code and direct ``session.execute`` can
  still trip it, so ``UNLOCK_DISPLAY`` was widened to the
  no-capability-gate safety-escape form (§15.3); ``dev.unlock_display()``
  clears ``LCK`` end-to-end on real V1_V7 hardware.

Follow-up session (same date): a second 10v20 unit, the cross-family
sync smoke on 8v17, and the LSS-probe + LCK-trigger + ZCA-disable
wire-form experiments on 7v09 / 10v20 together produced four more
library fixes (stream-recovery `max_bytes` cap, `SyncStreamingSession`
`wrap_async_context_manager` routing, `UNLOCK_DISPLAY` capability-gate
removal, `AUTO_TARE` disable-form shortening — all in §15.3).
Unit-test surface closed at **1674 tests, 3 skips**.

### 16.7 Evidence Discipline

Hardware observations are tagged internally by confidence:

1. **Canonical behavior:** repeated across devices or strongly consistent with
   documentation. Encode in parsers/gates.
2. **Plausibly canonical single-device behavior:** support it, but revalidate on
   the next matching device.
3. **Uncertain single-device behavior:** keep parsers tolerant or gates loose;
   document the observation.

The diagnostic script exists to move findings from uncertain to canonical as new
devices are captured.

---
