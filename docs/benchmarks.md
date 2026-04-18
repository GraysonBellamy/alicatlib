# Benchmarks

Latency and throughput baselines for alicatlib. We track these against
hardware so regressions in `anyserial` or in the protocol client hot path
surface as a visible number, not a vibe.

## How to run

```sh
# Set the device under test (skip any option you don't need).
export ALICATLIB_TEST_PORT=/dev/ttyUSB0
export ALICATLIB_TEST_UNIT_ID=A
export ALICATLIB_TEST_FIRMWARE=10v05

uv run python scripts/bench_query.py --n 1000
uv run python scripts/bench_query.py --n 1000 --eager   # A/B the factory
```

The warmup iterations (10 by default) are discarded. Numbers are per-query
*round-trip* — encode → write → read_until → decode.

## What we measure

**Single-line query round-trip** — the minimal end-to-end loop.

- Encoder: pure function.
- Transport write: one `SerialPort.send` bounded by `fail_after(write_timeout)`.
- Transport read: loop reading chunks until the CR terminator, bounded by `fail_after(timeout)`.
- Decoder: small parse step (gas-table lookup for `GS`, regex scan for `VE`).

`bench_query.py` defaults to `VE` because it's the one query supported on
every firmware family (GP, V8/V9, V10 alike). Pass `--cmd gs` on a 10v05+
device to bench `GAS_SELECT` instead.

No concurrency; one command in flight. Everything above the `Transport` /
`AlicatProtocolClient` seams is exercised.

## Results

### Single-line query baseline

| date       | host / converter           | device + firmware              | baud   | cmd | eager | n    | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |
| ---------- | -------------------------- | ------------------------------ | ------ | --- | ----- | ---- | -------- | -------- | -------- | -------- |
| 2026-04-17 | Linux x86_64 / Prolific PL2303 | M-series 8v17.0-R23 (V8/V9)    | 115200 | VE  | off   | 1000 | 3.997    | 4.204    | 8.337    | 13.184   |
| 2026-04-17 | same as above              | same                           | 115200 | VE  | on    | 1000 | 3.999    | 4.344    | 8.770    | 13.327   |

Capture template: run `scripts/bench_query.py --n 1000` twice (with and
without `--eager`), paste the summary block, fill in the row. Note the
USB-serial converter — buffering latency differs by chip (FT232RL,
CP210x, CH340, PL2303 all have distinct behaviour).

## Ordering observations — eager-task factory A/B

Design §5.2 warns that `asyncio.eager_task_factory` changes scheduling:
tasks that complete before their first suspension point never hit the event
loop, which can reorder observers. Record any such behaviour here when
running the `--eager` benchmark, alongside the raw timings.

| observation                                                           | commit | notes |
| --------------------------------------------------------------------- | ------ | ----- |
| 1000-iteration `VE` round-trip on PL2303 / 8v17: no ordering changes observed | _hardware day_ | p50 unchanged within noise (3.997 → 3.999 ms); p95/p99 marginally worse (+0.14 / +0.43 ms). Below the §5.2 ≥ 20% bar. |

**Decision.** Keep `AlicatConfig.eager_tasks` opt-in. The PL2303-bound 8v17
baseline shows eager produces no measurable p50 improvement (Δ ≈ +0.05 %,
inside noise) and a slight p95/p99 regression. The decision threshold is
≥ 20 % p50 improvement; we aren't close. Re-evaluate when a faster USB
converter or a 10v05+ device is on the bench (the bottleneck may be the
Prolific PL2303 buffering latency, not the asyncio scheduler).

## Sink throughput — `scripts/bench_sinks.py`

Synthetic benchmark: fabricated samples fed through
[`pipe()`](../src/alicatlib/sinks/base.py) at `batch_size=64`,
`flush_interval=1.0` s. No serial or network I/O — pure sink overhead.

```sh
uv run python scripts/bench_sinks.py --n 100000
# A/B Parquet codecs in one run:
uv run python scripts/bench_sinks.py --n 100000 \
    --parquet-compression "zstd,snappy,gzip,none"
# Optional Postgres row (skipped unless DSN provided):
uv run python scripts/bench_sinks.py --n 100000 \
    --postgres-dsn postgres://user:pw@localhost/bench
```

### Default run

| date       | host         | sink            | n      | samples/sec | bytes/sample |
| ---------- | ------------ | --------------- | ------ | ----------- | ------------ |
| 2026-04-17 | Linux x86_64 | CSV             | 100000 | ~80,000     | 121.8        |
| 2026-04-17 | Linux x86_64 | JSONL           | 100000 | ~80,000     | 244.8        |
| 2026-04-17 | Linux x86_64 | SQLite (WAL)    | 100000 | ~77,000     | 135.1        |
| 2026-04-17 | Linux x86_64 | Parquet (zstd)  | 100000 | ~73,000     | 44.0         |

Numbers are single-run, rounded; two back-to-back runs varied by ~10%.

### Parquet codec A/B

Same dataset (100k samples), run with
`--parquet-compression "zstd,snappy,gzip,none"`:

| codec  | samples/sec | bytes/sample | vs. `none` (size) |
| ------ | ----------- | ------------ | ----------------- |
| zstd   | ~73,000     | 44.0         | 3.3× smaller       |
| snappy | ~81,000     | 52.7         | 2.75× smaller      |
| gzip   | ~66,000     | 47.0         | 3.1× smaller       |
| none   | ~81,000     | 145.1        | baseline          |

**Why zstd is the default codec for `ParquetSink`:**

- zstd files are ~16% smaller than snappy's (44.0 vs 52.7 bytes/sample).
  At acquisition durations of weeks / months the size gap dominates.
- snappy is ~10% faster on this tiny-row workload (81k vs 73k sps),
  but both codecs are ~800× above a typical Alicat acquisition rate
  (100 Hz aggregate across 10 devices). Throughput isn't the
  binding constraint.
- gzip is strictly worse than snappy on both axes here — no reason to
  pick it as a default.

Callers who *are* genuinely throughput-limited can pass
`compression="snappy"` to `ParquetSink` and trade 16% of the size
win for 10% more samples/sec.

### Interpretation

- **CSV** is the throughput ceiling for text sinks; **JSONL** pays a
  per-row JSON format tax but costs the same in samples/sec here
  because the bottleneck is the flattener, not the emitter.
- **SQLite (WAL + `synchronous=NORMAL` + one transaction per batch)**
  sits between JSONL and Parquet for throughput, stores structured
  data, and supports random-access queries. It's the recommended
  stdlib database choice.
- **Parquet (zstd)** trades a small throughput cost for a ~3.3×
  reduction in on-disk size — a good default for long-horizon
  acquisition.

The Postgres row is intentionally absent from this table — it requires
a running server, and the raw number depends on network latency and
server tuning more than on the sink itself. Capture Postgres numbers
in-environment when running acceptance tests against production
infrastructure.

## Maintenance notes

- Current transport timeouts are 500 ms single-line / 1 s multiline (see
  [AlicatConfig](../src/alicatlib/config.py)). If the p50 here ever creeps
  within 2× of the timeout, bump the default.
- Every `anyserial` minor version bump should be followed by a fresh run
  of this bench and a new row in the table above. The package is alpha
  (`0.1.x`) with an explicitly unstable API; we re-pin per minor and
  verify no regression shipped (design §11).
