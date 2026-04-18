"""Measure single-line query round-trip latency on a real Alicat device.

Hot-loop baseline (design §5.2, §5.19) tracked against future ``anyserial``
releases and the eager-task-factory A/B (design §5.2).

Defaults to ``VE`` (firmware version) because it works on every device and
firmware family — including GP and pre-10v05 V8/V9 devices that don't
support ``GS``. Use ``--cmd gs`` to bench ``GAS_SELECT`` on a 10v05+ device.

Usage:
    ALICATLIB_TEST_PORT=/dev/ttyUSB0 \\
    uv run python scripts/bench_query.py              # default 200 iterations of VE
    uv run python scripts/bench_query.py --n 1000     # more samples
    uv run python scripts/bench_query.py --eager      # A/B the eager factory
    uv run python scripts/bench_query.py --cmd gs     # 10v05+ devices only

The script exercises the SerialTransport, AlicatProtocolClient, and
command encode/decode layers only; Session / facade round-trips have
their own benchmarks.

Results feed ``docs/benchmarks.md``. Paste the summary block into the
appropriate table row.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from typing import TYPE_CHECKING

from alicatlib._runtime import install_eager_task_factory
from alicatlib.commands import (
    GAS_SELECT,
    VE_QUERY,
    DecodeContext,
    GasSelectRequest,
    VeRequest,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.transport import SerialSettings, SerialTransport

if TYPE_CHECKING:
    from alicatlib.commands.base import Command

PORT_ENV = "ALICATLIB_TEST_PORT"
UNIT_ID_ENV = "ALICATLIB_TEST_UNIT_ID"
FIRMWARE_ENV = "ALICATLIB_TEST_FIRMWARE"
BAUD_ENV = "ALICATLIB_TEST_BAUD"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=200, help="Number of queries (default: 200)")
    p.add_argument("--warmup", type=int, default=10, help="Warmup iterations (default: 10)")
    p.add_argument(
        "--cmd",
        choices=("ve", "gs"),
        default="ve",
        help="Which command to bench: 've' (works on every firmware; default) or 'gs' (10v05+).",
    )
    p.add_argument(
        "--eager",
        action="store_true",
        help="Install asyncio.eager_task_factory before timing (design §5.2 A/B).",
    )
    p.add_argument(
        "--port",
        default=os.environ.get(PORT_ENV),
        help=f"Serial port path (default: ${PORT_ENV})",
    )
    p.add_argument(
        "--unit-id",
        default=os.environ.get(UNIT_ID_ENV, "A"),
        help=f"Unit id (default: ${UNIT_ID_ENV} or 'A')",
    )
    p.add_argument(
        "--baud",
        type=int,
        default=int(os.environ.get(BAUD_ENV, "19200")),
        help=f"Baud rate (default: ${BAUD_ENV} or 19200)",
    )
    p.add_argument(
        "--firmware",
        default=os.environ.get(FIRMWARE_ENV, "10v05"),
        help=f"Firmware version string (default: ${FIRMWARE_ENV} or '10v05')",
    )
    return p.parse_args()


def _select_command(name: str) -> tuple[Command[object, object], object]:
    """Return ``(command_spec, request)`` for the chosen --cmd."""
    if name == "ve":
        return VE_QUERY, VeRequest()  # type: ignore[return-value]
    if name == "gs":
        return GAS_SELECT, GasSelectRequest()  # type: ignore[return-value]
    raise ValueError(f"unknown --cmd {name!r}")


async def _bench(args: argparse.Namespace) -> None:
    if not args.port:
        raise SystemExit(f"Set --port or {PORT_ENV} to run the benchmark.")

    if args.eager:
        installed = install_eager_task_factory()
        print(f"eager_task_factory installed: {installed}")

    firmware = FirmwareVersion.parse(args.firmware)
    ctx = DecodeContext(unit_id=args.unit_id, firmware=firmware)
    cmd_spec, request = _select_command(args.cmd)

    transport = SerialTransport(SerialSettings(port=args.port, baudrate=args.baud))
    await transport.open()
    client = AlicatProtocolClient(transport)
    try:
        cmd = cmd_spec.encode(ctx, request)

        # Warmup — discard timings for the first few so the kernel driver /
        # USB-serial converter has settled before we measure.
        for _ in range(args.warmup):
            raw = await client.query_line(cmd)
            cmd_spec.decode(raw, ctx)

        samples_s: list[float] = []
        for _ in range(args.n):
            t0 = time.perf_counter()
            raw = await client.query_line(cmd)
            cmd_spec.decode(raw, ctx)
            samples_s.append(time.perf_counter() - t0)
    finally:
        await transport.close()

    samples_ms = [s * 1000.0 for s in samples_s]
    samples_ms.sort()

    def pct(q: float) -> float:
        return samples_ms[min(len(samples_ms) - 1, round(q * len(samples_ms)))]

    print()
    print(f"port:       {args.port}")
    print(f"baud:       {args.baud}")
    print(f"unit_id:    {args.unit_id}")
    print(f"firmware:   {args.firmware}")
    print(f"command:    {args.cmd.upper()}")
    print(f"eager:      {args.eager}")
    print(f"iterations: {args.n} (warmup {args.warmup} discarded)")
    print()
    print(f"min:   {min(samples_ms):8.3f} ms")
    print(f"p50:   {pct(0.50):8.3f} ms")
    print(f"p95:   {pct(0.95):8.3f} ms")
    print(f"p99:   {pct(0.99):8.3f} ms")
    print(f"max:   {max(samples_ms):8.3f} ms")
    print(f"mean:  {statistics.mean(samples_ms):8.3f} ms")
    print(f"stdev: {statistics.pstdev(samples_ms):8.3f} ms")


def main() -> int:
    args = _parse_args()
    asyncio.run(_bench(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
