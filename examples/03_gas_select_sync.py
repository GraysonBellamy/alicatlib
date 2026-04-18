#!/usr/bin/env python3
"""Read the active gas, change it, read it back.

Uses ``save=False`` — the device switches gas in volatile memory only,
which is the right default for scripts/demos that flip gas frequently
(writing to EEPROM repeatedly wears it out; use ``save=True`` only for
configuration changes intended to survive power cycles).

Gas readback is done via :meth:`SyncDevice.poll` (the data frame's
``Gas`` column) rather than :meth:`SyncDevice.gas`'s query form, because
the bare-query form of ``dev.gas()`` needs the ``GS`` command, which
requires V10 firmware ≥ 10v05. ``poll`` works on every firmware.

    PORT=/dev/ttyUSB0 GAS=N2 uv run python examples/03_gas_select_sync.py
"""

from __future__ import annotations

import os

from alicatlib import Gas, Statistic
from alicatlib.sync import Alicat
from alicatlib.transport import SerialSettings


def main() -> None:
    port = os.environ.get("PORT", "/dev/ttyUSB0")
    baud = int(os.environ.get("BAUD", "19200"))
    target = Gas[os.environ.get("GAS", "N2")]

    with Alicat.open(port, serial=SerialSettings(port=port, baudrate=baud)) as dev:
        before = dev.poll().get_statistic(Statistic.FLUID_NAME)
        print(f"before: {before}")

        dev.gas(target, save=False)

        after = dev.poll().get_statistic(Statistic.FLUID_NAME)
        print(f"after:  {after}")


if __name__ == "__main__":
    main()
