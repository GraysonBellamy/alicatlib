# Commands

Every Alicat command is one [`Command`](../src/alicatlib/commands/base.py#L152)
subclass — a frozen dataclass carrying metadata (name, token, response
mode, firmware / device-kind / media / capability gates, destructive
flag) plus pure `encode` / `decode` methods. The metadata is load-bearing:
the [`Session`](../src/alicatlib/devices/session.py) reads every gate
*before* dispatching, so commands fail fast with typed errors rather
than silently producing a bad wire payload.

This page is a catalogue by module. See [Design](design.md) §5.4 for
the command layer's architecture, §5.5–§5.8 for request/response
modelling, and the individual module docstrings for the authoritative
per-command detail. Full API reference is in the
[api](api/index.md) pages.

## Anatomy of a command

```python
from alicatlib.commands import Command, ResponseMode, Capability
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.medium import Medium
```

Key [`Command`](../src/alicatlib/commands/base.py#L152) fields:

| Field | Purpose |
| --- | --- |
| `name` / `token` | Python-friendly name; protocol token (emitted verbatim). |
| `response_mode` | [`ResponseMode`](../src/alicatlib/commands/base.py#L48) — `NONE` (write-only), `LINE` (single-line), `LINES` (multiline), `STREAM` (enters streaming mode). |
| `device_kinds` | Which `DeviceKind` values this command applies to. Empty = any. |
| `media` | Bitwise `Medium` gate. Default `GAS \| LIQUID` is medium-agnostic; gas-specific commands narrow to `GAS`, liquid-specific to `LIQUID`. |
| `required_capabilities` | Capability bits the device must have — see [Devices §Capability probes](devices.md#capability-probes). |
| `min_firmware` / `max_firmware` | Supported firmware range within a family. Cross-family comparison raises `TypeError`. |
| `firmware_families` | Family-level gate (monotonic; declare a family only when every captured device in it implements the command). Empty = any. |
| `destructive` | Requires explicit `confirm=True` at the session layer. |
| `experimental` | Emits a deprecation-style warning on use. |
| `case_sensitive` | Suppress hypothetical upstream lowercase normalisation. Only `FACTORY RESTORE ALL` needs this today. |
| `prefix_less` | Command opts out of the unit-id prefix (e.g. `@@` stop-stream, `@@ {uid}` start-stream). |
| `expected_lines` / `is_complete` | Multiline termination contract for `LINES` commands. `is_complete` takes priority; see design §5.2. |

Every `Command` operates through `Session.execute(spec, request)`
(async) or `session.execute(spec, request)` (sync facade). The request
is a typed per-command dataclass; the response is a typed result
model. Neither the session nor the command layer does any raw-string
juggling at the public surface.

## Commands by module

### Gas — [commands/gas.py](../src/alicatlib/commands/gas.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `GAS_LIST` | `??G` | `LINES` | `Device.gas_list()` — 98-entry gas table |
| `GAS_SELECT` | `GS` | `LINE` | `Device.gas(gas, save=False)` (modern path) |
| `GAS_SELECT_LEGACY` | `G` | `LINE` | Legacy-family routing of `Device.gas(...)` (GP, pre-9v00 V1_V7) |

`media=GAS` on all three. `GasState` response model carries the
selected gas, selected code, and optional save flag echo.

### Units — [commands/units.py](../src/alicatlib/commands/units.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `ENGINEERING_UNITS` | `DCU` | `LINE` | `Device.engineering_units(...)` — unit-code per statistic |
| `FULL_SCALE_QUERY` | `FPF` | `LINE` | `Device.full_scale(statistic)` — full-scale per statistic |

`DCU` and `FPF` also run as a post-`??D*` sweep in the factory to bind
[data-frame](data-frames.md) fields to their active `Unit` and
populate `DeviceInfo.full_scale`.

### Setpoint — [commands/setpoint.py](../src/alicatlib/commands/setpoint.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `SETPOINT` | `LS` / `S` | `LINE` | `FlowController.setpoint(value, unit)` — modern path on V10/9v00+ |
| `SETPOINT_LEGACY` | `S` | `LINE` | Legacy-path routing of `FlowController.setpoint(...)` |
| `SETPOINT_SOURCE` | `LSS` | `LINE` | `FlowController.setpoint_source(mode, save=False)` — Serial / Analog / User-knob |

Setpoint range validation runs pre-I/O: `FlowController.setpoint`
consults `DeviceInfo.full_scale[lv.statistic]` and raises
`AlicatValidationError` on out-of-range values. Bidirectional devices
accept `[-fs, +fs]`; unidirectional accept `[0, +fs]` (negative on
unidirectional hits the `BIDIRECTIONAL` capability gate first). See
[safety.md](safety.md) for the V1_V7 / pre-9v00 caveat.

### Loop control — [commands/loop_control.py](../src/alicatlib/commands/loop_control.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `LOOP_CONTROL_VARIABLE` | `LV` | `LINE` | `FlowController.loop_control_variable(variable=None)` |

Controller sessions cache `LV` at startup so `setpoint` range-checks
against the right statistic's full-scale.

### Control — [commands/control.py](../src/alicatlib/commands/control.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `RAMP_RATE` | `SR` | `LINE` | `FlowController.ramp_rate(...)` (min firmware: V1_V7 7v11+) |
| `DEADBAND_LIMIT` | `LCDB` | `LINE` | `FlowController.deadband_limit(...)` |

Both live on the shared `_ControllerMixin`, so `PressureController`
inherits them without duplication.

### Valve — [commands/valve.py](../src/alicatlib/commands/valve.py)

| Command | Token | Mode | Surface | Destructive |
| --- | --- | --- | --- | --- |
| `HOLD_VALVES` | `HP` | `LINE` | `FlowController.hold_valves()` | No (non-destructive hold) |
| `HOLD_VALVES_CLOSED` | `HC` | `LINE` | `FlowController.hold_valves_closed(confirm=True)` | **Yes** |
| `CANCEL_VALVE_HOLD` | `C` | `LINE` | `FlowController.cancel_valve_hold()` | No |
| `VALVE_DRIVE` | `VD` | `LINE` | `FlowController.valve_drive()` (min firmware: V8_V9 8v18+) | No |

`VD` decoder accepts 1–4 fixed-width percentage columns; physical
valve count is a capability question, not a column-count one.

### Data readings — [commands/data_readings.py](../src/alicatlib/commands/data_readings.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `ZERO_BAND` | `DCZ` | `LINE` | `Device.zero_band(...)` |
| `AVERAGE_TIMING` | `DCA` | `LINE` | `Device.average_timing(...)` |
| `STP_NTP_PRESSURE` | `DCFRP` | `LINE` | `Device.stp_ntp_pressure(mode=...)` — STP or NTP |
| `STP_NTP_TEMPERATURE` | `DCFRT` | `LINE` | `Device.stp_ntp_temperature(mode=...)` |

`DCA` accepts both the 2-field real-10v20 reply shape
(`<uid> <averaging_ms>`) and the primer's 3-field shape with echoed
statistic code; the facade re-populates the statistic from the request.

### Tare — [commands/tare.py](../src/alicatlib/commands/tare.py)

| Command | Token | Mode | Surface | Capability |
| --- | --- | --- | --- | --- |
| `TARE_FLOW` | `T` | `LINE` | `Device.tare_flow()` | — |
| `TARE_GAUGE_PRESSURE` | `TP` | `LINE` | `Device.tare_gauge_pressure()` | — |
| `TARE_ABSOLUTE_PRESSURE` | `PC` | `LINE` | `Device.tare_absolute_pressure()` | `TAREABLE_ABSOLUTE_PRESSURE` |
| `AUTO_TARE` | `ZCA` | `LINE` | `FlowController.auto_tare(enabled, delay_s=...)` | — |
| `POWER_UP_TARE` | `ZCP` | `LINE` | `Device.power_up_tare(...)` | — |

`AUTO_TARE` disable form emits `ZCA 0` (3-token reply) — the primer's
`ZCA 0 0` rejects with `?` on real 10v20. See design §15.3.

Tare preconditions (no flow for flow tare, line depressurized for
pressure tare) are caller responsibility; see [safety.md](safety.md).

### Display — [commands/display.py](../src/alicatlib/commands/display.py)

| Command | Token | Mode | Surface | Capability |
| --- | --- | --- | --- | --- |
| `BLINK_DISPLAY` | `FFP` | `LINE` | `Device.blink_display(...)` | `DISPLAY` |
| `LOCK_DISPLAY` | `L` | `LINE` | `Device.lock_display()` | `DISPLAY` |
| `UNLOCK_DISPLAY` | `U` | `LINE` | `Device.unlock_display()` | **intentionally not gated** |

`unlock_display` is the safety escape for a locked device — always
callable. See [safety.md](safety.md) for the V1_V7 display-lock
recovery note.

### User data — [commands/user_data.py](../src/alicatlib/commands/user_data.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `USER_DATA` | `UD` | `LINE` | `Device.user_data(slot, value=None)` |

Value is validated pre-I/O: ASCII only, length-bounded, no `\r` /
`\n`. Empty-slot reply is 1-field (`<uid>`); decoder returns
`slot=-1` and the facade refills from the request.

### Analog output — [commands/output.py](../src/alicatlib/commands/output.py)

| Command | Token | Mode | Surface | Capability |
| --- | --- | --- | --- | --- |
| `ANALOG_OUTPUT_SOURCE` | `ASOCV` | `LINE` | `Device.analog_output_source(...)` | `ANALOG_OUTPUT` |

### Totalizer — [commands/totalizer.py](../src/alicatlib/commands/totalizer.py)

| Command | Token | Mode | Surface | Destructive |
| --- | --- | --- | --- | --- |
| `TOTALIZER_CONFIG` | `TC` | `LINE` | `FlowController.totalizer_config(...)` | No |
| `TOTALIZER_RESET` | `T <n>` | `LINE` | `FlowController.totalizer_reset(id, confirm=True)` | **Yes** |
| `TOTALIZER_RESET_PEAK` | `TP <n>` | `LINE` | `FlowController.totalizer_reset_peak(id, confirm=True)` | **Yes** |
| `TOTALIZER_SAVE` | `TCR` | `LINE` | `FlowController.totalizer_save()` | No |

`T` / `TP` reset encoders always emit the numeric totalizer argument
so the wire form cannot degrade into bare `T\r` / `TP\r`, which are
reserved for `TARE_FLOW` / `TARE_GAUGE_PRESSURE`. The invariant is
pinned by dedicated unit tests.

### Polling — [commands/polling.py](../src/alicatlib/commands/polling.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `POLL_DATA` | `{uid}` | `LINE` | `Device.poll()` — the primary acquisition primitive |
| `REQUEST_DATA` | `DV` | `LINE` | `Device.request(statistics, *, averaging_ms=1)` — on-demand statistic read |

`POLL_DATA` decodes through the cached
[`DataFrameFormat`](data-frames.md). `REQUEST_DATA` has a unique
wire shape — the reply carries no unit-ID prefix; invalid statistics
return a column-width run of dashes per slot and surface as `None`
in `MeasurementSet`.

### Streaming — [commands/streaming.py](../src/alicatlib/commands/streaming.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `STREAMING_RATE` | `NCS` | `LINE` | `Device.streaming_rate(rate_ms=...)` (min firmware: V10 10v05+) |

Mode-transition bytes for streaming are **not** normal commands —
start-stream is `{uid}@ @\r` and stop-stream is `@@ {uid}\r`, both
written directly under the port lock from
[`StreamingSession`](../src/alicatlib/devices/streaming.py). See
[streaming.md](streaming.md) for the full flow.

### System / identification — [commands/system.py](../src/alicatlib/commands/system.py)

| Command | Token | Mode | Surface |
| --- | --- | --- | --- |
| `VE_QUERY` | `VE` | `LINE` | Factory-only; runs during identification |
| `MANUFACTURING_INFO` | `??M*` | `LINES` | Factory-only; 10-line table |
| `DATA_FRAME_FORMAT_QUERY` | `??D*` | `LINES` | Factory + `session.refresh_data_frame_format()` |

These are session-private in normal use — the factory runs them once
at open. Users generally don't hit `VE` / `??M*` / `??D*` directly,
but they're part of the public catalog for test fixtures and
diagnostic scripts.

## Destructive commands

Every command with `destructive=True` requires `confirm=True` at the
facade layer and raises `AlicatValidationError` *pre-I/O* otherwise.
See [safety.md](safety.md) for the full list and rationale. The
library never ships "off by default" destructive paths that activate
on a single keyword argument — destructive means `confirm=True` in the
caller, every call site, every time.

## Experimental commands

Commands marked `experimental=True` emit a `DeprecationWarning`-style
warning on use and may change in wire shape or return type before
being promoted to stable. None ship in v1.0; the flag exists so future
Tier-3 commands can land behind it.

## Adding a new command

Per design §5.4, adding a command is ~50 lines and localised:

1. Declare the request / response dataclasses in the relevant
   `alicatlib.commands.<module>` file.
2. Declare the `Command` subclass with its metadata gates.
3. Implement `encode(ctx, request) -> bytes` and
   `decode(response, ctx) -> Resp`.
4. Add the facade method on the correct `Device` subclass.
5. Add the sync wrapper on the parallel `alicatlib.sync.device` class.
   Parity tests fail CI if this is skipped.
6. Write a `FakeTransport` fixture test; see [testing.md](testing.md).

No manager / recorder / sink changes are needed — sample flattening
reads `DataFrame.as_dict()`, which is schema-stable across commands.
