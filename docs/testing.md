# Testing

95%+ of the test suite runs with no hardware attached —
[`FakeTransport`](../src/alicatlib/transport/fake.py) replays scripted
byte exchanges, the fixture-file format lets captured hardware
sessions round-trip through code review, and the
[`device_matrix.yaml`](../tests/fixtures/device_matrix.yaml) fixture
is the empirical source of truth for per-firmware command
availability. See [Design](design.md) §6 for the full strategy.

## Running tests

```bash
uv run pytest                              # fast suite, no hardware
uv run pytest -m hardware                  # read-only hardware tests (needs ALICATLIB_TEST_*_PORT)
uv run pytest -m hardware_stateful         # requires ALICATLIB_ENABLE_STATEFUL_TESTS=1
uv run pytest -m hardware_destructive      # requires ALICATLIB_ENABLE_DESTRUCTIVE_TESTS=1
```

Default `pytest` run excludes every `hardware*` marker, so the fast
suite is always hermetic. Coverage runs via
`uv run pytest --cov --cov-report=xml`.

## Hardware test tiers

| Marker | What it does | Opt-in |
| --- | --- | --- |
| `hardware` | Read-only — identify, poll, query commands. Never changes device state. | `ALICATLIB_TEST_*_PORT` env vars set |
| `hardware_stateful` | Changes device state (gas, setpoint, tare, unit-id). Reverts before exit where possible. | `ALICATLIB_ENABLE_STATEFUL_TESTS=1` |
| `hardware_destructive` | Factory reset, baud change, valve exhaust. No automatic revert. | `ALICATLIB_ENABLE_DESTRUCTIVE_TESTS=1` |
| `slow` | Excluded from the fast CI run; used for latency / soak benchmarks. | `-m slow` explicit pass |

Markers are defined in
[pyproject.toml](../pyproject.toml#L299) under `[tool.pytest.ini_options]`;
opt-in env vars are read by `tests/conftest.py`.

## FakeTransport

[`FakeTransport`](../src/alicatlib/transport/fake.py) satisfies the
[`Transport` Protocol](../src/alicatlib/transport/base.py) with a scripted
reply table. Writes are recorded; reads drain from the scripted
replies for that write. Forced-timeout and short-read knobs let tests
exercise the protocol client's error paths deterministically.

### Inline script

```python
import pytest
from alicatlib.testing import FakeTransport

@pytest.mark.anyio
async def test_gas_select_encode() -> None:
    fake = FakeTransport({b"AGS 5\r": [b"A 5 N2 Nitrogen\r"]})
    # drive a Session through the fake; assert fake.writes, assert decoded reply
```

One key per unique write; one list of reply chunks per key. Multiple
lines for multiline commands concatenate inside one list entry —
`??M*` scripts look like:

```python
FakeTransport({
    b"A??M*\r": [
        b"A M01 Alicat Scientific\r"
        b"A M02 www.example.com\r"
        b"A M03 +1 555-0000\r"
        # ...
    ],
})
```

### Forced errors

`FakeTransport(..., fail=FailPlan(read_timeout_at_call=2))` fires a
`TimeoutError` on the second read call; useful for testing retry /
recovery paths. See the `FakeTransport` docstring for the full knob
list.

### `@pytest.mark.anyio`

Async tests use the AnyIO pytest plugin — **not** `pytest-asyncio`.
The two auto-modes disagree and `pytest-asyncio` wraps fixtures in
fresh tasks that break cancel scopes. `tests/conftest.py` wires up a
parametrised `anyio_backend` fixture that runs every async test
against both `asyncio` and `trio` for cross-backend coverage.

## Fixture format

Captured hardware traffic lives in
[tests/fixtures/responses/](../tests/fixtures/responses/) as plaintext
`.txt` files. The format is deliberately skimmable:

```text
# scenario: Set active gas to N2 (code 8) via GS command
# Response is "<unit_id> <code> <short> <long_name>".

> AGS 8
< A 8 N2 Nitrogen
```

Rules ([testing.py](../src/alicatlib/testing.py)):

- Lines starting with `#` are comments.
- Blank lines are ignored.
- `>` introduces a send. The carriage-return terminator is appended
  automatically so the fixture stays human-readable.
- `<` introduces one reply line (`\r`-terminated).
- Multiple `<` lines after a single `>` concatenate into one scripted
  reply — the right shape for multiline commands.
- Duplicate `>` entries are a file-format error, not a silent
  overwrite. Two writes of the same bytes must use two separate `>`
  blocks in order.

### Loading a fixture

```python
from alicatlib.testing import FakeTransportFromFixture

fake = FakeTransportFromFixture("tests/fixtures/responses/gas_select_n2.txt")
```

`FakeTransportFromFixture` is a drop-in replacement for the
dictionary-constructed `FakeTransport`; the file is parsed once at
construction and the scripted replies are populated from the `>` /
`<` pairs.

### Capturing new fixtures

An automated `record_session(device, scenario)` helper is planned
but **not shipped yet** — the docstring in
[testing.py:33-35](../src/alicatlib/testing.py#L33-L35) notes it lands
with the hardware integration suite.

For now, capture fixtures by hand or by pasting from a `--log-level=DEBUG`
transcript. The protocol client emits one `tx` / `rx` DEBUG event per
write / read on the `alicatlib.protocol` logger; translate the
structured `{direction, raw, len}` extras into `>` / `<` lines. See
[troubleshooting.md §Getting raw wire bytes](troubleshooting.md#getting-raw-wire-bytes).

## `device_matrix.yaml`

[tests/fixtures/device_matrix.yaml](../tests/fixtures/device_matrix.yaml)
is the empirical behaviour matrix — one `(device_model, firmware,
captured_at)` triple per entry, with per-command status across the
whole catalog:

```yaml
- model: MC-100SCCM-D
  firmware: GP07R100
  family: GP
  captured_at: "2026-04-17"
  prefix:
    reads: none
    writes: dollar
  dialects:
    mm: gp_short_code_backspace_padded
    dd: legacy_backspace_padded
  commands:
    poll:          supported
    ve:            silent
    mm:            supported
    dv:            rejected        # firmware-gate (GP family)
```

Status taxonomy (defined at the top of the YAML):

| Status | Meaning |
| --- | --- |
| `supported` | Device responds usefully; reply parses per the command spec. |
| `rejected` | Device returns `?` *or* a library gate blocks pre-I/O (firmware / kind / capability / media). |
| `silent` | Device returns nothing within a reasonable timeout. |
| `fallback` | Device returns a data frame instead of the proper reply (e.g. pre-10v05 with display lock). |
| `placeholder` | Degraded reply (`A 1 ---`, `A +0 1 ---`, `A Feature Not Enabled`). |
| `adc_counts` | Pre-10v05 `DCU` returns raw ADC counts — a different meaning entirely. |
| `untested` | No capture yet — default. |

The matrix is validated against every command spec's
`firmware_families` declaration by
[tests/unit/test_device_matrix.py](../tests/unit/test_device_matrix.py):
an entry marked `supported` on a family the spec doesn't allow fails
CI, and vice versa. This is the load-bearing cross-check that keeps
the command catalog honest about real-hardware capture evidence.

New captures **append** to the file — never silently edit an
existing entry unless the original capture was mis-transcribed (and
in that case, the commit message must document the re-transcription).

## Coverage-layer guidance

| Layer | Primary test strategy |
| --- | --- |
| Parsers (`protocol/parser.py`, `devices/data_frame.py`) | Pure-function unit tests against raw fixture bytes. Clock-free; no `FakeTransport` needed. |
| Commands (`commands/*`) | `encode` / `decode` round-trip against fixture replies; one test per spec covers the happy path and each gate. |
| Session gates | `FakeTransport` + `Session.execute`; assert the right typed exception and zero tx when a gate fires. |
| Factory + discovery | `FakeTransportFromFixture` against captured identification traces. |
| Manager + recorder | Lightweight `PollSource` stub (design §5.14); no full transport stack. |
| Sinks | Per-backend fixtures; `InMemorySink` as the oracle for `sample_to_row`. |
| Sync parity | Dedicated parity test compares every async / sync method pair by parameter name, kind, and default (design §5.16). |

## Hypothesis

Property-based tests use [Hypothesis](https://hypothesis.works) —
configured via
`tests/conftest.py`. Useful for encoder invariants (0 / False / None
distinct, round-trip through parse, no ASCII-outside-range payloads),
data-frame parser robustness, and sink row-layout stability.

## Pre-push + CI

The pre-push hook runs `mypy` via `uv run --frozen` against the same
dep groups CI uses, so "works locally" matches "works in CI" by
construction. See [.pre-commit-config.yaml](../.pre-commit-config.yaml).

CI runs ruff (format + lint), mypy + pyright, the test suite across
Python 3.13/3.14 × Linux/macOS/Windows, `uv build` +
`twine check --strict`, and a codegen idempotency guard on the
generated registry. See
[.github/workflows/ci.yml](../.github/workflows/ci.yml).
