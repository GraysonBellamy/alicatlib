# Async quickstart

```python
import anyio
from alicatlib import Gas, Unit, open_device

async def main() -> None:
    async with await open_device("/dev/ttyUSB0") as dev:
        frame = await dev.poll()
        print(frame.get_float("Mass_Flow"))

        await dev.gas(Gas.N2, save=True)
        await dev.setpoint(50.0, Unit.SCCM)

anyio.run(main)
```

## Multi-device acquisition

```python
from alicatlib import AlicatManager
from alicatlib.streaming import record
from alicatlib.sinks.csv import CsvSink
from alicatlib.sinks.base import pipe

async def run() -> None:
    async with AlicatManager() as mgr:
        await mgr.add("fuel", "/dev/ttyUSB0")
        await mgr.add("air",  "/dev/ttyUSB1")
        async with CsvSink("run.csv") as sink:
            await pipe(record(mgr, rate_hz=10, duration=60), sink)
```

See the [Design doc](design.md) §5.14 for scheduling and backpressure details.
