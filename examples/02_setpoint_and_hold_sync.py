#!/usr/bin/env python3
"""Ramp a flow controller to a setpoint, hold briefly, return to zero.

Demonstrates the safe idiom for controller interaction: the ``finally``
block always drops the setpoint back to zero so an abort (Ctrl-C,
exception) can't leave flow running. Exits early if the port opens to a
meter rather than a controller.

    PORT=/dev/ttyUSB0 SETPOINT=50 uv run python examples/02_setpoint_and_hold_sync.py
"""

from __future__ import annotations

import os
import sys
import time

from alicatlib import Statistic, Unit
from alicatlib.sync import Alicat, SyncFlowController
from alicatlib.transport import SerialSettings


def main() -> int:
    port = os.environ.get("PORT", "/dev/ttyUSB0")
    baud = int(os.environ.get("BAUD", "19200"))
    target = float(os.environ.get("SETPOINT", "50.0"))

    with Alicat.open(port, serial=SerialSettings(port=port, baudrate=baud)) as dev:
        if not isinstance(dev, SyncFlowController):
            print(
                f"error: {port} opened as {type(dev).__name__}; "
                "this example requires a flow controller",
                file=sys.stderr,
            )
            return 1

        try:
            dev.setpoint(target, Unit.SCCM)
            print(f"setpoint -> {target} SCCM; polling...")
            for tick in range(5):
                time.sleep(0.2)
                frame = dev.poll()
                mass_flow = frame.get_statistic(Statistic.MASS_FLOW)
                print(f"  t={tick * 0.2:>3.1f}s  mass_flow={mass_flow}")
        finally:
            dev.setpoint(0.0, Unit.SCCM)
            print("setpoint -> 0 SCCM")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
