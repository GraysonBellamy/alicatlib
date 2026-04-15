# Installation

```bash
pip install alicatlib
```

Requires **Python 3.12 or newer**.

## Optional extras

```bash
pip install 'alicatlib[parquet]'     # ParquetSink (pyarrow)
pip install 'alicatlib[postgres]'    # PostgresSink (asyncpg)
pip install 'alicatlib[docs]'        # build the docs locally
pip install 'alicatlib[dev]'         # full dev toolchain
```

CSV and JSONL sinks need no extras — they use only the standard library.

## Platform support

Linux, macOS, and Windows are tested in CI. Serial-port enumeration uses
`pyserial.tools.list_ports` on all three platforms.
