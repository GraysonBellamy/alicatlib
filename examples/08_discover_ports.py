#!/usr/bin/env python3
"""Enumerate serial ports and probe each for an Alicat device.

Prints the OS-visible serial ports, then runs the identification
pipeline across ``ports × unit_ids × baudrates`` and tabulates each
attempt. ``find_devices`` never raises — every combination ends up in
the result tuple, either ok (with ``info``) or failed (with ``error``).

Set ``FAST=1`` to enable ``stop_on_first_hit`` — once a port responds at
one baud, other bauds for that port are skipped. Cuts sweep time roughly
in half on devices at the default 19200 baud, but the result tuple no
longer includes one row per combination.

    uv run python examples/08_discover_ports.py
    uv run python examples/08_discover_ports.py A B       # extra unit ids
    FAST=1 uv run python examples/08_discover_ports.py    # stop on first hit
"""

from __future__ import annotations

import os
import sys

from alicatlib.sync import find_devices, list_serial_ports


def main() -> None:
    unit_ids = tuple(sys.argv[1:]) or ("A",)
    stop_on_first_hit = bool(os.environ.get("FAST"))

    ports = list_serial_ports()
    print(f"serial ports ({len(ports)}):")
    for port in ports:
        print(f"  {port}")
    print()

    if not ports:
        print("nothing to probe")
        return

    results = find_devices(
        unit_ids=unit_ids,
        timeout=0.3,
        stop_on_first_hit=stop_on_first_hit,
    )

    header = f"{'port':<20} {'uid':<4} {'baud':>6}  status"
    print(header)
    print("-" * len(header))
    for r in results:
        if r.ok:
            assert r.info is not None
            status = f"ok  {r.info.model} ({r.info.firmware})"
        else:
            assert r.error is not None
            status = f"err {type(r.error).__name__}"
        print(f"{r.port:<20} {r.unit_id:<4} {r.baudrate:>6}  {status}")


if __name__ == "__main__":
    main()
