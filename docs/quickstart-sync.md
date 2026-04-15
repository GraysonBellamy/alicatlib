# Sync quickstart

```python
from alicatlib.sync import Alicat

with Alicat.open("/dev/ttyUSB0") as dev:
    print(dev.poll())
    dev.setpoint(50.0, "SCCM")
```

The sync API wraps the async core through a per-context `BlockingPortal`; see
[Design doc](design.md) §5.16.
