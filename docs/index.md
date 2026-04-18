# alicatlib

Python library for the full Alicat instrument matrix — flow meters, flow
controllers, pressure meters, pressure controllers, across gas and liquid
mediums, plus the CODA Coriolis line.

This site is the reference for the **v1** design. The authoritative
architectural document lives at [Design](design.md); every design decision in
the library should be traceable to a section there.

## Where to start

- [Installation](installation.md)
- [Sync quickstart](quickstart-sync.md)
- [Async quickstart](quickstart-async.md)
- [Logging and acquisition](logging.md) — recorder, sinks, backpressure, structured log events
- [Safety](safety.md) — destructive operations and setpoint validation rules
- [Testing](testing.md) — FakeTransport, fixtures, hardware tiers

## Status

Beta. The library ships the full transport, protocol, registry, device
identification, Tier-1 and Tier-2 command surfaces, multi-device manager
and recorder, all first-party sinks (CSV, JSONL, InMemory, SQLite,
Parquet, Postgres), the sync facade, and streaming mode. Documentation
completion and release prep are in progress. The liquid / fluid surface
and Tier-3 commands are tracked as future work — see [Design](design.md).
