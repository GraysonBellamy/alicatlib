#!/usr/bin/env python3
"""Two devices polled at 10 Hz for 10 s, piped into a CSV file.

The canonical "what's this library actually for" example. Exercises the
manager (shared-lock, parallel-port), the drift-free recorder, the CSV
sink, and the ``AcquisitionSummary`` that pops out when the CM exits.

    PORT1=/dev/ttyUSB0 PORT2=/dev/ttyUSB1 \\
        uv run python examples/04_multi_device_csv_sync.py
"""

from __future__ import annotations

import os

from alicatlib.sync import SyncAlicatManager, SyncCsvSink, pipe, record
from alicatlib.transport import SerialSettings


def main() -> None:
    port1 = os.environ.get("PORT1", "/dev/ttyUSB0")
    port2 = os.environ.get("PORT2", "/dev/ttyUSB1")
    baud = int(os.environ.get("BAUD", "19200"))
    output = os.environ.get("OUTPUT", "run.csv")

    with SyncAlicatManager() as mgr:
        mgr.add("fuel", port1, serial=SerialSettings(port=port1, baudrate=baud))
        mgr.add("air", port2, serial=SerialSettings(port=port2, baudrate=baud))

        with (
            record(mgr, rate_hz=10.0, duration=10.0) as stream,
            SyncCsvSink(output) as sink,
        ):
            summary = pipe(stream, sink)

    print(f"wrote {output}")
    print(f"  started_at:      {summary.started_at.isoformat()}")
    print(f"  finished_at:     {summary.finished_at.isoformat()}")
    print(f"  samples_emitted: {summary.samples_emitted}")
    print(f"  samples_late:    {summary.samples_late}")
    print(f"  max_drift_ms:    {summary.max_drift_ms:.2f}")


if __name__ == "__main__":
    main()
