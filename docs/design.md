# pyAlicat v2 — Final Design Document (Synthesis)

**Status:** Draft, ready for implementation planning.
**Scope:** Complete rewrite of the `pyalicat` package. Clean break; no import compatibility with `0.0.x`.

---

## 1. Purpose and Context

pyAlicat provides a Python API for Alicat mass flow meters (`M-`, `MS-`, `MQ-`, `MW-`) and mass flow controllers (`MC-`, `MCS-`, `MCQ-`, `MCW-`) over serial using Alicat's ASCII protocol. It targets:

- Interactive scripts and notebooks (sync).
- Long-running acquisition services (async, multi-device).
- Scientific experiments where timing fidelity and data provenance matter.

The existing 0.0.x codebase holds valuable domain knowledge — especially in `device.py` and `codes.json` — but mixes transport, framing, command construction, response parsing, firmware capability checks, device modeling, multi-device orchestration, and database logging across a few large modules. The rewrite preserves the command catalog and the code tables; it replaces the structure around them.

## 2. Goals and Non-Goals

### Goals

1. **Correctness first.** Reliable over hours or days with one device. Reliable with multiple devices driven concurrently from one process.
2. **Robustness.** Every I/O boundary has an explicit timeout. Every unexpected response raises a typed exception. No silent fallback ever returns partial data. `0` and `False` are valid inputs, never treated as "missing."
3. **Performance.** Open the serial port once. No per-command dictionary lookups into a 600-entry JSON blob at runtime. No busy-waits. Absolute-target scheduling for fixed-rate acquisition (no drift).
4. **Extensibility.** Swappable transports (Serial today, TCP/Modbus later). New commands as declarative objects. New device families as subclasses that bind a different command set.
5. **Maintainability.** Less code, stronger types, fewer hand-written branches. `mypy --strict` passes cleanly. Ruff is the only linter. 95%+ of tests run without hardware.
6. **Discoverable API.** Typed arguments (`Gas.N2`) and typed responses (`DataFrame`, `SetpointState`). IDE autocomplete guides the user through the command surface.
7. **Async-first, sync-available.** Canonical API is `async def`. A thin sync facade wraps it for scripts, notebooks, and REPL use without duplicating logic.
8. **Data out, not sinks in.** The library emits typed sample streams (`AsyncIterator`). Consumers decide where they go. First-party sinks (CSV, JSONL, Parquet, Postgres) live behind extras.
9. **Safety.** Physically dangerous commands (factory reset, baud change, exhaust) require explicit confirmation. Setpoints are validated against device range. Logs never contain credentials.
10. **Lean core.** `pip install pyalicat` pulls in `anyio` and one serial backend. Nothing else. No Postgres, no pandas, no pyarrow, no pydantic in the core import path.

### Non-goals

- No built-in GUI or web server.
- No multi-process RPC. A single process owns a serial port.
- No Modbus or TCP implementations in v2.0. Interfaces exist; implementations come later.
- No support for liquid (L-series) or pressure (P-series) controllers in v2.0. Extension points exist.
- No ORM. Sinks are thin wrappers.
- No built-in units library integration (e.g. `pint`). Provide hooks for users who want it.
- No automatic preservation of `0.0.x` method signatures where they conflict with correctness.

## 3. Design Principles

1. **One layer, one job.** Transport moves bytes. Protocol frames commands. Commands encode and parse. Sessions serialize I/O and cache device state. Devices expose user-friendly methods. Streaming produces samples. Sinks store samples.
2. **Async core, sync wrapper.** Alicat operations are I/O-bound; multi-device acquisition benefits from cooperative concurrency. Sync mirrors async through a generated facade — never a reimplementation.
3. **Separate transport from protocol.** Serial, TCP, fake transports all satisfy the same interface.
4. **Separate protocol from product API.** Command framing and response parsing are testable without device classes.
5. **Declarative commands.** A `Command` object describes encoding, decoding, firmware requirements, supported device kinds, and response shape. No 20-line copy-paste per command.
6. **Typed models at boundaries.** Frozen dataclasses with `slots=True`. `.as_dict()` for migration and serialization convenience.
7. **Typed arguments at boundaries.** `Gas`, `Unit`, `Statistic` are enums for IDE completion and static checking; alias-aware registries handle coercion, reverse lookup, and error messages.
8. **Explicit capability model.** Firmware versions, device kinds, and supported commands are metadata on each `Command`, not scattered `if self._vers < ...` checks.
9. **Optional features truly optional.** Core device control has no hard dependency on Postgres, pandas, or a database.
10. **Testable without hardware.** A fake transport with golden response fixtures covers 95%+ of behavior in CI.
11. **Fail loudly and specifically.** Timeouts, malformed replies, unsupported commands, unknown gases — all distinct exception types carrying structured context.
12. **Safety is a first-class concern.** Destructive operations are gated behind `confirm=True`. Setpoints are range-checked before I/O.

## 4. Package Layout

```
src/
  pyalicat/
    __init__.py
    py.typed
    errors.py
    version.py
    config.py
    _logging.py
    firmware.py

    transport/
      __init__.py
      base.py               # Transport Protocol (PEP 544)
      serial.py             # SerialTransport
      fake.py               # FakeTransport for tests
      # tcp.py               (v2.1+)

    protocol/
      __init__.py
      framing.py            # read_until, CR delimiter, line parsing
      client.py             # AlicatProtocolClient: write + read, one-in-flight
      parser.py             # parse_fields, parse_float, parse_bool_code, ...
      streaming.py          # streaming-mode state machine
      raw.py                # raw-access escape hatch for advanced users

    registry/
      __init__.py
      codes.py              # Gas, Unit, Statistic enums (generated)
      _codes_gen.py         # build-time generated from codes.json
      aliases.py            # AliasRegistry: coerce(str|enum) -> enum, reverse by code
      gases.py              # gas_registry singleton
      units.py              # unit_registry singleton
      statistics.py         # statistic_registry singleton
      data/codes.json       # source of truth (shipped in the wheel)

    commands/
      __init__.py
      base.py               # Command, ResponseMode, DecodeContext
      catalog.py            # Commands namespace with all specs
      polling.py
      gas.py
      setpoint.py
      tare.py
      valve.py
      units.py
      totalizer.py
      output.py             # analog outputs, display
      system.py             # baud, unit id, info, blink, reset
      diagnostics.py

    devices/
      __init__.py
      base.py               # Device abstract + model registry
      factory.py            # open_device, identify, device_class_for
      session.py            # Session: transport + lock + firmware cache
      data_frame.py         # DataFrameFormat, DataFrameField
      flow_meter.py
      flow_controller.py
      discovery.py

    streaming/
      __init__.py
      recorder.py           # async record(...) -> AsyncIterator[Batch]
      sample.py             # Sample dataclass

    sinks/                   # optional: installed via pyalicat[sinks]
      __init__.py
      base.py               # SampleSink Protocol, pipe()
      csv.py
      jsonl.py
      parquet.py            # pyalicat[parquet]
      postgres.py           # pyalicat[postgres]

    manager.py              # AlicatManager: multi-device orchestrator
    sync/
      __init__.py           # re-exports sync wrappers
      portal.py             # BlockingPortal lifecycle
    testing.py              # helpers for user tests (fixtures, FakeTransport)

tests/
  unit/
    test_framing.py
    test_parsers.py
    test_registry.py
    test_firmware.py
    test_command_specs.py
    test_data_frame.py
    test_session.py
    test_device_factory.py
    test_manager.py
    test_recorder.py
    test_sinks_csv.py
    test_sinks_jsonl.py
  integration/
    test_fake_end_to_end.py
    test_hardware_flow_meter.py         # marker: hardware
    test_hardware_flow_controller.py    # marker: hardware
    test_hardware_stateful.py           # marker: hardware_stateful
    test_hardware_destructive.py        # marker: hardware_destructive
  fixtures/
    responses/
      manufacturing_info.txt
      dataframe_format_fm.txt
      dataframe_format_fc.txt
      poll_flow_meter.txt
      poll_flow_controller.txt
      gas_list.txt
      gas_set.txt
      setpoint_set.txt
      tare_flow.txt
      error_reply.txt
```

`src/` layout is used so tests import the installed package, not a loose module tree. `py.typed` ships so downstream users get type hints.

## 5. Layer Designs

### 5.1 Transport Layer

Purpose: move bytes. Knows nothing about Alicat.

```python
# transport/base.py
from typing import Protocol

class Transport(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def write(self, data: bytes) -> None: ...
    async def read_until(self, separator: bytes, timeout: float) -> bytes: ...
    async def read_available(self, idle_timeout: float, max_bytes: int | None = None) -> bytes: ...
    async def drain_input(self) -> None: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def label(self) -> str: ...   # port name, for error messages
```

Invariants:

- Single lifecycle: `open()` once, `close()` once.
- Every read has an explicit timeout. On expiry: raise `AlicatTimeoutError`. Never return an empty/partial bytes silently.
- Backend exceptions normalize to `AlicatTransportError` with `__cause__` preserved.

`SerialTransport` takes a frozen `SerialSettings` dataclass:

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
    exclusive: bool = True          # take exclusive lock where supported
    read_timeout: float = 0.5
    write_timeout: float = 0.5
```

**Backend choice is deferred to M1.** Candidates: `anyserial` (anyio-native), `pyserial-asyncio` (asyncio-only), or `pyserial` wrapped in `anyio.to_thread.run_sync` (simplest, portable). A small hardware-in-the-loop benchmark in M1 picks the winner on p50/p99 latency; the `Transport` interface is unchanged either way.

`FakeTransport` is a scripted transport used in tests:

```python
fake = FakeTransport({
    b"A??M*\r":  [b"A M01 Alicat ...", b"A M02 ...", ...],
    b"A\r":      [b"A +0.000 +25.0 ... SCCM ... N2\r"],
    b"AGS 5 1\r":[b"A 5 N2 Nitrogen\r"],
})
await fake.open()
```

It records every write (so tests can assert exact command bytes), supports callables for fuzzier tests, and can simulate timeouts, malformed replies, and added latency.

### 5.2 Protocol Client

Purpose: one path for request/response commands. Translates Python-level intent into one serial round-trip.

```python
class AlicatProtocolClient:
    def __init__(
        self,
        transport: Transport,
        *,
        eol: bytes = b"\r",
        default_timeout: float = 0.25,
        drain_before_write: bool = False,
    ) -> None: ...

    async def query_line(self, command: bytes, *, timeout: float | None = None) -> bytes: ...
    async def query_lines(
        self,
        command: bytes,
        *,
        first_timeout: float | None = None,
        idle_timeout: float | None = None,
        max_lines: int | None = None,
    ) -> tuple[bytes, ...]: ...
    async def write_only(self, command: bytes) -> None: ...
```

Rules:

- Exactly one in-flight command per client. Concurrent callers serialize on an `anyio.Lock`.
- Commands are ASCII-encoded; EOL is appended once.
- `query_line` raises `AlicatTimeoutError` if no terminator arrives before `timeout`.
- `query_lines` reads the first line with a normal timeout, then continues in a loop guarded by `anyio.move_on_after(idle_timeout)` — each successful read resets the idle window. The loop exits on idle expiry, a parser-defined terminator, or `max_lines`. This avoids the common bug where the protocol reads only the first line and leaves the rest as stale input for the next command.
- Empty response is an error unless a spec declares it valid.
- A bare `?` reply becomes `AlicatCommandRejectedError`.
- Optional `drain_input()` hook called before a command when re-syncing after a timeout.

**Timeout and cancellation primitives.** Use `anyio.fail_after(seconds)` / `anyio.move_on_after(seconds)` exclusively. **Never use `asyncio.wait_for`** — it is legacy, has known cancel-leak corner cases around the awaited task, and is superseded by `asyncio.timeout()` (3.11+), which `anyio.fail_after` wraps. A CI lint rule (ruff `ASYNC109`/custom) blocks `wait_for` in `src/`.

**Eager task execution (3.12+).** The M1 serial backend benchmark evaluates `asyncio.eager_task_factory` (or anyio's backend-options equivalent) on the protocol client's hot path. Eager execution skips one event-loop round-trip when the first `await` doesn't suspend — a measurable win for short, lock-contended command sequences. Decision recorded alongside the backend choice.

### 5.3 Registry

The current `codes.json` holds ~98 statistics, ~230 units, ~295 gases. We keep the JSON as the source of truth and generate typed enums **plus** alias-aware registries as two distinct layers.

**Generated enums** give IDE completion and type-precision:

```python
# registry/_codes_gen.py (generated from codes.json by scripts/gen_codes.py)
from enum import StrEnum

class Gas(StrEnum):
    N2 = "N2"
    AIR = "Air"
    # ... 295 entries
    @property
    def code(self) -> int: ...         # numeric Alicat code
    @property
    def long_name(self) -> str: ...    # display name

class Unit(StrEnum): ...
class Statistic(StrEnum): ...
```

**Alias registries** handle coercion, reverse lookup, and informative errors:

```python
# registry/aliases.py
class AliasRegistry(Generic[E]):
    def coerce(self, value: E | str) -> E: ...                  # informative error on miss
    def by_code(self, code: int) -> E: ...
    def aliases(self, member: E) -> tuple[str, ...]: ...
    def suggest(self, bad: str, *, n: int = 3) -> tuple[str, ...]: ...

# registry/gases.py
gas_registry: AliasRegistry[Gas] = ...
# registry/units.py
unit_registry: AliasRegistry[Unit] = ...
# registry/statistics.py
statistic_registry: AliasRegistry[Statistic] = ...
```

Why this split: enums are *types* (what the API accepts and returns). Registries are *lookup tables* (how strings from users or devices resolve to those types). Keeping them separate means unit tests, error messages, and alias lists do not bloat every enum class.

Command arguments accept `Gas | str`, coerce through the registry, and raise `UnknownGasError(value, suggestions=[...])` on a miss.

**Generation is build-time**, emitted to `registry/_codes_gen.py`. A CI check fails if `codes.json` changes without regenerating. Build-time gives IDE completion, mypy coverage, and stable import performance.

**Validation at load.** A registry test asserts:

- No duplicate codes within a category.
- No duplicate aliases.
- Every enum member round-trips through `.code` and `by_code()`.

### 5.4 Command Layer

Every Alicat command is one object. No hand-written methods with inlined `write_readline` calls.

```python
# commands/base.py
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

Req = TypeVar("Req")
Resp = TypeVar("Resp")

class ResponseMode(Enum):
    NONE = "none"       # write-only; no read
    LINE = "line"       # single-line response
    LINES = "lines"     # multi-line table response
    STREAM = "stream"   # enters streaming mode (not a normal command)

@dataclass(frozen=True, slots=True)
class DecodeContext:
    unit_id: str
    data_frame_format: "DataFrameFormat | None"
    firmware: "FirmwareVersion"

@dataclass(frozen=True, slots=True)
class Command(Generic[Req, Resp]):
    name: str
    token: str
    response_mode: ResponseMode
    device_kinds: frozenset["DeviceKind"]
    min_firmware: "FirmwareVersion | None" = None
    max_firmware: "FirmwareVersion | None" = None
    destructive: bool = False            # requires explicit confirm=True
    experimental: bool = False

    def encode(self, unit_id: str, request: Req) -> bytes: ...
    def decode(self, response: bytes | tuple[bytes, ...], ctx: DecodeContext) -> Resp: ...
```

`ResponseMode` lets the `Session` pick `write_only` / `query_line` / `query_lines` without per-command branching in session code. The command object says what it needs; the session dispatches.

Example:

```python
# commands/gas.py
@dataclass(frozen=True, slots=True)
class GasSelectRequest:
    gas: Gas | str | None = None
    save: bool | None = None

@dataclass(frozen=True, slots=True)
class GasSelect(Command[GasSelectRequest, GasState]):
    name: str = "gas_select"
    token: str = "GS"
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = frozenset(
        {DeviceKind.FLOW_METER, DeviceKind.FLOW_CONTROLLER}
    )
    min_firmware: FirmwareVersion | None = FirmwareVersion(10, 5)

    def encode(self, unit_id: str, req: GasSelectRequest) -> bytes:
        if req.gas is None:
            return f"{unit_id}GS\r".encode()              # query form
        gas = gas_registry.coerce(req.gas)
        save_flag = "" if req.save is None else ("1" if req.save else "0")
        return f"{unit_id}GS {gas.code} {save_flag}".rstrip().encode() + b"\r"

    def decode(self, response: bytes, ctx: DecodeContext) -> GasState:
        fields = parse_fields(response.decode(), expected_count=4, command="gas_select")
        code = parse_int(fields[1], field="code")
        return GasState(
            unit_id=fields[0],
            code=code,
            gas=gas_registry.by_code(code),
            label=fields[2],
            long_name=fields[3],
        )

GAS_SELECT = GasSelect()
```

Encoders must distinguish four distinct "missing" cases: `None` (omitted), `0` (valid zero), `False` (valid false), empty string (generally invalid). The current code conflates these; v2 must not.

All specs register into a `Commands` namespace (`commands/catalog.py`) — the programmatic entry point for advanced users and the source from which device facade methods are generated.

Benefits:

- One file per command group.
- One place for firmware gating, encode, decode, response shape.
- Middleware can wrap `Session.execute(command, request)` uniformly (retry, logging, recording for fixtures).
- Unit tests call `cmd.encode(...)` and `cmd.decode(...)` directly — no transport needed.

### 5.5 Typed Models

Public returns are frozen dataclasses with `slots=True`:

```python
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
    kind: DeviceKind

@dataclass(frozen=True, slots=True)
class DataFrame:
    unit_id: str
    format: "DataFrameFormat"
    values: Mapping[str, float | str | None]
    received_at: datetime
    monotonic_ns: int
    def as_dict(self) -> dict[str, float | str | None]: ...
    def get_float(self, name: str) -> float | None: ...

@dataclass(frozen=True, slots=True)
class MeasurementSet:
    unit_id: str
    values: Mapping[Statistic, float | str | None]
    averaging_ms: int
    received_at: datetime

@dataclass(frozen=True, slots=True)
class GasState:
    unit_id: str
    code: int
    gas: Gas
    label: str
    long_name: str

@dataclass(frozen=True, slots=True)
class SetpointState:
    unit_id: str
    current: float
    requested: float
    unit: Unit | None
    unit_label: str | None
    frame: DataFrame

@dataclass(frozen=True, slots=True)
class TareResult:
    frame: DataFrame

@dataclass(frozen=True, slots=True)
class UnitSetting:
    unit_id: str
    statistic: Statistic
    unit: Unit | None
    label: str
# ... more per command family
```

Every field that can be unavailable (`--` on the wire) is `Optional`. `as_dict()` is provided on complex frames for serialization and migration from 0.0.x dict returns.

### 5.6 Data Frame Format

The data frame — what `A\r` returns — is core to polling and to many commands that return a post-operation state frame. It is not static; it is discovered from the device via `??D*` at session start.

```python
@dataclass(frozen=True, slots=True)
class DataFrameField:
    name: str                     # canonical Alicat name, e.g. "Mass_Flow"
    raw_name: str                 # exact name as reported by the device
    type_name: str                # "decimal", "text", "integer", ...
    statistic: Statistic | None   # registry link for typed aggregation
    parser: Callable[[str], float | str | None]

@dataclass(frozen=True, slots=True)
class DataFrameFormat:
    fields: tuple[DataFrameField, ...]
    def names(self) -> tuple[str, ...]: ...
    def parse(self, raw: bytes) -> DataFrame: ...
```

Linking each field back to `Statistic` matters for downstream aggregation, unit lookup, and sink schemas: consumers can key by `Statistic.MASS_FLOW` rather than the raw string `"Mass_Flow"`.

The `Session` caches this format at startup and exposes `await session.refresh_data_frame_format()` for the rare case it changes at runtime.

### 5.7 Session

The `Session` is the only object that sends commands. One per device (per unit ID).

```python
class Session:
    def __init__(
        self,
        client: AlicatProtocolClient,
        *,
        unit_id: str,
        info: DeviceInfo,
        data_frame_format: DataFrameFormat,
        command_lock: anyio.Lock,          # port-level, shared across unit IDs
        default_timeout: float = 0.25,
    ) -> None: ...

    async def execute(
        self, command: Command[Req, Resp], request: Req, *, timeout: float | None = None
    ) -> Resp: ...

    async def refresh_data_frame_format(self) -> DataFrameFormat: ...
    async def refresh_firmware(self) -> FirmwareVersion: ...
    async def close(self) -> None: ...
```

Responsibilities:

1. Verify `command.min_firmware <= self.firmware <= command.max_firmware`; else raise `AlicatFirmwareError` naming the required range and actual version.
2. Verify the current device kind is in `command.device_kinds`.
3. If `command.destructive` and `request.confirm is not True`, raise `AlicatValidationError` — before any I/O.
4. Encode → dispatch to the right `AlicatProtocolClient` method based on `command.response_mode` → decode. All under `command_lock` so daisy-chained devices on one port never interleave.
5. Tag exceptions with structured `ErrorContext` (command, raw command bytes, raw response bytes, unit_id, firmware, port, elapsed_ms).
6. Emit structured log events: INFO summary, DEBUG raw bytes, ERROR with full context.

**Multi-unit-on-one-port.** Alicat supports multiple devices on one RS-485 bus addressed by unit ID. Each unit gets its own `Session`, but they all share the same `AlicatProtocolClient` and its lock. Two sessions on the same port serialize; two sessions on different ports run concurrently. This mirrors the physical reality and is the one place where correctness depends on sharing state across `Session` objects.

### 5.8 Streaming Mode

Alicat's streaming mode (`A @ @`) is a state change, not a command. In streaming, the device pushes data frames continuously; request/response traffic is invalid.

Modeled as a state machine on the `AlicatProtocolClient`:

```python
class ClientState(Enum):
    REQUEST_RESPONSE = "rr"
    STREAMING = "streaming"

class StreamingSession:
    async def __aenter__(self) -> AsyncIterator[DataFrame]: ...
    async def __aexit__(self, *exc) -> None: ...
```

Rules:

- Entering streaming marks the client as `STREAMING`. All `Session.execute` calls fail fast with `AlicatStreamingModeError`.
- Exit always sends the stop command, even on exception. Idempotent.
- Device ID changes during stop are explicit (`stop_stream(new_unit_id="B")`).
- Parsing errors during streaming are logged but do not kill the iterator unless `strict=True`.

```python
async with device.stream(rate_ms=50) as frames:
    async for frame in frames:
        process(frame)
```

### 5.9 Device Factory and Facades

Opening a device:

```python
async def open_device(
    port: str | Transport | AlicatProtocolClient,
    *,
    unit_id: str = "A",
    serial: SerialSettings | None = None,
    timeout: float = 0.25,
) -> Device: ...

async def identify_device(
    client: AlicatProtocolClient, unit_id: str = "A"
) -> DeviceInfo: ...

def device_class_for(info: DeviceInfo) -> type[Device]: ...
```

Inputs accepted by `open_device`:

- serial port string (`"/dev/ttyUSB0"`, `"COM3"`);
- pre-built `Transport` (for tests, for future TCP);
- pre-built `AlicatProtocolClient` (for reusing a port across unit IDs).

**Model classification** uses an explicit registry, not `__subclasses__` walks:

```python
class DeviceKind(Enum):
    FLOW_METER = "flow_meter"
    FLOW_CONTROLLER = "flow_controller"
    PRESSURE_METER = "pressure_meter"           # v2.1+
    PRESSURE_CONTROLLER = "pressure_controller" # v2.1+
    LIQUID_METER = "liquid_meter"               # v2.1+
    LIQUID_CONTROLLER = "liquid_controller"     # v2.1+
    UNKNOWN = "unknown"

@dataclass(frozen=True, slots=True)
class ModelRule:
    prefix: str
    kind: DeviceKind
    device_cls: type[Device]

MODEL_RULES: tuple[ModelRule, ...] = (
    ModelRule("MC-",  DeviceKind.FLOW_CONTROLLER, FlowController),
    ModelRule("MCS-", DeviceKind.FLOW_CONTROLLER, FlowController),
    ModelRule("MCQ-", DeviceKind.FLOW_CONTROLLER, FlowController),
    ModelRule("MCW-", DeviceKind.FLOW_CONTROLLER, FlowController),
    ModelRule("M-",   DeviceKind.FLOW_METER,      FlowMeter),
    ModelRule("MS-",  DeviceKind.FLOW_METER,      FlowMeter),
    ModelRule("MQ-",  DeviceKind.FLOW_METER,      FlowMeter),
    ModelRule("MW-",  DeviceKind.FLOW_METER,      FlowMeter),
)
```

Controller prefixes are checked before meter prefixes (longest/most-specific first). Adding a new family is one tuple entry plus a `Device` subclass.

Device facade:

```python
class Device:
    # lifecycle
    async def close(self) -> None: ...
    async def __aenter__(self) -> "Device": ...
    async def __aexit__(self, *exc) -> None: ...

    # identity
    @property
    def info(self) -> DeviceInfo: ...

    # polling
    async def poll(self) -> DataFrame: ...
    async def request(
        self, stats: Sequence[Statistic | str], *, averaging_ms: int = 1
    ) -> MeasurementSet: ...
    def stream(self, *, rate_ms: int = 50) -> StreamingSession: ...

    # gas
    async def gas(
        self, gas: Gas | str | None = None, *, save: bool | None = None
    ) -> GasState: ...
    async def gas_list(self) -> Mapping[int, str]: ...

    # units
    async def engineering_units(
        self,
        statistic: Statistic | str,
        unit: Unit | str | None = None,
        *,
        apply_to_group: bool = False,
        override_special_rules: bool = False,
    ) -> UnitSetting: ...

    # tare
    async def tare_flow(self) -> TareResult: ...
    async def tare_absolute_pressure(self) -> TareResult: ...
    async def tare_gauge_pressure(self) -> TareResult: ...
    async def auto_tare(
        self, enable: bool | None = None, delay_s: float | None = None
    ) -> AutoTareState: ...

    # escape hatch
    async def execute(self, command: Command[Req, Resp], request: Req) -> Resp: ...


class FlowMeter(Device):
    # no controller-only methods
    pass


class FlowController(FlowMeter):
    async def setpoint(
        self, value: float | None = None, unit: Unit | str | None = None
    ) -> SetpointState: ...
    async def loop_control_variable(
        self, variable: Statistic | str | None = None
    ) -> LoopControlState: ...
    async def hold_valves(self) -> DataFrame: ...
    async def hold_valves_closed(self, *, confirm: bool = False) -> DataFrame: ...
    async def cancel_valve_hold(self) -> DataFrame: ...
    async def exhaust(self, *, confirm: bool = False) -> DataFrame: ...   # destructive
    async def query_valve(self) -> ValveDrive: ...
    async def batch(
        self, volume: float | None = None, unit: Unit | str | None = None
    ) -> BatchState: ...
```

Facade methods are thin; each delegates to `self._session.execute(CMD, request)`. Advanced users bypass the facade entirely via `execute()` or the `protocol/raw.py` escape hatch for one-off commands not in the catalog.

### 5.10 Capability Model

```python
# firmware.py
@dataclass(frozen=True, order=True, slots=True)
class FirmwareVersion:
    major: int
    minor: int

    @classmethod
    def parse(cls, software: str) -> "FirmwareVersion":
        # accepts "10v05", "10v5", "GP-10v05", ...
        ...

    def __str__(self) -> str:
        return f"{self.major}v{self.minor:02d}"
```

Firmware must not be compared as a float — `10v05`, `10v5`, and `10.05` all normalize to the same structured value. Firmware is parsed once at session startup from the `software` field of the manufacturing info. Gating is metadata on each `Command`. When a command fails capability checks:

```python
raise AlicatFirmwareError(
    command="auto_tare",
    reason="firmware_too_old",
    actual=FirmwareVersion(9, 0),
    required_min=FirmwareVersion(10, 5),
)
```

### 5.11 Response Parsing Helpers

Shared helpers live in `protocol/parser.py`:

```python
def parse_ascii(raw: bytes) -> str: ...
def parse_fields(raw: str, *, expected_count: int | None = None, command: str) -> list[str]: ...
def parse_float(value: str, *, field: str) -> float: ...
def parse_int(value: str, *, field: str) -> int: ...
def parse_optional_float(value: str, *, field: str) -> float | None: ...    # "--" -> None
def parse_bool_code(
    value: str, *, field: str, mapping: Mapping[str, bool] = {"1": True, "0": False}
) -> bool: ...
def parse_enum_code(value: str, *, field: str, registry: AliasRegistry[E]) -> E: ...
def parse_data_frame(raw: bytes, fmt: DataFrameFormat) -> DataFrame: ...
def parse_data_frame_table(lines: Sequence[bytes]) -> DataFrameFormat: ...
def parse_manufacturing_info(lines: Sequence[bytes]) -> DeviceInfo: ...
def parse_gas_list(lines: Sequence[bytes]) -> dict[int, str]: ...
```

Rules:

- On mismatch, raise `AlicatParseError(command, raw_response, field, expected, actual)`.
- `--` normalizes to `None` consistently.
- Missing fields raise `AlicatParseError`. Extra fields raise too unless the parser explicitly allows them.
- Unit ID mismatch raises `AlicatUnitIdMismatchError`.
- Raw response is preserved in every error for debugging.
- Never silently truncate or pad with empty strings.

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

async def probe(
    port: str, *, unit_id: str = "A", baudrate: int = 19200, timeout: float = 0.2
) -> DiscoveryResult: ...

async def find_devices(
    ports: Iterable[str] | None = None,
    *,
    unit_ids: Sequence[str] = ("A",),
    baudrates: Sequence[int] = (19200, 115200),
    timeout: float = 0.2,
    max_concurrency: int = 8,
) -> tuple[DiscoveryResult, ...]: ...
```

- Platform-aware port enumeration via `serial.tools.list_ports` (Linux, macOS, Windows).
- Iterates multiple baudrates by default — real fleets are mixed, and guessing wrong leaves a device "invisible."
- Concurrency bounded by `max_concurrency` via `anyio.CapacityLimiter`.
- Every result is returned, success or failure — the caller decides what to do with failures.
- Never prints. Diagnostics (listing, human-readable report) belong in an example script or CLI, not core.

### 5.13 Multi-Device Manager

Replaces today's `DAQ`.

```python
class AlicatManager:
    def __init__(self, *, error_policy: ErrorPolicy = ErrorPolicy.RAISE) -> None: ...

    async def add(
        self,
        name: str,
        source: Device | str | Transport | AlicatProtocolClient,
        *,
        unit_id: str = "A",
        serial: SerialSettings | None = None,
    ) -> Device: ...

    async def remove(self, name: str) -> None: ...
    def get(self, name: str) -> Device: ...

    async def poll(
        self, names: Sequence[str] | None = None
    ) -> Mapping[str, DeviceResult[DataFrame]]: ...
    async def request(
        self, stats: Sequence[Statistic | str], names: Sequence[str] | None = None
    ) -> Mapping[str, DeviceResult[MeasurementSet]]: ...
    async def execute(
        self,
        command: Command[Req, Resp],
        requests_by_name: Mapping[str, Req],
    ) -> Mapping[str, DeviceResult[Resp]]: ...

    async def close(self) -> None: ...
    async def __aenter__(self) -> "AlicatManager": ...
    async def __aexit__(self, *exc) -> None: ...


class ErrorPolicy(Enum):
    RAISE = "raise"      # first failure raises; collects rest via ExceptionGroup
    RETURN = "return"    # per-device result carries either value or error

@dataclass(frozen=True, slots=True)
class DeviceResult(Generic[T]):
    value: T | None
    error: AlicatError | None
    @property
    def ok(self) -> bool: return self.error is None
```

Concurrency rules:

- Operations across different physical ports run concurrently (`anyio.create_task_group`).
- Operations against the same physical port serialize via the shared command lock on the port's client (see §5.7).
- Under `RAISE`, the manager raises an `ExceptionGroup` if any device failed — never silently drops results.
- Under `RETURN`, every device produces a `DeviceResult`; callers inspect `.error`.

**Resource lifecycle.** The manager composes device contexts through `contextlib.AsyncExitStack`. Each `add(name, source, ...)` call enters the new device's async context on the stack; `close()` / `__aexit__` exits them in LIFO order. This handles partial-open failures cleanly: if the 3rd of 5 devices fails to open, the first two are guaranteed to close via the stack's unwind — no hand-rolled cleanup chains, no leaked serial ports. Per-port clients are ref-counted within the stack so the last `remove()` on a shared port triggers the client's close.

### 5.14 Acquisition and Samples

The library emits timed `Sample` streams. It does not own the sink.

```python
# streaming/sample.py
@dataclass(frozen=True, slots=True)
class Sample:
    device: str
    unit_id: str
    monotonic_ns: int                  # for scheduling/drift analysis
    requested_at: datetime             # wall clock, send time
    received_at: datetime              # wall clock, response time
    midpoint_at: datetime              # (requested + received) / 2 — best estimate of sample time
    latency_s: float
    frame: DataFrame

# streaming/recorder.py
@dataclass(frozen=True, slots=True)
class AcquisitionSummary:
    started_at: datetime
    finished_at: datetime
    samples_emitted: int
    samples_late: int
    max_drift_ms: float

async def record(
    manager: AlicatManager,
    *,
    stats: Sequence[Statistic | str] | None = None,  # None -> use poll()
    rate_hz: float,
    duration: float | None = None,
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
) -> AsyncIterator[Mapping[str, Sample]]: ...

class OverflowPolicy(Enum):
    BLOCK = "block"             # await slow consumer — default; silent data loss is surprising
    DROP_NEWEST = "drop_newest" # skip this sample, record as late
    DROP_OLDEST = "drop_oldest" # evict oldest queued, enqueue newest
```

Implementation:

- Absolute-target scheduling: `target = start + n * period`, wait with `anyio.sleep_until`. If a cycle overruns, increment `samples_late` and skip the missed slot (never drift).
- Monotonic clock for scheduling; wall clock for timestamps; midpoint timestamp for best-estimate sample time.
- `anyio.create_memory_object_stream(max_buffer_size=buffer_size)` between producer task and consumer iterator.
- No threads. No `time.sleep`. No busy-waits.
- Cancellation-safe: `async for` exit cleanly cancels the producer task.

### 5.15 Sinks

Sinks are opt-in; installed via extras (`pyalicat[parquet]`, `pyalicat[postgres]`).

```python
# sinks/base.py
class SampleSink(Protocol):
    async def open(self) -> None: ...
    async def write_many(self, samples: Sequence[Sample]) -> None: ...
    async def close(self) -> None: ...
    async def __aenter__(self) -> "SampleSink": ...
    async def __aexit__(self, *exc) -> None: ...

async def pipe(
    stream: AsyncIterator[Mapping[str, Sample]],
    sink: SampleSink,
    *,
    batch_size: int = 64,
    flush_interval: float = 1.0,
) -> AcquisitionSummary: ...
```

First-party sinks:

- **`InMemorySink`** (testing): collects samples in a list.
- **`CsvSink(path)`**: plain CSV, column set fixed at `open()` time from the first batch's schema. Stdlib only.
- **`JsonlSink(path)`**: one JSON object per line; schema-free. Stdlib only.
- **`ParquetSink(path, schema=None)`**: `pyarrow`-based; schema inferred or supplied; rotates files by size/time.
- **`PostgresSink(config, table="alicat_samples", timescale=False)`**: `asyncpg`; `ensure_schema(conn)` is explicit; column set derived from a user-supplied `RecordSchema` or the first batch; all writes use parameterized queries; TimescaleDB hypertable creation is opt-in, not assumed.

`PostgresConfig` is a plain dataclass (see §5.18). No hardcoded credentials.

### 5.16 Sync Facade

Async is canonical. Sync mirrors it through `anyio.from_thread.BlockingPortal`:

```python
# sync/portal.py
@contextmanager
def portal() -> Iterator[BlockingPortal]:
    # one portal per sync device by default; no process-wide singleton
    ...

# sync/__init__.py
class Alicat:
    @classmethod
    def open(cls, port: str, **kw) -> "Alicat": ...
    def info(self) -> DeviceInfo: ...
    def poll(self) -> DataFrame: ...
    def request(self, stats, averaging_ms: int = 1) -> MeasurementSet: ...
    def gas(self, gas=None, save=None) -> GasState: ...
    def setpoint(self, value=None, unit=None) -> SetpointState: ...
    # ...
    def close(self) -> None: ...
    def __enter__(self): ...
    def __exit__(self, *exc): self.close()

class AlicatManagerSync:
    # mirror of AlicatManager with the same per-context portal strategy
    ...
```

Implementation:

- Each sync `Alicat`/`AlicatManagerSync` owns its own portal by default. Portals are lifecycle-scoped to the sync object's `__enter__`/`__exit__` — no background threads outlive the context manager. This is safer than a process-wide singleton and rules out a whole class of shutdown-order bugs.
- An opt-in shared portal is available for advanced users that do many short-lived sync sessions and want to amortize portal startup cost.
- Sync methods are generated by a `@sync_version` decorator on async methods, so the two APIs cannot drift. Every async facade method has a sync counterpart; a parity test asserts this.

```python
from pyalicat.sync import Alicat

with Alicat.open("/dev/ttyUSB0") as dev:
    print(dev.poll())
    dev.setpoint(50.0, "SCCM")
```

### 5.17 Errors

```python
class AlicatError(Exception):
    context: "ErrorContext"

class AlicatConfigurationError(AlicatError): ...
class UnknownGasError(AlicatConfigurationError): ...
class UnknownUnitError(AlicatConfigurationError): ...
class UnknownStatisticError(AlicatConfigurationError): ...
class InvalidUnitIdError(AlicatConfigurationError): ...
class AlicatValidationError(AlicatConfigurationError): ...    # pre-I/O bad args, missing confirm

class AlicatTransportError(AlicatError): ...
class AlicatTimeoutError(AlicatTransportError): ...
class AlicatConnectionError(AlicatTransportError): ...

class AlicatProtocolError(AlicatError): ...
class AlicatParseError(AlicatProtocolError): ...
class AlicatCommandRejectedError(AlicatProtocolError): ...    # device replied with ?/error marker
class AlicatStreamingModeError(AlicatProtocolError): ...
class AlicatUnitIdMismatchError(AlicatProtocolError): ...

class AlicatCapabilityError(AlicatError): ...
class AlicatUnsupportedCommandError(AlicatCapabilityError): ...
class AlicatFirmwareError(AlicatCapabilityError): ...

class AlicatDiscoveryError(AlicatError): ...
```

Every error carries a typed context object:

```python
@dataclass(frozen=True, slots=True)
class ErrorContext:
    command_name: str | None = None
    command_bytes: bytes | None = None
    raw_response: bytes | None = None
    unit_id: str | None = None
    port: str | None = None
    firmware: FirmwareVersion | None = None
    elapsed_s: float | None = None
```

A typed dataclass beats `**kwargs` for IDE completion, static checking, and consistent rendering in tracebacks.

Rules:

- A timeout is never represented as an empty successful response.
- A malformed response is not a `ValueError`; it is `AlicatParseError`.
- An unsupported firmware path is not a generic `VersionError`; it is `AlicatFirmwareError`.
- Device error markers such as `?` become `AlicatCommandRejectedError`.

### 5.18 Configuration

Core config is a plain dataclass. No `pydantic-settings` in the core install.

```python
# config.py
@dataclass(frozen=True, slots=True)
class AlicatConfig:
    default_timeout_s: float = 0.5
    default_baudrate: int = 19200
    drain_before_write: bool = False

def config_from_env(prefix: str = "PYALICAT_") -> AlicatConfig:
    """Best-effort env loader; only reads well-known keys."""
    ...

# sinks/postgres.py (only imported when the extra is installed)
@dataclass(frozen=True, slots=True)
class PostgresConfig:
    dsn: str | None = None
    host: str | None = None
    port: int = 5432
    user: str | None = None
    password: str | None = None      # never logged
    database: str | None = None
    table: str = "alicat_samples"
    timescale: bool = False
```

Keeping `PostgresConfig` as a plain dataclass (rather than a `pydantic-settings` `BaseSettings`) means no `pydantic` or `pydantic-settings` dependency — even as an extra. Users who want env-driven config use `config_from_env()` or build the dataclass themselves.

**No credentials in code. Ever.**

### 5.19 Observability

- Logger tree: `pyalicat`, `pyalicat.transport`, `pyalicat.protocol`, `pyalicat.session`, `pyalicat.commands`, `pyalicat.streaming`, `pyalicat.sinks.<name>`.
- Structured `extra={"device": ..., "unit_id": ..., "port": ..., "command": ..., "elapsed_ms": ..., "raw": ...}`.
- No `print`. No `warnings.warn` for operational events (only for deprecations).
- The library never configures root handlers — users do.
- Debug logs may include raw bytes. Info logs avoid dumping every payload by default.

### 5.20 Safety

Device-control libraries affect physical systems. Safety rules:

1. **Destructive operations require `confirm=True`.** Factory restore, baud change, unit ID change, valve exhaust, valve hold closed, overpressure disable, power-up setpoint, gas mix deletion, clear totalizer on some firmware, controller gain changes. If `confirm is not True`, raise `AlicatValidationError` before any I/O.
2. **Setpoint validation.** `setpoint()` checks against the device's full-scale range (cached at session startup from manufacturing info). Out-of-range raises `AlicatValidationError` before I/O.
3. **No silent fallback** on capability failure. If a command is not supported, raise; do not substitute a legacy command unless the user opted into that explicitly (e.g., a documented legacy setpoint path for older firmware).
4. **Tare preconditions documented** in docstrings (no flow, line depressurized, etc.). These are user responsibilities; the library surfaces them clearly.
5. **No credentials in source.** Ever.
6. **SQL values always parameterized. SQL identifiers validated** (column names) against a whitelist or an `isidentifier()` + length guard.
7. **Library never prints data by default.** Keeps sensitive experimental data out of unintended log streams.
8. **Hardware tests are tiered** (see §6.1) so read-only runs are the default and destructive runs require explicit opt-in.

## 6. Testing Strategy

### 6.1 Layers

1. **Pure unit tests** (no I/O)
   - `Command.encode(request)` → exact bytes, for every combination of optional args including zero values and `False`.
   - `Command.decode(raw, ctx)` → expected typed model, from recorded fixtures.
   - Registry coercion (enums + aliases) with close-match suggestions.
   - Firmware parse/compare.
   - Parser helpers.
   - Registry invariants (no duplicate codes/aliases; round-trip via `by_code`).
   - Target: 95%+ on `protocol/`, `registry/`, `commands/`, `firmware.py`.

2. **FakeTransport integration**
   - `Session` serializes concurrent calls (same port vs different ports).
   - Firmware gating raises with correct context.
   - Timeouts raise `AlicatTimeoutError` with full context.
   - Multiline table reads do not leave stale input behind.
   - Streaming state transitions correct; `execute()` during stream raises `AlicatStreamingModeError`.
   - Recorder respects absolute-target scheduling under simulated latency; drift reported.
   - `AlicatManager` concurrency and error policies.
   - Sinks: CSV/JSONL byte-for-byte; `PostgresSink` tested against `pytest-asyncpg` or a spun-up container only on a hardware-optional marker.

3. **Hardware-in-the-loop, tiered markers**

   ```python
   @pytest.mark.hardware              # read-only
   @pytest.mark.hardware_stateful     # changes device state (gas, setpoint, tare)
   @pytest.mark.hardware_destructive  # factory reset, baud change, exhaust
   ```

   - All skipped in CI by default. Run on your workstation before each release.
   - Read-only (default hardware run): open → identify → firmware → poll 1000× → close.
   - Stateful (`PYALICAT_ENABLE_STATEFUL_TESTS=1`): set gas, setpoint, tare, with restoration in teardown.
   - Destructive (`PYALICAT_ENABLE_DESTRUCTIVE_TESTS=1`): only the narrow set that truly needs it; documented.
   - Env vars: `PYALICAT_TEST_FLOW_METER_PORT`, `PYALICAT_TEST_FLOW_CONTROLLER_PORT`, `PYALICAT_TEST_UNIT_ID`.

4. **Property-based** (hypothesis)
   - Round-trip: `decode(encode(req)) == req` where meaningful.
   - Parser fuzzing on malformed input never raises anything other than `AlicatParseError`.

### 6.2 Fixtures

- `tests/fixtures/responses/*.txt` holds captured `> send` / `< recv` sequences in a readable plaintext format:

  ```text
  # scenario: identify-flow-controller
  > A??M*
  < A M01 Alicat Scientific
  < A M02 ...
  ```

- `pyalicat.testing.record_session(device, scenario)` captures new fixtures from hardware runs.
- `pyalicat.testing.FakeTransportFromFixture(path)` loads a fixture into a `FakeTransport`.

### 6.3 Performance Suite (non-default)

- Single-device poll latency p50, p95, p99.
- Multi-device poll latency.
- Recorder scheduling jitter at 1, 10, 25, 50 Hz.
- CSV sink throughput.
- Postgres sink throughput if available.

### 6.4 CI Checks

- Ruff format + lint.
- `mypy --strict` (actually passing, no blanket ignores).
- `pytest -m "not hardware and not hardware_stateful and not hardware_destructive"` on Python 3.12 and 3.13.
- Coverage threshold: 90% overall, 95% for `protocol/`, `registry/`, `commands/`.
- `hatch build` (or `uv build`) must succeed; `twine check` on the wheel.
- `mkdocs build --strict`.
- `gen_codes.py` idempotency check: running regenerates the same `_codes_gen.py`.

## 7. Tooling and Packaging

- Build backend: **`hatchling`** (PEP 621, minimal config).
- Environment and lock: **`uv`** (already partially adopted — `uv.lock` exists in repo).
- Python: **≥ 3.12** (floor; aligns with existing project and gives us `asyncio.TaskGroup`, `asyncio.timeout()`, `ExceptionGroup`, and `asyncio.eager_task_factory`). **3.13 also tested in CI** — users additionally get the asyncio-aware REPL (`python -m asyncio`) and improved task cancellation semantics at no code cost.
- Source layout: **`src/pyalicat/`**.
- **Runtime deps (core):** `anyio`, `pyserial` (or the chosen backend after M1 benchmark). Nothing else.
- **Optional extras:**
  ```toml
  [project.optional-dependencies]
  postgres = ["asyncpg>=0.30"]
  parquet  = ["pyarrow>=16"]
  docs     = ["mkdocs-material", "mkdocstrings[python]"]
  dev      = ["pytest", "pytest-cov", "hypothesis", "ruff", "mypy", "pre-commit"]
  ```
  `csv` and `jsonl` sinks need no extras (stdlib only).
- **Explicitly avoided in core:** `asyncpg`, `pandas`, `scipy`, `numpy`, `pyarrow`, `pydantic`, `pydantic-settings`.
- Linting: `ruff` with `E, F, I, UP, B, SIM, ASYNC, PL, PT, RUF, D` (pydocstyle Google).
- Pre-commit: ruff-format, ruff-check, mypy, codespell.

## 8. Before / After

### Polling

```python
# before
dev = await Device.new_device("/dev/ttyUSB0")
data = await dev.poll()
mass = data["Mass_Flow"]   # "--" | float, type unclear

# after
async with await open_device("/dev/ttyUSB0") as dev:
    frame = await dev.poll()
    mass = frame.get_float("Mass_Flow")   # float | None, typed
```

### Setting gas

```python
# before
await dev.gas("N2", True)

# after (preferred)
await dev.gas(Gas.N2, save=True)

# after (string still accepted, alias-aware)
await dev.gas("N2", save=True)
```

### Logging 10 Hz to CSV

```python
# before: thread + queue + hardcoded Postgres creds + busy-wait

# after
from pyalicat import AlicatManager
from pyalicat.sinks.csv import CsvSink
from pyalicat.sinks.base import pipe
from pyalicat.streaming import record

async with AlicatManager() as mgr:
    await mgr.add("fuel", "/dev/ttyUSB0")
    await mgr.add("air",  "/dev/ttyUSB1")
    async with CsvSink("run.csv") as sink:
        await pipe(record(mgr, rate_hz=10, duration=60), sink)
```

### Custom downstream consumer (no sink)

```python
async for batch in record(mgr, rate_hz=10, duration=60):
    await kafka.send("flows", json.dumps({k: v.frame.as_dict() for k, v in batch.items()}))
```

### Adding a new command

**Before:** add a 20-line method to `device.py` with its own version check, split, coerce, zip.

**After:** create one `Command` subclass with `encode`/`decode`, one request dataclass, one response dataclass, one facade one-liner, one fixture-backed test. ~50 lines, all localized.

## 9. Command Coverage Tiers

### Tier 1 — Required for v1.0

- Identify / manufacturing info.
- Firmware version parse.
- Data frame format query/cache.
- `poll()` current data frame.
- `request()` selected statistics (averaging window supported).
- Gas query / set / list.
- Engineering units query / set (statistic-level and group-level).
- Flow tare; absolute pressure tare; gauge pressure tare.
- Setpoint query / set (controllers); legacy setpoint path for older supported firmware.
- Loop control variable.
- Manager poll / request.
- CSV + JSONL sinks.
- FakeTransport and fixture loader.

### Tier 2 — Follow-up within v1.x

- Totalizer: configure, query, reset.
- Standard/normal pressure & temperature references.
- Zero band.
- Analog output source & scaling.
- Streaming mode.
- Valve hold / hold closed / cancel hold / query valve drive.
- Ramp rate.
- Deadband.
- Blink / front panel.
- User data.
- Baud query/set with safety notes.
- Auto-tare; power-up tare.
- Parquet and Postgres sinks.

### Tier 3 — Advanced / requires extra hardware validation

- Custom gas mix creation / deletion / query.
- Remote tare action map.
- Factory restore (destructive).
- Controller PID gains.
- Batch mode.
- Overpressure.
- Power-up setpoint (destructive).
- Exhaust (destructive).

Tier 3 items land as commands with `destructive=True` and/or `experimental=True` and are marked experimental in docs until hardware-validated.

## 10. Milestones

Rough estimates. Each milestone ends in a tagged internal release with a working end-to-end path and green CI.

**M0 — Scaffolding (1–2 days)**
- `src/` layout. Hatchling + uv. Ruff + mypy --strict green on skeleton.
- Empty package modules. `py.typed`. CI pipeline.
- `errors.py` (with `ErrorContext`), `firmware.py`, `config.py` complete and tested.

**M1 — Transport + Protocol + One Command (2–3 days)**
- `Transport` protocol, `SerialTransport`, `FakeTransport`.
- Serial backend benchmark on hardware (p50/p99 latency); pick winner.
- Eager-task-factory A/B on the protocol client hot path; decision recorded.
- `AlicatProtocolClient` with one-in-flight lock, multiline read via `move_on_after`, `fail_after`-based timeouts (no `wait_for`).
- `registry/_codes_gen.py` generated from `codes.json`; `AliasRegistry` + three registry singletons; CI idempotency guard.
- `Command` + `ResponseMode` + `GAS_SELECT` end-to-end.
- Hardware test: open a real device and run `gas`.

**M2 — Identification, Polling, Facades (2–3 days)**
- Manufacturing info parser → `DeviceInfo`.
- Data frame table parser → `DataFrameFormat` with `Statistic` linkage.
- `Session` with command lock, firmware gating, destructive-op gating.
- `open_device`, `ModelRule` registry, `FlowMeter`, `FlowController`.
- `find_devices()` with multi-baudrate, `DiscoveryResult`.
- `poll()` + `request()` working end-to-end.
- Tier-1 unit tests + hardware read-only smoke test.

**M3 — Tier-1 Command Coverage (3–5 days)**
- Gas list; units get/set; all tare commands.
- Setpoint query/set (with range validation).
- Legacy setpoint path for older firmware.
- Loop control variable.
- Fixtures captured from hardware for each command.
- Facade methods wired; all async tests green.

**M4 — Manager + Recorder (1–2 days)**
- `AlicatManager` with `ErrorPolicy`, `DeviceResult`.
- `record()` with absolute-target scheduling; `Sample` model with midpoint timestamp; backpressure policies.
- `InMemorySink` and `CsvSink`, `JsonlSink`.
- `pipe()` helper.
- Drift assertions under simulated and real latency.

**M5 — Optional Sinks (2 days)**
- `ParquetSink` (rotation by size/time).
- `PostgresSink` with plain-dataclass config; `ensure_schema`; optional TimescaleDB.

**M6 — Sync Facade (1 day)**
- `BlockingPortal` wiring, per-context portal lifecycle.
- `@sync_version` decorator; `Alicat`, `AlicatManagerSync`.
- Parity tests: every async facade method has a sync counterpart.

**M7 — Tier-2 Commands + Streaming Mode (3–5 days)**
- Totalizer, STP references, zero band, analog out, user data.
- Valve hold / cancel / exhaust (destructive), query valve.
- Auto-tare; power-up tare.
- `StreamingSession` with state machine; stream mode tests against FakeTransport and hardware.
- Ramp and deadband.

**M8 — Docs + 1.0.0 (2 days)**
- mkdocs pages per §12.
- Migration guide from 0.0.x.
- API reference auto-generated from docstrings.
- Tag `v1.0.0`.

**M9 — Hardware Validation and Tier-3 (open-ended)**
- Tier-3 commands as budget allows, each with hardware-validated fixtures.
- Compatibility matrix (models × firmware versions).

**Total projected to v1.0: ~2.5–3.5 calendar weeks** of focused work, excluding Tier-3 and v2.x follow-ups (TCP transport, L-/P-series support).

## 11. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Serial backend choice wrong for target latency | M1 benchmark with real hardware (p50/p99); interface stays the same; swap is local. |
| Scope creep (Modbus, TCP, L-series) delays v1.0 | Interfaces yes, implementations no. Milestone gates. |
| Typed frames surprise users with missing fields | All optional fields default `None`. `as_dict()` escape hatch for raw access; `get_float()` helper. |
| `mypy --strict` blocks progress | Narrowly-scoped `# type: ignore[code]` with justification allowed; no blanket ignores. |
| `codes.json` drift across firmware | CI idempotency check; fixtures recorded per firmware version. |
| Single maintainer bus factor | Declarative command catalog, typed frames, and fixture tests are documentation. mkdocs auto-generates the command reference. |
| Streaming mode race with request/response | State machine rejects request/response while streaming; tests cover transitions. |
| Hardware tests require presence of device | Tiered markers (`hardware`/`stateful`/`destructive`); env vars for port selection; CI skips. |
| Postgres/Timescale assumptions leak into core | `PostgresSink` behind an extra; `sinks/` is an optional subpackage; plain dataclass config avoids even a `pydantic-settings` dep. |
| Physical damage from bad setpoint or destructive command | Setpoint range validation; `confirm=True` required for destructive ops; tiered hardware tests. |
| Sync facade hides thread-lifecycle bugs | Per-context portal by default; opt-in shared portal only for advanced users; parity test against async API. |
| Multiline read leaves stale bytes in buffer | `query_lines` with idle-timeout and drain-on-timeout behavior, fixture-tested. |

## 12. Documentation Plan

Pages under `docs/`:

- `index.md` — package purpose and quickstart.
- `installation.md` — core install, extras.
- `quickstart-async.md` — open, poll, request, setpoint.
- `quickstart-sync.md` — sync wrapper.
- `devices.md` — supported model families, firmware notes.
- `commands.md` — command groups and return models (auto-generated tables from the catalog).
- `data-frames.md` — dynamic data-frame formats and `Statistic` linkage.
- `logging.md` — recorder, sinks, backpressure.
- `streaming.md` — streaming mode and state transitions.
- `testing.md` — FakeTransport, fixtures, hardware test tiers.
- `safety.md` — destructive commands, confirm flag, tare conditions.
- `migration.md` — from 0.0.x to 1.0.
- `troubleshooting.md` — serial ports, timeouts, malformed responses, stale input, permissions.
- `api/` — mkdocstrings-generated reference.

## 13. Open Questions

Answer before implementation starts or during M0:

1. Package name: keep `pyalicat` and ship as v1.0 (breaking), or cut a new name like `pyalicat-next` and deprecate the old? **Recommendation:** keep `pyalicat`, tag v1.0.
2. Which firmware versions exist on your real devices? Drives what commands can be hardware-tested and which Tier-1 commands need both legacy and new paths.
3. Windows and/or macOS support wanted alongside Linux?
4. Do any workflows require streaming mode, or is request/response polling sufficient? (Determines Tier priority for streaming.)
5. Acquisition target rates — 10 Hz? 25 Hz? 50 Hz? How many devices simultaneously? (Sizes sink batching, buffer sizes, backpressure defaults.)
6. TimescaleDB or plain Postgres for `PostgresSink` defaults?
7. Should `request()` accept `Statistic | str` or require the enum? (Current plan: accept both.)
8. Units library integration (`pint`)? Not in v1.0, but reserve field names/types accordingly.
9. Serial backend: `pyserial`, `anyserial`, or `pyserial-asyncio` after M1 benchmark?
10. Should generated enums be committed to the repo, generated at build time, or generated at import time? (Current plan: build-time, committed under `_codes_gen.py`.)
11. Should `PollFrame` be a generic `DataFrame` plus typed views, or one large optional-field dataclass? (Current plan: generic `DataFrame`; typed views as a future enhancement.)
12. Hardware availability for the full Tier-1 fixture capture session — can you block off time for a single recording run?
13. Do you want a CLI (`python -m pyalicat discover`, `python -m pyalicat poll /dev/ttyUSB0`) in v1.0, or is API-only enough?

## 14. Success Criteria

The rewrite is done when:

- Adding a new Alicat command requires one `Command` subclass, one request dataclass, one response dataclass, one facade one-liner, and one fixture-backed test — nothing else.
- 95%+ of tests run with no hardware attached.
- A timeout is visibly distinct from a malformed response, an unsupported command, or a device error — each is a different exception with full `ErrorContext`.
- Concurrent multi-device acquisition is deterministic and latency-bounded; same-port requests cannot interleave bytes.
- A 10 Hz recorder runs for an hour without drift exceeding one sample period and without dropping samples under default backpressure.
- `0`, `False`, and `None` are handled correctly and distinctly by every command encoder.
- Multiline responses never leave stale input behind for the next command.
- Data-frame format is modeled, cached, and linked to `Statistic`.
- No credentials in source. No hardcoded database endpoints. No built-in `print` statements.
- Public return types are all documented, typed, and frozen.
- Optional dependencies are truly optional: `pip install pyalicat` works with zero DB, data-science, or validation libs.
- Core import does not require `pydantic`, `pydantic-settings`, `pandas`, `numpy`, `pyarrow`, or `asyncpg`.
- CI enforces format, lint, types, tests, docs build, wheel build, and codegen idempotency.
- The package supports new firmware by editing `codes.json` and regenerating; new device families by adding one `ModelRule` and one subclass.
- Destructive operations cannot fire without `confirm=True`.
- Hardware tests are gated by safety tier (`hardware`, `hardware_stateful`, `hardware_destructive`).

---
