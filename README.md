# alicatlib

Python library for [Alicat Scientific](https://www.alicat.com/) mass flow
meters (`M-`, `MS-`, `MQ-`, `MW-`) and controllers (`MC-`, `MCS-`, `MCQ-`,
`MCW-`) over serial.

`alicatlib` is the successor to `pyAlicat`. It is a clean-break rewrite focused
on correctness, typed APIs, and reliable multi-device acquisition.

> Status: pre-release scaffolding. See [docs/design.md](docs/design.md) for the
> full v1 design and milestone plan.

## Highlights

- **Typed end to end.** `Gas.N2`, `Unit.SCCM`, `Statistic.MASS_FLOW`, frozen
  dataclass responses. `py.typed` shipped. `mypy --strict` passes.
- **Declarative commands.** One `Command` object per Alicat command with
  encoding, decoding, firmware gating, and device-kind gating as metadata —
  adding a new command is ~50 lines.
- **Typed errors.** Distinct exception types for timeout, malformed response,
  unsupported firmware, and device error markers — all carrying structured
  `ErrorContext` for debuggability.
- **Safety.** Destructive operations (factory reset, baud change, exhaust)
  require `confirm=True`. Setpoints are range-checked before any I/O.
- **Multi-device, correctly.** `AlicatManager` runs many devices concurrently;
  same-port requests serialize via a shared lock, different ports run in
  parallel, resources unwind cleanly on partial-open failures.
- **Swappable transports.** `SerialTransport` for hardware, `FakeTransport` for
  tests; TCP / Modbus can land behind the same interface later.
- **Sync or async.** The core is `async` (built on `anyio`), and a sync facade
  (`alicatlib.sync.Alicat`) wraps it for scripts, notebooks, and REPLs.
- **Lean core.** `pip install alicatlib` pulls in `anyio` and `pyserial` — and
  nothing else. Parquet, Postgres, and docs live behind extras.

## Install

```bash
pip install alicatlib
# optional sinks
pip install 'alicatlib[parquet]'
pip install 'alicatlib[postgres]'
```

Requires Python 3.12+.

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
