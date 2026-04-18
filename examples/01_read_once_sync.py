#!/usr/bin/env python3
"""Open a serial port, poll once, print the frame.

The "does it work" smoke test — exercises port open, the context-manager
lifecycle, and typed ``DataFrame`` access. Works against any Alicat flow
or pressure device.

    PORT=/dev/ttyUSB0 uv run python examples/01_read_once_sync.py
"""

from __future__ import annotations

import os

from alicatlib import Statistic
from alicatlib.sync import Alicat
from alicatlib.transport import SerialSettings


def main() -> None:
    port = os.environ.get("PORT", "/dev/ttyUSB0")
    baud = int(os.environ.get("BAUD", "19200"))
    with Alicat.open(port, serial=SerialSettings(port=port, baudrate=baud)) as dev:
        frame = dev.poll()

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
    main()
