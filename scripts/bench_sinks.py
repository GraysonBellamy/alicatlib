"""Synthetic sink-throughput benchmark: CSV / JSONL / SQLite / Parquet / Postgres.

Generates N synthetic :class:`Sample` objects and drives each available
sink through :func:`alicatlib.sinks.pipe` at a fixed batch size, then
reports throughput (samples/sec) and the final on-disk footprint.
There's no hardware dependency — all samples are fabricated — so this
runs anywhere. The Parquet and Postgres rows are skipped cleanly if
the respective extras / servers aren't available.

Feed the output into ``docs/benchmarks.md``.

Usage::

    uv run python scripts/bench_sinks.py                 # 100k synthetic samples
    uv run python scripts/bench_sinks.py --n 500000
    uv run python scripts/bench_sinks.py --batch-size 256
    # Postgres target (optional):
    uv run python scripts/bench_sinks.py \\
        --postgres-dsn postgres://u:p@localhost/bench \\
        --postgres-table samples

Ordering note: file sinks write to a fresh tmpdir per run; the Postgres
sink truncates (via ``create_table=True`` against a fresh table name
that includes a timestamp) to keep runs idempotent without clobbering
existing state.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

from alicatlib.devices.data_frame import (
    DataFrame,
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
    ParsedFrame,
)
from alicatlib.devices.models import StatusCode
from alicatlib.registry import Statistic
from alicatlib.sinks import (
    CsvSink,
    JsonlSink,
    SampleSink,
    SqliteSink,
    pipe,
)
from alicatlib.streaming.sample import Sample


def _decimal(v: str) -> float:
    return float(v)


_FORMAT: Final = DataFrameFormat(
    fields=(
        DataFrameField(
            name="Mass_Flow",
            raw_name="Mass_Flow",
            type_name="decimal",
            statistic=Statistic.MASS_FLOW,
            unit=None,
            conditional=False,
            parser=_decimal,
        ),
    ),
    flavor=DataFrameFormatFlavor.DEFAULT,
)


def _fabricate_sample(i: int, base: datetime) -> Sample:
    """Deterministic-ish synthetic sample. Value modulates so compression has something to do."""
    when = base + timedelta(microseconds=i * 100)
    value = 10.0 + (i % 1000) * 0.01
    parsed = ParsedFrame(
        unit_id="A",
        values={"Mass_Flow": value},
        values_by_statistic={Statistic.MASS_FLOW: value},
        status=frozenset[StatusCode](),
    )
    frame = DataFrame.from_parsed(
        parsed,
        format=_FORMAT,
        received_at=when,
        monotonic_ns=i,
    )
    return Sample(
        device="fuel",
        unit_id="A",
        monotonic_ns=i,
        requested_at=when,
        received_at=when + timedelta(milliseconds=5),
        midpoint_at=when + timedelta(milliseconds=2),
        latency_s=0.005,
        frame=frame,
    )


async def _stream(
    n: int,
    base: datetime,
    yield_batch: int,
) -> AsyncIterator[Mapping[str, Sample]]:
    """Yield ``n`` fabricated samples in per-tick batches of ``yield_batch``."""
    batch: dict[str, Sample] = {}
    for i in range(n):
        batch["fuel"] = _fabricate_sample(i, base)
        if (i + 1) % yield_batch == 0:
            yield batch
            batch = {}
    if batch:
        yield batch


async def _bench_sink(
    label: str,
    sink: SampleSink,
    *,
    n: int,
    batch_size: int,
    flush_interval: float,
    report_file: Path | None,
) -> tuple[str, float, float, int | None]:
    """Pipe ``n`` samples into ``sink``; return (label, duration_s, sps, bytes_or_none)."""
    base = datetime.now(UTC)
    t0 = time.perf_counter()
    async with sink:
        await pipe(
            _stream(n, base, yield_batch=1),
            sink,
            batch_size=batch_size,
            flush_interval=flush_interval,
        )
    elapsed = time.perf_counter() - t0
    sps = n / elapsed if elapsed > 0 else float("inf")
    size = report_file.stat().st_size if report_file and report_file.exists() else None
    return label, elapsed, sps, size


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=100_000, help="Sample count (default: 100_000).")
    p.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="pipe() batch threshold (default: 64).",
    )
    p.add_argument(
        "--flush-interval",
        type=float,
        default=1.0,
        help="pipe() flush interval seconds (default: 1.0).",
    )
    p.add_argument(
        "--skip-parquet",
        action="store_true",
        help="Skip ParquetSink even if pyarrow is installed.",
    )
    p.add_argument(
        "--parquet-compression",
        default="zstd",
        help=(
            "Comma-separated Parquet codecs to A/B, e.g. 'zstd,snappy,none'. "
            "Each runs its own row in the report. Default: 'zstd'."
        ),
    )
    p.add_argument(
        "--postgres-dsn",
        default=os.environ.get("ALICATLIB_BENCH_POSTGRES_DSN"),
        help="Run PostgresSink against this DSN. Skipped if unset.",
    )
    p.add_argument(
        "--postgres-table",
        default="alicat_bench_samples",
        help="Target table (will be CREATE TABLE IF NOT EXISTS'd).",
    )
    p.add_argument(
        "--postgres-no-copy",
        action="store_true",
        help="Force executemany fallback instead of COPY.",
    )
    return p.parse_args()


def _parquet_codecs(args: argparse.Namespace) -> list[str]:
    """Parse the ``--parquet-compression`` list; empty when parquet is off."""
    if args.skip_parquet:
        return []
    try:
        import pyarrow  # noqa: F401, PLC0415
    except Exception:
        return []
    codecs = [c.strip() for c in args.parquet_compression.split(",") if c.strip()]
    return codecs


def _make_parquet(tmpdir: Path, codec: str) -> SampleSink:
    """Build a :class:`ParquetSink` for ``codec`` at ``tmpdir/bench.<codec>.parquet``."""
    # PLC0415: intentional deferred import — parquet extra is optional.
    from alicatlib.sinks import ParquetSink  # noqa: PLC0415

    return ParquetSink(
        tmpdir / f"bench.{codec}.parquet",
        compression=codec,  # type: ignore[arg-type]
    )


async def _maybe_postgres(args: argparse.Namespace) -> SampleSink | None:
    if not args.postgres_dsn:
        return None
    try:
        import asyncpg  # noqa: F401, PLC0415
    except Exception:
        print("[skip] asyncpg not installed; install alicatlib[postgres]")
        return None
    # PLC0415: intentional deferred import — postgres extra is optional.
    from alicatlib.sinks import PostgresConfig, PostgresSink  # noqa: PLC0415

    table = f"{args.postgres_table}_{int(time.time())}"
    cfg = PostgresConfig(
        dsn=args.postgres_dsn,
        table=table,
        create_table=True,
        use_copy=not args.postgres_no_copy,
    )
    return PostgresSink(cfg)


async def _bench_all(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="alicat-bench-") as raw_tmp:
        tmpdir = Path(raw_tmp)
        results: list[tuple[str, float, float, int | None]] = []

        csv_path = tmpdir / "bench.csv"
        results.append(
            await _bench_sink(
                "csv",
                CsvSink(csv_path),
                n=args.n,
                batch_size=args.batch_size,
                flush_interval=args.flush_interval,
                report_file=csv_path,
            ),
        )

        jsonl_path = tmpdir / "bench.jsonl"
        results.append(
            await _bench_sink(
                "jsonl",
                JsonlSink(jsonl_path),
                n=args.n,
                batch_size=args.batch_size,
                flush_interval=args.flush_interval,
                report_file=jsonl_path,
            ),
        )

        sqlite_path = tmpdir / "bench.sqlite"
        results.append(
            await _bench_sink(
                "sqlite",
                SqliteSink(sqlite_path),
                n=args.n,
                batch_size=args.batch_size,
                flush_interval=args.flush_interval,
                report_file=sqlite_path,
            ),
        )

        for codec in _parquet_codecs(args):
            parquet_path = tmpdir / f"bench.{codec}.parquet"
            results.append(
                await _bench_sink(
                    f"parquet({codec})",
                    _make_parquet(tmpdir, codec),
                    n=args.n,
                    batch_size=args.batch_size,
                    flush_interval=args.flush_interval,
                    report_file=parquet_path,
                ),
            )

        postgres_sink = await _maybe_postgres(args)
        if postgres_sink is not None:
            label = "postgres(copy)" if not args.postgres_no_copy else "postgres(exec)"
            results.append(
                await _bench_sink(
                    label,
                    postgres_sink,
                    n=args.n,
                    batch_size=args.batch_size,
                    flush_interval=args.flush_interval,
                    report_file=None,
                ),
            )

    _print_report(results, args)


def _print_report(
    results: list[tuple[str, float, float, int | None]],
    args: argparse.Namespace,
) -> None:
    print()
    print(f"samples:     {args.n}")
    print(f"batch_size:  {args.batch_size}")
    print(f"flush_intv:  {args.flush_interval}")
    print()
    print(f"{'sink':<16} {'duration_s':>12} {'samples/sec':>14} {'bytes/sample':>14}")
    print(f"{'-' * 16:<16} {'-' * 12:>12} {'-' * 14:>14} {'-' * 14:>14}")
    for label, duration, sps, size in results:
        bps = "n/a" if size is None else f"{size / args.n:.1f}"
        print(f"{label:<16} {duration:>12.3f} {sps:>14,.0f} {bps:>14}")


def main() -> int:
    args = _parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_bench_all(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
