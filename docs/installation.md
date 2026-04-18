# Installation

```bash
pip install alicatlib
```

Requires **Python 3.13 or newer** (inherited from [`anyserial`](https://pypi.org/project/anyserial/)).

## Optional extras

```bash
pip install 'alicatlib[parquet]'     # ParquetSink (pyarrow)
pip install 'alicatlib[postgres]'    # PostgresSink (asyncpg)
pip install 'alicatlib[docs]'        # build the docs locally
pip install 'alicatlib[dev]'         # full dev toolchain
```

CSV and JSONL sinks need no extras — they use only the standard library.

## Platform support

Linux, macOS, BSD, and Windows are supported via
[`anyserial`](https://pypi.org/project/anyserial/) (readiness-driven I/O on
POSIX, IOCP on Windows). Serial-port enumeration uses
`anyserial.list_serial_ports()` natively; a `pyserial`-backed fallback is
available under the `anyserial[discovery-pyserial]` extra for platforms where
native enumeration misses devices.
