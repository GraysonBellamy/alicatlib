# Migration from pyAlicat

`alicatlib` is a clean-break successor to `pyAlicat` 0.0.x. There is **no import
compatibility**; names, return types, and error types are all new.

The full before/after is in the [Design doc](design.md) §8. Quick highlights:

| pyAlicat 0.0.x                     | alicatlib                                          |
| ---------------------------------- | ------------------------------------------------- |
| `Device.new_device(port)`          | `await open_device(port)`                         |
| `dev.poll()` returns `dict`        | `dev.poll()` returns `DataFrame` (typed)          |
| `data["Mass_Flow"]` → `str\|float` | `frame.get_float("Mass_Flow")` → `float \| None`  |
| `dev.gas("N2", True)`              | `await dev.gas(Gas.N2, save=True)`                |
| silent fallbacks on timeout        | `AlicatTimeoutError` with `ErrorContext`          |
| hardcoded Postgres logging         | opt-in `PostgresSink` behind `alicatlib[postgres]` |
