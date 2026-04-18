# Data frames

The Alicat poll response — the `<uid>\r` command's reply — is a
**device-dependent** line of whitespace-separated fields. Alicat
firmware advertises the layout via `??D*`, and devices vary on the
layout dialect, the set of present fields, and whether a given field
resolves to a recognised engineering unit. The library models every
layer of that explicitly so polling is type-safe end to end.

See [Design](design.md) §5.4, §5.6, and §15.3 for the authoritative
architecture. Source: [devices/data_frame.py](../src/alicatlib/devices/data_frame.py).

## The problem

A poll reply on a 10v20 flow controller looks roughly like:

```
A +014.70 +023.45 +0050.0 +0050.0   Air MOV HLD
```

The column order, count, and types differ per device — across firmware
families, across device kinds, across optional features. A fixed parser
can't survive this; the library caches the advertised format at open
time and decodes every subsequent poll through that cached
[`DataFrameFormat`](../src/alicatlib/devices/data_frame.py#L144).

## `??D*` dialects

[`DataFrameFormatFlavor`](../src/alicatlib/devices/data_frame.py#L51)
models two observed dialects:

| Flavor | Header shape | Observed devices |
| --- | --- | --- |
| `DEFAULT` | `<uid> D00 ID_ NAME... TYPE... WIDTH NOTES...` | 6v21, 8v17, 8v30, 10v04, 10v20 |
| `LEGACY` | `<uid>  D00 NAME... TYPE... MinVal MaxVal UNITS...` | 5v12 |

Detection is **by header, not firmware family**. The transition
happened between `5v12` and `6v21` — both V1_V7-family devices — so
the dialect does not correlate with family. `SIGNED` and `VARIABLE_V8`
are reserved for future captures if a third dialect emerges; currently
unused.

The `DEFAULT` dialect carries a stat-code column and marks conditional
fields with a leading `*` on the name. `LEGACY` does neither — fields
are always required, units are inline in a single column, and the
types are `signed` / `char` rather than `s decimal` / `string`.

GP strips a `\x08` padding byte from `??M*` replies; `??D*` on GP is
currently not captured and identification uses `model_hint` to skip
the probe. See [devices.md](devices.md) for the GP identification path.

## Post-`??D*` sweep

Two command sweeps run *after* `??D*` parses at open time:

1. **`DCU` (engineering-units query).** For every numeric
   [`DataFrameField`](../src/alicatlib/devices/data_frame.py#L87) whose
   `unit` the `??D*` parser left `None` (because the advertisement
   carries only a label, not a registry-resolvable code in some
   dialects), the factory issues `DCU <stat>` to query the active
   unit. The format is rebuilt with the resolved unit; per-field
   failures (firmware gate, rejection, timeout) leave the slot
   unresolved rather than failing the open.
2. **`FPF` (full-scale query).** For every numeric field, the factory
   issues `FPF <stat>` to populate `DeviceInfo.full_scale`. Controller
   sessions also pre-cache the loop-control variable via `LV`, so
   `FlowController.setpoint` can range-check
   `full_scale[lv.statistic]` pre-I/O.

GP is skipped on both sweeps; the `---` placeholder that real devices
return for absent statistics is filtered out.

## `DataFrameField`

```python
@dataclass(frozen=True, slots=True)
class DataFrameField:
    name: str                       # canonical, e.g. "Mass_Flow"
    raw_name: str                   # verbatim from device
    type_name: str                  # "decimal" / "integer" / "text" …
    statistic: Statistic | None     # registry linkage
    unit: Unit | None               # DCU-bound engineering unit
    conditional: bool               # True when *-marked in ??D*
    parser: Callable[[str], float | str | None]
```

`statistic` is the bridge to
[`Statistic`](../src/alicatlib/registry/__init__.py) — a typed enum
over the full 98-entry registry. Binding `statistic` here lets sinks,
dashboards, and analysis code address columns by typed identifier
instead of a wire-name string that could be renamed in a firmware
update. See below for `get_statistic(stat)`.

`unit` is bound at open time. The data frame itself carries no units
— the device's active `DCU` binding is what tells you the value's
engineering unit at read time. A runtime `DCU` write re-binds, and
`session.refresh_data_frame_format()` picks up the change.

`conditional` marks fields that appear on the wire only when their
condition is met (for example, bidirectional valve percentage on a
bidirectional controller). `DEFAULT`-dialect `??D*` replies mark them
with a leading `*`; the parser tail-matches conditionals after the
required fields are consumed.

## Parsing

[`DataFrameFormat.parse(raw: bytes) -> ParsedFrame`](../src/alicatlib/devices/data_frame.py#L162)
is **pure** — no timing, no logging, no clocks. The factory and the
polling path wrap it separately in `DataFrame.from_parsed(...)` at
the site that captures `received_at` / `monotonic_ns`. Keeping the
split means parser unit tests are clock-free (no freeze-time mocking).

The algorithm (design §5.6):

1. Tokenise on whitespace; first token is the device's unit ID.
2. Match required (non-conditional) fields left-to-right against the
   leading tokens — they always appear.
3. Walk surplus tokens. Any token matching a
   [`StatusCode`](../src/alicatlib/devices/models.py) value collapses
   into `ParsedFrame.status`; remaining tokens are assigned to
   conditional fields in declared order.
4. Conditional fields that never receive a token are simply **absent**
   from `ParsedFrame.values` — they are not `None`. This is
   load-bearing for downstream sinks: an absent column is distinct
   from a column whose value is the `--` sentinel (which *does* land
   as `None` via `parse_optional_float`).

The parser raises `AlicatParseError` on empty frames, non-ASCII bytes,
or frames with fewer tokens than required fields. Malformed individual
values (a field declared `decimal` that parses as text) return
`None` via the per-field `parser` callable; the frame overall still
decodes.

## `DataFrame`

[`DataFrame`](../src/alicatlib/devices/data_frame.py#L240) is the
public polling result —
[`ParsedFrame`](../src/alicatlib/devices/data_frame.py#L127) plus
`received_at: datetime` (UTC) and `monotonic_ns: int`.

Three accessors:

### `values: Mapping[str, float | str | None]`

Direct access by canonical name. Strict — absent keys raise `KeyError`
on subscript, and there is no type coercion beyond what each field's
`parser` applies.

```python
frame.values["Mass_Flow"]          # 14.7 (float)
frame.values["Gas"]                # "Air" (str)
frame.values["Setpoint"]           # KeyError on a meter
```

### `get_float(name: str) -> float | None`

"Forgiving" accessor: absent or non-numeric fields return `None`; no
exception. Use when the caller accepts missing values.

```python
frame.get_float("Mass_Flow")       # 14.7
frame.get_float("Gas")             # None (not numeric)
frame.get_float("Nonexistent")     # None (absent)
```

### `get_statistic(stat: Statistic) -> float | str | None`

Keyed by typed `Statistic`. Prefer this over `get_float` when the
caller has a typed statistic — IDE-completable, robust to wire-name
renames across firmware versions.

```python
from alicatlib import Statistic
frame.get_statistic(Statistic.MASS_FLOW)         # 14.7
frame.get_statistic(Statistic.PRESSURE_GAUGE)    # None on a mass-flow device
```

### `as_dict() -> dict[str, float | str | None]`

Flatten to JSON/CSV-friendly dict for sinks. Produces
`{field_name: value, "status": "HLD,OPL", "received_at": iso8601}` —
status codes collapse into a single comma-joined sorted string (empty
when no codes are active) so downstream schema is stable across rows.

```python
frame.as_dict()
# {"Mass_Flow": 14.7, "Setpoint": 50.0, ..., "status": "HLD", "received_at": "2026-04-18T10:22:01.123456+00:00"}
```

The single `status` key is a deliberate choice — per-code boolean
columns would explode the sink schema unpredictably across devices.
Callers that need per-code columns wrap this themselves.

## Status codes

[`StatusCode`](../src/alicatlib/devices/models.py) is a `StrEnum`
covering every status marker the primer documents: `HLD`, `OPL`,
`MOV`, `LCK`, `OVR`, etc. The parser builds
`status: frozenset[StatusCode]` from the tail tokens, which makes
code-equality checks cheap:

```python
from alicatlib.devices.models import StatusCode
if StatusCode.HLD in frame.status:
    print("valves on hold")
```

Unknown status tokens (never observed but possible in a firmware
update) are discarded by the parser rather than raising, so a new
code doesn't brick polling. The code is still in the raw reply for
diagnostic scripts that need it.

## Refreshing the format

The cached format is immutable. Any change produces a new
`DataFrameFormat` via
`session.refresh_data_frame_format()` — useful after a runtime `DCU`
write (unit re-binding) or `FDF` (field re-ordering). Polling picks
up the new format on the next call; the sample recorder captures its
format reference at emit time, so running under `record()` across a
refresh is safe but emits two shapes.

## Relation to the sample recorder

The [recorder](logging.md) ships each poll as a
[`Sample`](../src/alicatlib/streaming/sample.py) wrapping the
`DataFrame`. `sample_to_row` flattens via `DataFrame.as_dict()` —
the same method called directly here. The sink's row shape is
therefore exactly the data-frame shape, with recorder-side provenance
(`device`, `unit_id`, timestamps, `latency_s`) prepended. See
[logging.md](logging.md#stable-row-layout-sample_to_row) for the full
row layout.
