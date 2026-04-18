"""Tests for :class:`alicatlib.sinks.ParquetSink`.

Focus (design §5.15):

- Round-trip via :func:`pyarrow.parquet.read_table`.
- zstd is the default codec; caller-supplied alternatives are respected.
- Schema locked on first batch; unknown columns drop with one-shot WARN.
- One row group per ``write_many`` by default; ``row_group_size`` overrides.
- Missing ``pyarrow`` (the ``parquet`` extra) surfaces as
  :class:`AlicatSinkDependencyError` on :meth:`open`, not at import time.

Skipped on bare-core installs via ``pytest.importorskip`` — the sink's
dependency-missing error path has its own test that monkey-patches
``sys.modules`` so it runs even when pyarrow is installed.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import pytest

from alicatlib.errors import AlicatConfigurationError, AlicatSinkDependencyError
from alicatlib.sinks import ParquetSink
from tests.unit._sink_fixtures import make_sample

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from alicatlib.streaming.sample import Sample

pyarrow = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


pytestmark = pytest.mark.anyio


class TestConstruction:
    def test_rejects_invalid_row_group_size(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="row_group_size"):
            ParquetSink(tmp_path / "x.parquet", row_group_size=0)
        with pytest.raises(ValueError, match="row_group_size"):
            ParquetSink(tmp_path / "x.parquet", row_group_size=-5)

    def test_exposes_path_and_compression(self, tmp_path: Path) -> None:
        sink = ParquetSink(tmp_path / "x.parquet", compression="snappy")
        assert sink.path == tmp_path / "x.parquet"
        assert sink.compression == "snappy"
        assert sink.columns is None


class TestLifecycle:
    async def test_context_manager_open_close(self, tmp_path: Path) -> None:
        async with ParquetSink(tmp_path / "run.parquet"):
            pass  # open/close only

    async def test_close_without_open_is_noop(self, tmp_path: Path) -> None:
        sink = ParquetSink(tmp_path / "never.parquet")
        await sink.close()

    async def test_open_is_idempotent(self, tmp_path: Path) -> None:
        async with ParquetSink(tmp_path / "run.parquet") as sink:
            await sink.open()

    async def test_write_before_open_raises(self, tmp_path: Path) -> None:
        sink = ParquetSink(tmp_path / "run.parquet")
        with pytest.raises(RuntimeError, match="write_many called before open"):
            await sink.write_many([make_sample()])

    async def test_empty_batch_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.parquet"
        async with ParquetSink(path) as sink:
            await sink.write_many([])
        # No file should have been created (writer never opened)
        assert not path.exists()


class TestRoundTrip:
    async def test_round_trip_via_read_table(self, tmp_path: Path) -> None:
        path = tmp_path / "run.parquet"
        samples = [make_sample(value=v) for v in (1.0, 2.0, 3.0)]
        async with ParquetSink(path) as sink:
            await sink.write_many(samples[:2])
            await sink.write_many(samples[2:])

        table = pq.read_table(path)
        assert table.num_rows == 3
        assert table.column("Mass_Flow").to_pylist() == [1.0, 2.0, 3.0]
        assert table.column("device").to_pylist() == ["fuel", "fuel", "fuel"]

    async def test_schema_has_correct_pyarrow_types(self, tmp_path: Path) -> None:
        path = tmp_path / "run.parquet"
        async with ParquetSink(path) as sink:
            await sink.write_many([make_sample(value=1.0)])

        table = pq.read_table(path)
        by_name = {f.name: f.type for f in table.schema}
        assert str(by_name["Mass_Flow"]) == "double"
        assert str(by_name["latency_s"]) == "double"
        assert str(by_name["device"]) == "string"
        assert str(by_name["status"]) == "string"

    async def test_all_fields_are_nullable(self, tmp_path: Path) -> None:
        path = tmp_path / "run.parquet"
        async with ParquetSink(path) as sink:
            await sink.write_many([make_sample(value=1.0)])
        table = pq.read_table(path)
        assert all(field.nullable for field in table.schema)


class TestCompression:
    async def test_zstd_by_default(self, tmp_path: Path) -> None:
        path = tmp_path / "zstd.parquet"
        async with ParquetSink(path) as sink:
            await sink.write_many([make_sample(value=1.0)])
        meta = pq.ParquetFile(path).metadata
        assert meta.row_group(0).column(0).compression == "ZSTD"

    async def test_snappy_respected(self, tmp_path: Path) -> None:
        path = tmp_path / "snappy.parquet"
        async with ParquetSink(path, compression="snappy") as sink:
            await sink.write_many([make_sample(value=1.0)])
        meta = pq.ParquetFile(path).metadata
        assert meta.row_group(0).column(0).compression == "SNAPPY"


class TestRowGroups:
    async def test_one_row_group_per_write_many(self, tmp_path: Path) -> None:
        path = tmp_path / "rg.parquet"
        async with ParquetSink(path) as sink:
            await sink.write_many([make_sample(value=1.0), make_sample(value=2.0)])
            await sink.write_many([make_sample(value=3.0)])
            await sink.write_many([make_sample(value=4.0), make_sample(value=5.0)])
        assert pq.ParquetFile(path).num_row_groups == 3


class TestSchemaLock:
    async def test_columns_locked_after_first_batch(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.parquet"
        async with ParquetSink(path) as sink:
            await sink.write_many([make_sample(value=1.0)])
            locked = sink.columns
            await sink.write_many([make_sample(value=2.0)])
        assert locked is not None
        assert {c.name for c in locked} >= {"device", "Mass_Flow", "latency_s"}

    async def test_unknown_column_dropped_with_one_shot_warn(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = tmp_path / "drift.parquet"
        async with ParquetSink(path) as sink:
            await sink.write_many([make_sample(field_name="Mass_Flow", value=1.0)])
            with caplog.at_level(logging.WARNING, logger="alicatlib.sinks.parquet"):
                await sink.write_many(
                    [make_sample(field_name="Pressure", value=2.0)],
                )
                await sink.write_many(
                    [make_sample(field_name="Pressure", value=3.0)],
                )
        warns = [r for r in caplog.records if r.getMessage() == "sink.unknown_column_dropped"]
        assert len(warns) == 1
        assert getattr(warns[0], "column", None) == "Pressure"

    async def test_missing_column_rows_written_as_null(self, tmp_path: Path) -> None:
        path = tmp_path / "nulls.parquet"
        async with ParquetSink(path) as sink:
            await sink.write_many([make_sample(field_name="Mass_Flow", value=1.0)])
            # Drifted field -> Mass_Flow missing; locked schema fills it None
            await sink.write_many([make_sample(field_name="Pressure", value=2.0)])
        table = pq.read_table(path)
        values = table.column("Mass_Flow").to_pylist()
        assert values == [1.0, None]


class TestMissingExtra:
    async def test_raises_when_pyarrow_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify the lazy-import error surfaces on :meth:`open`, not import."""
        monkeypatch.setitem(sys.modules, "pyarrow", None)
        monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
        sink = ParquetSink(tmp_path / "x.parquet")  # construction still OK
        with pytest.raises(AlicatSinkDependencyError) as excinfo:
            await sink.open()
        assert "alicatlib[parquet]" in str(excinfo.value)
        assert isinstance(excinfo.value, AlicatConfigurationError)


class TestPipeIntegration:
    async def test_pipe_end_to_end(self, tmp_path: Path) -> None:
        from alicatlib.sinks import pipe

        samples = [make_sample(value=float(i)) for i in range(4)]

        async def _stream() -> AsyncIterator[Mapping[str, Sample]]:
            for s in samples:
                yield {"fuel": s}

        path = tmp_path / "piped.parquet"
        async with ParquetSink(path) as sink:
            summary = await pipe(_stream(), sink, batch_size=2, flush_interval=1.0)

        assert summary.samples_emitted == 4
        assert pq.read_table(path).num_rows == 4
