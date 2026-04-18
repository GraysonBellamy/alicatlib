#!/usr/bin/env python3
"""Async multi-device recording into a SQLite WAL database.

Mirrors example 04 on the async core, with a SQLite sink instead of CSV.
After the acquisition CM exits, reopens the DB with the stdlib driver to
demonstrate the rows really persisted.

    PORT1=/dev/ttyUSB0 PORT2=/dev/ttyUSB1 \\
        uv run python examples/07_async_multi_device_sqlite.py
"""

from __future__ import annotations

import os
import sqlite3

import anyio

from alicatlib import AlicatManager
from alicatlib.sinks import SqliteSink, pipe
from alicatlib.streaming import record
from alicatlib.transport import SerialSettings


async def main() -> None:
    port1 = os.environ.get("PORT1", "/dev/ttyUSB0")
    port2 = os.environ.get("PORT2", "/dev/ttyUSB1")
    baud = int(os.environ.get("BAUD", "19200"))
    db_path = os.environ.get("OUTPUT", "run.db")

    async with AlicatManager() as mgr:
        await mgr.add("fuel", port1, serial=SerialSettings(port=port1, baudrate=baud))
        await mgr.add("air", port2, serial=SerialSettings(port=port2, baudrate=baud))

        async with (
            record(mgr, rate_hz=20.0, duration=30.0) as stream,
            SqliteSink(db_path) as sink,
        ):
            summary = await pipe(stream, sink)

    print(f"wrote {db_path}")
    print(f"  samples_emitted: {summary.samples_emitted}")
    print(f"  samples_late:    {summary.samples_late}")
    print(f"  max_drift_ms:    {summary.max_drift_ms:.2f}")

    with sqlite3.connect(db_path) as conn:
        (row_count,) = conn.execute("SELECT COUNT(*) FROM samples").fetchone()
        per_device = conn.execute(
            "SELECT device, COUNT(*) FROM samples GROUP BY device ORDER BY device",
        ).fetchall()
    print(f"  total rows:      {row_count}")
    for device, count in per_device:
        print(f"    {device:<8} {count}")


if __name__ == "__main__":
    anyio.run(main)
