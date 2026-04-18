#!/usr/bin/env python3
"""Run the full stack against a scripted ``FakeTransport`` — no hardware.

Builds an in-memory script of ``(send_bytes → reply_bytes)`` for the
identify handshake plus a poll, passes the transport to ``Alicat.open``,
and exercises the library end-to-end. Runnable in CI and on any
machine; useful for trying the API shape without a device plugged in.

For a file-based variant, see :func:`alicatlib.testing.FakeTransportFromFixture`.

    uv run python examples/09_offline_with_fake_transport.py
"""

from __future__ import annotations

from alicatlib import Statistic
from alicatlib.sync import Alicat
from alicatlib.testing import FakeTransport


def _build_script() -> dict[bytes, bytes]:
    """Scripted replies for the identify + poll path on an MC-500SCCM-D."""
    mfg = b"".join(
        [
            b"A M00 Alicat Scientific\r",
            b"A M01 www.example.com\r",
            b"A M02 Ph   555-000-0000\r",
            b"A M03 info@example.com\r",
            b"A M04 Model Number MC-500SCCM-D\r",
            b"A M05 Serial Number 521641\r",
            b"A M06 Date Manufactured 03/02/2025\r",
            b"A M07 Date Calibrated   03/02/2025\r",
            b"A M08 Calibrated By     BL\r",
            b"A M09 Software Revision 10v20.0-R24\r",
        ],
    )
    df = b"".join(
        [
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________\r",
            b"A D01 700 Unit ID                    string          1\r",
            b"A D02 002 Abs Press                  s decimal     7/2 010 02 PSIA\r",
            b"A D03 003 Flow Temp                  s decimal     7/2 002 02 `C\r",
            b"A D04 004 Volu Flow                  s decimal     7/2 012 02 CCM\r",
            b"A D05 005 Mass Flow                  s decimal     7/2 012 02 SCCM\r",
            b"A D06 037 Mass Flow Setpt            s decimal     7/2 012 02 SCCM\r",
            b"A D07 703 Gas                        string          6\r",
        ],
    )
    return {
        b"AVE\r": b"A   10v20.0-R24 Jan  9 2025,15:04:07\r",
        b"A??M*\r": mfg,
        b"A??D*\r": df,
        b"A\r": b"A +014.62 +021.89 +000.00 +000.00 +050.00     N2\r",
    }


def main() -> None:
    transport = FakeTransport(_build_script(), label="fake://mc-500sccm-d")

    with Alicat.open(transport) as dev:
        print(f"identified: {dev.info.model} (firmware {dev.info.firmware})")
        print()

        frame = dev.poll()
        for label, stat in (
            ("pressure", Statistic.ABS_PRESS),
            ("temperature", Statistic.TEMP_STREAM),
            ("mass_flow", Statistic.MASS_FLOW),
            ("setpoint", Statistic.MASS_FLOW_SETPT),
            ("gas", Statistic.FLUID_NAME),
        ):
            print(f"  {label:<12} {frame.get_statistic(stat)}")

    print()
    print("all of the above ran without a serial port.")


if __name__ == "__main__":
    main()
