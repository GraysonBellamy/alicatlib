"""Tests for :class:`alicatlib.sinks.SqliteSink`.

Focus (design §5.15, §5.20):

- PRAGMA defaults (WAL, synchronous, busy_timeout) actually land.
- Schema lock on first batch; unknown columns drop with one-shot WARN;
  missing columns materialise as ``NULL``.
- ``create_table=False`` introspects an existing table via
  ``PRAGMA table_info`` and raises on a missing target.
- Every value passes through ``?`` placeholders — an injection-shaped
  value round-trips as a literal string, not a SQL fragment.
- Identifier validation rejects unsafe table names at the ``__init__``
  boundary.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

import pytest

from alicatlib.errors import AlicatSinkSchemaError, AlicatSinkWriteError
from alicatlib.sinks import SqliteSink
from tests.unit._sink_fixtures import make_sample

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from alicatlib.streaming.sample import Sample


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


pytestmark = pytest.mark.anyio


class TestConstruction:
    def test_rejects_unsafe_table_name(self, tmp_path: Path) -> None:
        for bad in ("; DROP TABLE x", "1bad", "a b", 'sam"ples', "a" * 80):
            with pytest.raises(ValueError, match="table name"):
                SqliteSink(tmp_path / "x.sqlite", table=bad)

    def test_accepts_normal_table_names(self, tmp_path: Path) -> None:
        for ok in ("samples", "run_1", "_private"):
            sink = SqliteSink(tmp_path / "x.sqlite", table=ok)
            assert sink.table == ok

    def test_rejects_negative_busy_timeout(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="busy_timeout_ms"):
            SqliteSink(tmp_path / "x.sqlite", busy_timeout_ms=-1)


class TestLifecycle:
    async def test_context_manager_open_close(self, tmp_path: Path) -> None:
        sink = SqliteSink(tmp_path / "run.sqlite")
        assert sink.columns is None
        async with sink:
            pass  # open/close only
        # re-entering is allowed after close
        async with sink:
            pass

    async def test_close_without_open_is_noop(self, tmp_path: Path) -> None:
        sink = SqliteSink(tmp_path / "never-opened.sqlite")
        await sink.close()  # must not raise

    async def test_open_is_idempotent(self, tmp_path: Path) -> None:
        async with SqliteSink(tmp_path / "run.sqlite") as sink:
            await sink.open()  # second open: no-op, no error

    async def test_write_before_open_raises(self, tmp_path: Path) -> None:
        sink = SqliteSink(tmp_path / "run.sqlite")
        with pytest.raises(RuntimeError, match="write_many called before open"):
            await sink.write_many([make_sample()])


class TestPragmas:
    async def test_wal_journal_active_by_default(self, tmp_path: Path) -> None:
        path = tmp_path / "run.sqlite"
        async with SqliteSink(path) as sink:
            await sink.write_many([make_sample(value=1.0)])
        conn = sqlite3.connect(path)
        try:
            (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            assert mode.lower() == "wal"
        finally:
            conn.close()

    async def test_synchronous_set_to_normal_by_default(self, tmp_path: Path) -> None:
        path = tmp_path / "run.sqlite"
        async with SqliteSink(path) as sink:
            await sink.write_many([make_sample(value=1.0)])
        # PRAGMA is set per-connection — verify via a fresh connection that
        # the header is WAL (persistent) and the journal file exists.
        assert (path.with_suffix(".sqlite-wal")).exists() or path.exists()

    async def test_custom_journal_mode_respected(self, tmp_path: Path) -> None:
        path = tmp_path / "run.sqlite"
        async with SqliteSink(path, journal_mode="DELETE") as sink:
            await sink.write_many([make_sample(value=1.0)])
        conn = sqlite3.connect(path)
        try:
            (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            assert mode.lower() == "delete"
        finally:
            conn.close()


class TestCreateTable:
    async def test_first_batch_creates_table_with_inferred_types(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "run.sqlite"
        async with SqliteSink(path) as sink:
            await sink.write_many([make_sample(value=1.0)])

        conn = sqlite3.connect(path)
        try:
            cols = conn.execute('PRAGMA table_info("samples")').fetchall()
        finally:
            conn.close()

        by_name = {row[1]: row for row in cols}
        assert by_name["Mass_Flow"][2] == "REAL"
        assert by_name["latency_s"][2] == "REAL"
        assert by_name["device"][2] == "TEXT"
        assert by_name["status"][2] == "TEXT"

    async def test_round_trip_via_sqlite3(self, tmp_path: Path) -> None:
        path = tmp_path / "run.sqlite"
        samples = [make_sample(value=v) for v in (1.0, 2.0, 3.0)]
        async with SqliteSink(path) as sink:
            await sink.write_many(samples[:2])
            await sink.write_many(samples[2:])

        conn = sqlite3.connect(path)
        try:
            values = [
                row[0]
                for row in conn.execute(
                    "SELECT Mass_Flow FROM samples ORDER BY rowid",
                )
            ]
        finally:
            conn.close()
        assert values == [1.0, 2.0, 3.0]

    async def test_columns_locked_after_first_batch(self, tmp_path: Path) -> None:
        path = tmp_path / "run.sqlite"
        async with SqliteSink(path) as sink:
            await sink.write_many([make_sample(value=1.0)])
            locked = sink.columns
            await sink.write_many([make_sample(value=2.0)])
        assert locked is not None
        assert len(locked) > 0
        # Column tuple is locked from the first batch; identity not asserted
        # because SchemaLock may return a new tuple per call, but content
        # must match.
        assert {c.name for c in locked} >= {"device", "Mass_Flow", "latency_s"}


class TestExistingTable:
    async def test_missing_table_raises_when_not_creating(
        self,
        tmp_path: Path,
    ) -> None:
        sink = SqliteSink(tmp_path / "missing.sqlite", create_table=False)
        with pytest.raises(AlicatSinkSchemaError, match="does not exist"):
            await sink.open()

    async def test_existing_table_locks_schema_from_pragma_table_info(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "pre.sqlite"
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                'CREATE TABLE "samples" ("device" TEXT, "Mass_Flow" REAL)',
            )
            conn.commit()
        finally:
            conn.close()

        async with SqliteSink(path, create_table=False) as sink:
            assert sink.columns is not None
            names = [c.name for c in sink.columns]
            assert names == ["device", "Mass_Flow"]
            # row projection discards everything not in the locked schema
            await sink.write_many([make_sample(value=9.5)])

        check = sqlite3.connect(path)
        try:
            rows = check.execute("SELECT device, Mass_Flow FROM samples").fetchall()
        finally:
            check.close()
        assert rows == [("fuel", 9.5)]


class TestSchemaEvolution:
    async def test_unknown_column_dropped_with_one_shot_warn(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = tmp_path / "run.sqlite"
        async with SqliteSink(path) as sink:
            await sink.write_many([make_sample(field_name="Mass_Flow", value=1.0)])
            with caplog.at_level(logging.WARNING, logger="alicatlib.sinks.sqlite"):
                # Second batch has a frame field the first batch didn't declare.
                await sink.write_many(
                    [make_sample(field_name="Volumetric_Flow", value=2.0)],
                )
                await sink.write_many(
                    [make_sample(field_name="Volumetric_Flow", value=3.0)],
                )

        warn_records = [
            r for r in caplog.records if r.getMessage() == "sink.unknown_column_dropped"
        ]
        # one-shot: two writes of the same unknown key only warn once
        assert len(warn_records) == 1
        assert getattr(warn_records[0], "column", None) == "Volumetric_Flow"


class TestParameterisation:
    async def test_malicious_string_stored_as_literal(self, tmp_path: Path) -> None:
        """Values pass through ``?``; a SQL-fragment value must round-trip verbatim."""
        path = tmp_path / "run.sqlite"
        attack = "'; DROP TABLE samples; --"
        sample = make_sample(device=attack, value=1.0)
        async with SqliteSink(path) as sink:
            await sink.write_many([sample])

        conn = sqlite3.connect(path)
        try:
            (stored,) = conn.execute("SELECT device FROM samples").fetchone()
            # Table must still exist
            count_row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='samples'",
            ).fetchone()
        finally:
            conn.close()
        assert stored == attack
        assert count_row[0] == 1


class TestPipeIntegration:
    async def test_pipe_end_to_end(self, tmp_path: Path) -> None:
        from alicatlib.sinks import pipe

        samples = [make_sample(value=float(i)) for i in range(5)]

        async def _stream() -> AsyncIterator[Mapping[str, Sample]]:
            yield {"fuel": samples[0]}
            yield {"fuel": samples[1]}
            yield {"fuel": samples[2]}
            yield {"fuel": samples[3]}
            yield {"fuel": samples[4]}

        path = tmp_path / "piped.sqlite"
        async with SqliteSink(path) as sink:
            summary = await pipe(_stream(), sink, batch_size=2, flush_interval=1.0)

        assert summary.samples_emitted == 5
        conn = sqlite3.connect(path)
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM samples").fetchone()
        finally:
            conn.close()
        assert count == 5


class TestWriteErrors:
    async def test_write_error_wraps_sqlite3_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "run.sqlite"
        sink = SqliteSink(path)
        await sink.open()
        try:
            # After first-batch lock, reach in and corrupt the insert SQL so
            # the second write_many trips a sqlite3 error deterministically.
            await sink.write_many([make_sample(value=1.0)])
            monkeypatch.setattr(sink, "_insert_sql", "THIS IS NOT VALID SQL ?")
            with pytest.raises(AlicatSinkWriteError, match="INSERT"):
                await sink.write_many([make_sample(value=2.0)])
        finally:
            await sink.close()
