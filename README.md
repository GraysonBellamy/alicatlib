# alicatlib

Python library for [Alicat Scientific](https://www.alicat.com/) instruments
over serial — the full **{flow, pressure} × {meter, controller} × {gas,
liquid}** matrix, plus the CODA Coriolis line. Covered prefixes include
`M-` / `MC-` gas mass flow, `P-` / `PC-` gas pressure, `L-` / `LC-`
liquid flow, the K-family (`K-` / `KM-` / `KC-` / `KF-` / `KG-`) CODA
Coriolis prefixes, and all documented specialty variants (`MCDW-`,
`PCD-`, `LCR-`, `BASIS-`, `SFF-`, …). The `Medium` model is flexible
enough to handle devices configured for gas, liquid, or both — users
narrow to the specific unit configuration via `assume_media=` on
`open_device`.

`alicatlib` is focused on correctness, typed APIs, and reliable multi-device
acquisition.

> Status: beta. Documentation and release prep in progress; see
> [docs/design.md](docs/design.md) for the architecture and
> remaining future work.

## Highlights

- **Typed end to end.** `Gas.N2`, `Unit.SCCM`, `Statistic.MASS_FLOW`, frozen
  dataclass responses. `py.typed` shipped. `mypy --strict` passes.
- **Declarative commands.** One `Command` object per Alicat command with
  encoding, decoding, firmware gating, device-kind gating, and medium gating
  (gas vs. liquid, for CODA and friends) as metadata — adding a new command
  is ~50 lines.
- **Typed errors.** Distinct exception types for timeout, malformed response,
  unsupported firmware, and device error markers — all carrying structured
  `ErrorContext` for debuggability.
- **Safety.** Destructive operations (factory reset, baud change, exhaust)
  require `confirm=True`. Setpoints are range-checked before any I/O.
- **Multi-device, correctly.** `AlicatManager` runs many devices concurrently;
  same-port requests serialize via a shared lock, different ports run in
  parallel, resources unwind cleanly on partial-open failures.
- **Acquisition built in.** `record()` drives one or more devices at an
  absolute-target cadence with no cumulative drift, and `pipe()` drains
  samples into any `SampleSink`. First-party sinks: `InMemorySink`,
  `CsvSink`, `JsonlSink`, `SqliteSink` (stdlib WAL), plus `ParquetSink`
  and `PostgresSink` behind extras.
- **Streaming mode.** `dev.stream(...)` opens a bounded-memory
  `StreamingSession` that normalises the wire and fast-fails any
  concurrent request/response command on the same bus; `NCS` sets the
  device's continuous-stream rate on V10 firmware 10v05+.
- **Swappable transports.** `SerialTransport` for hardware, `FakeTransport` for
  tests; TCP / Modbus can land behind the same interface later.
- **Sync or async.** The core is `async` (built on `anyio`), and a sync facade
  (`alicatlib.sync.Alicat`) wraps it for scripts, notebooks, and REPLs.
- **Lean core.** `pip install alicatlib` pulls in `anyio` and `anyserial` — and
  nothing else. Parquet, Postgres, and docs live behind extras.

## Install

```bash
pip install alicatlib
# optional sinks
pip install 'alicatlib[parquet]'
pip install 'alicatlib[postgres]'
```

Requires Python 3.13+.

## Quickstart (sync)

```python
from alicatlib.sync import Alicat

with Alicat.open("/dev/ttyUSB0") as dev:
    frame = dev.poll()
    print(frame.get_float("Mass_Flow"))
    dev.setpoint(50.0, "SCCM")
```

## Quickstart (async)

```python
import anyio
from alicatlib import Gas, open_device

async def main():
    async with await open_device("/dev/ttyUSB0") as dev:
        frame = await dev.poll()
        print(frame.get_float("Mass_Flow"))
        await dev.gas(Gas.N2, save=True)

anyio.run(main)
```

## Development

Uses [`uv`](https://docs.astral.sh/uv/) for env and lock management, `hatchling`
for the build backend, `ruff` for format and lint, and `mypy --strict` for
types.

```bash
uv sync --all-extras --dev
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow, and
[docs/design.md](docs/design.md) for the architectural design.

## License

MIT. See [LICENSE](LICENSE).
