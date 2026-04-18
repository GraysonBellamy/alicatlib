"""Sample sinks — stdlib-backed (core) plus optional Parquet & Postgres.

Public surface:

- :class:`SampleSink` — the Protocol every sink satisfies.
- :func:`pipe` — drains a recorder stream into a sink with buffered flushes.
- :class:`InMemorySink` — test-only; collects samples in a list.
- :class:`CsvSink` — stdlib-backed CSV; schema locked at first batch.
- :class:`JsonlSink` — stdlib-backed JSONL; one object per line.
- :class:`SqliteSink` — stdlib-backed SQLite (WAL, parameterised inserts).
- :class:`ParquetSink` — pyarrow-backed; requires ``alicatlib[parquet]``.
- :class:`PostgresSink` + :class:`PostgresConfig` — asyncpg-backed; requires
  ``alicatlib[postgres]``.

The optional sinks (:class:`ParquetSink`, :class:`PostgresSink`) import
their backing drivers lazily inside :meth:`open`. That means
instantiation succeeds without the extra installed — calling
:meth:`open` on an un-provisioned install raises
:class:`~alicatlib.errors.AlicatSinkDependencyError` with a copy-paste
install hint.

See ``docs/design.md`` §5.15.
"""

from __future__ import annotations

from alicatlib.sinks.base import SampleSink, pipe, sample_to_row
from alicatlib.sinks.csv import CsvSink
from alicatlib.sinks.jsonl import JsonlSink
from alicatlib.sinks.memory import InMemorySink
from alicatlib.sinks.parquet import ParquetSink
from alicatlib.sinks.postgres import PostgresConfig, PostgresSink
from alicatlib.sinks.sqlite import SqliteSink

__all__ = [
    "CsvSink",
    "InMemorySink",
    "JsonlSink",
    "ParquetSink",
    "PostgresConfig",
    "PostgresSink",
    "SampleSink",
    "SqliteSink",
    "pipe",
    "sample_to_row",
]
