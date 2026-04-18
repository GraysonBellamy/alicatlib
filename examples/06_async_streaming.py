#!/usr/bin/env python3
"""Streaming into memory, with an overflow-policy contrast.

Opens a :class:`StreamingSession`, collects 100 frames, then rewires the
same bus with a tiny buffer and an artificially slow consumer so
``dropped_frames`` accrues — the point of the contrast.

Setting the stream rate uses the ``NCS`` command, which requires V10
firmware 10v05+. This example defaults to *not* setting a rate — the
device streams at whatever it's already configured for, which is the
portable choice. Pass ``RATE_MS=50`` to exercise the configurable path
on a V10 device.

    PORT=/dev/ttyUSB0 uv run python examples/06_async_streaming.py
    PORT=/dev/ttyUSB0 RATE_MS=50 uv run python examples/06_async_streaming.py
"""

from __future__ import annotations

import os

import anyio

from alicatlib import Statistic, open_device
from alicatlib.streaming import OverflowPolicy
from alicatlib.transport import SerialSettings


async def main() -> None:
    port = os.environ.get("PORT", "/dev/ttyUSB0")
    baud = int(os.environ.get("BAUD", "19200"))
    rate_ms_env = os.environ.get("RATE_MS")
    rate_ms = int(rate_ms_env) if rate_ms_env else None

    async with open_device(port, serial=SerialSettings(port=port, baudrate=baud)) as dev:
        print("run 1: full-speed consumer, default overflow (DROP_OLDEST)")
        frames: list[float] = []
        async with dev.stream(rate_ms=rate_ms) as stream:
            async for frame in stream:
                mass_flow = frame.get_statistic(Statistic.MASS_FLOW)
                if isinstance(mass_flow, float):
                    frames.append(mass_flow)
                if len(frames) >= 100:
                    break
        print(f"  collected={len(frames)}  dropped={stream.dropped_frames}")
        if frames:
            print(f"  first={frames[0]:.3f}  last={frames[-1]:.3f}")

        print()
        print("run 2: deliberately slow consumer, tiny buffer")
        slow_count = 0
        async with dev.stream(
            rate_ms=rate_ms,
            overflow=OverflowPolicy.DROP_OLDEST,
            buffer_size=4,
        ) as stream:
            async for _ in stream:
                slow_count += 1
                await anyio.sleep(0.2)  # slower than any reasonable producer rate
                if slow_count >= 20:
                    break
        print(f"  collected={slow_count}  dropped={stream.dropped_frames}")


if __name__ == "__main__":
    anyio.run(main)
