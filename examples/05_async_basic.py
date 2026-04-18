#!/usr/bin/env python3
"""Async mirror of ``01_read_once_sync.py``.

The async core is canonical (``alicatlib.sync`` wraps it through a
portal). Most production callers should prefer this entry point.

    PORT=/dev/ttyUSB0 uv run python examples/05_async_basic.py
"""

from __future__ import annotations

import os

import anyio

from alicatlib import Statistic, open_device
from alicatlib.transport import SerialSettings


async def main() -> None:
    port = os.environ.get("PORT", "/dev/ttyUSB0")
    baud = int(os.environ.get("BAUD", "19200"))
    async with open_device(port, serial=SerialSettings(port=port, baudrate=baud)) as dev:
        frame = await dev.poll()

        print(f"device:      {dev.info.model} (firmware {dev.info.firmware})")
        print(f"unit_id:     {dev.unit_id}")
        print(f"received_at: {frame.received_at.isoformat()}")
        print()

        for label, stat in (
            ("pressure", Statistic.ABS_PRESS),
            ("temperature", Statistic.TEMP_STREAM),
            ("vol_flow", Statistic.VOL_FLOW),
            ("mass_flow", Statistic.MASS_FLOW),
            ("setpoint", Statistic.MASS_FLOW_SETPT),
            ("gas", Statistic.FLUID_NAME),
        ):
            value = frame.get_statistic(stat)
            if value is None:
                continue
            print(f"  {label:<12} {value}")


if __name__ == "__main__":
    anyio.run(main)
