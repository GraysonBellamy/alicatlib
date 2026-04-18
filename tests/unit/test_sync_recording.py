"""Tests for :mod:`alicatlib.sync.recording` and :mod:`alicatlib.sync.sinks`.

Drives the sync :func:`record` / :func:`pipe` end-to-end against a
stub PollSource + :class:`SyncInMemorySink`, then exercises each
concrete sink wrapper (CSV, JSONL, SQLite, Parquet) with minimal
sample fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest

from alicatlib.commands import Capability
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.data_frame import (
    DataFrame,
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
    ParsedFrame,
)
from alicatlib.devices.models import DeviceInfo, StatusCode
from alicatlib.errors import AlicatSinkDependencyError
from alicatlib.firmware import FirmwareVersion
from alicatlib.manager import DeviceResult
from alicatlib.registry import Statistic
from alicatlib.streaming import Sample
from alicatlib.sync import (
    AcquisitionSummary,
    SyncCsvSink,
    SyncInMemorySink,
    SyncJsonlSink,
    SyncParquetSink,
    SyncPortal,
    SyncSqliteSink,
    pipe,
    record,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _format() -> DataFrameFormat:
    def _decimal(value: str) -> float | str | None:
        return float(value)

    return DataFrameFormat(
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


def _info() -> DeviceInfo:  # pyright: ignore[reportUnusedFunction]
    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model="MC-100SCCM-D",
        serial="1",
        manufactured="2024-01-01",
        calibrated="2024-02-01",
        calibrated_by="ACS",
        software="10v20",
        firmware=FirmwareVersion.parse("10v20"),
        firmware_date=date(2024, 1, 1),
        kind=DeviceKind.FLOW_CONTROLLER,
        media=Medium.GAS,
        capabilities=Capability.NONE,
    )


def _frame(mass_flow: float = 1.0, tick: int = 0) -> DataFrame:
    fmt = _format()
    parsed = ParsedFrame(
        unit_id="A",
        values={"Mass_Flow": mass_flow},
        values_by_statistic={Statistic.MASS_FLOW: mass_flow},
        status=frozenset[StatusCode](),
    )
    return DataFrame.from_parsed(
        parsed,
        format=fmt,
        received_at=datetime.now(UTC),
        monotonic_ns=1_000_000 * tick,
    )


def _sample(tick: int = 0, mass_flow: float = 1.0) -> Sample:
    now = datetime.now(UTC)
    return Sample(
        device="dev0",
        unit_id="A",
        requested_at=now,
        received_at=now,
        midpoint_at=now,
        latency_s=0.001,
        monotonic_ns=1_000_000 * tick,
        frame=_frame(mass_flow=mass_flow, tick=tick),
    )


class _StubPoll:
    def __init__(self, device_names: Sequence[str] = ("dev0",)) -> None:
        self._names = tuple(device_names)
        self.calls = 0

    async def poll(
        self,
        names: Sequence[str] | None = None,
    ) -> Mapping[str, DeviceResult[DataFrame]]:
        self.calls += 1
        targets = list(names) if names is not None else list(self._names)
        return {
            n: DeviceResult(
                value=_frame(mass_flow=float(self.calls), tick=self.calls),
                error=None,
            )
            for n in targets
        }


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------


class TestSyncRecord:
    def test_yields_tick_batches(self) -> None:
        src = _StubPoll()
        with record(src, rate_hz=100, duration=0.05) as stream:
            batches: list[Mapping[str, Sample]] = list(stream)
        assert batches
        assert all("dev0" in b for b in batches)

    def test_shared_portal_reused(self) -> None:
        src = _StubPoll()
        with SyncPortal() as portal:
            with record(src, rate_hz=100, duration=0.02, portal=portal) as stream:
                for _ in stream:
                    pass
            # Portal still alive after record exits.
            assert portal.running is True

    def test_invalid_rate_raises(self) -> None:
        src = _StubPoll()

        def _body() -> None:
            with record(src, rate_hz=0, duration=0.01) as stream:
                for _ in stream:
                    pass

        with pytest.raises(ValueError, match="rate_hz"):
            _body()


# ---------------------------------------------------------------------------
# pipe()
# ---------------------------------------------------------------------------


class TestSyncPipe:
    def test_pipe_into_memory_sink(self) -> None:
        src = _StubPoll()
        with SyncPortal() as portal:
            with (
                SyncInMemorySink(portal=portal) as sink,
                record(src, rate_hz=100, duration=0.05, portal=portal) as stream,
            ):
                summary = pipe(stream, sink, batch_size=4, flush_interval=0.5)
            assert isinstance(summary, AcquisitionSummary)
            assert summary.samples_emitted == len(sink.samples)
            assert summary.samples_emitted > 0

    def test_pipe_flushes_trailing_buffer(self) -> None:
        """Samples less than batch_size must still flush on stream end."""

        def _iter() -> list[dict[str, Sample]]:
            return [{"dev0": _sample(tick=i)} for i in range(3)]

        class _FakeStream:
            def __init__(self, batches: list[dict[str, Sample]]) -> None:
                self._it = iter(batches)

            def __iter__(self) -> _FakeStream:
                return self

            def __next__(self) -> dict[str, Sample]:
                return next(self._it)

        with SyncInMemorySink() as sink:
            summary = pipe(_FakeStream(_iter()), sink, batch_size=100, flush_interval=60)
        assert summary.samples_emitted == 3
        assert len(sink.samples) == 3

    def test_pipe_validates_batch_size(self) -> None:
        with SyncInMemorySink() as sink, pytest.raises(ValueError, match="batch_size"):
            pipe(iter([]), sink, batch_size=0)

    def test_pipe_validates_flush_interval(self) -> None:
        with SyncInMemorySink() as sink, pytest.raises(ValueError, match="flush_interval"):
            pipe(iter([]), sink, flush_interval=0)


# ---------------------------------------------------------------------------
# Sink wrappers
# ---------------------------------------------------------------------------


class TestSyncSinks:
    def test_in_memory_sink_records(self) -> None:
        with SyncInMemorySink() as sink:
            sink.write_many([_sample(tick=0), _sample(tick=1)])
            assert len(sink.samples) == 2

    def test_in_memory_sink_is_one_shot(self) -> None:
        sink = SyncInMemorySink()
        with sink:
            pass
        with pytest.raises(RuntimeError, match="not reusable"):
            sink.__enter__()

    def test_csv_sink_writes_file(self, tmp_path: Path) -> None:
        path = tmp_path / "samples.csv"
        with SyncCsvSink(path) as sink:
            sink.write_many([_sample(tick=0), _sample(tick=1)])
        text = path.read_text()
        assert "device,unit_id" in text.splitlines()[0]
        assert text.count("\n") == 3  # header + 2 rows

    def test_jsonl_sink_writes_file(self, tmp_path: Path) -> None:
        path = tmp_path / "samples.jsonl"
        with SyncJsonlSink(path) as sink:
            sink.write_many([_sample(tick=0)])
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["device"] == "dev0"

    def test_sqlite_sink_roundtrip(self, tmp_path: Path) -> None:
        import sqlite3

        path = tmp_path / "samples.db"
        with SyncSqliteSink(path) as sink:
            sink.write_many([_sample(tick=0), _sample(tick=1)])

        conn = sqlite3.connect(path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        finally:
            conn.close()
        assert count == 2

    def test_parquet_sink_roundtrip(self, tmp_path: Path) -> None:
        pq: Any = pytest.importorskip("pyarrow.parquet")

        path = tmp_path / "samples.parquet"
        with SyncParquetSink(path) as sink:
            sink.write_many([_sample(tick=0), _sample(tick=1)])

        table = pq.read_table(path)
        assert table.num_rows == 2
        assert "device" in table.schema.names


# ---------------------------------------------------------------------------
# Optional-dependency error semantics — raises :class:`AlicatSinkDependencyError`
# through the portal just like the async path.
# ---------------------------------------------------------------------------


class TestDependencyErrors:
    def test_parquet_missing_dep_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If pyarrow is absent, open() surfaces AlicatSinkDependencyError."""
        # Hide pyarrow by setting it to None in sys.modules and forcing the
        # async sink's lazy import to fail.
        import sys

        monkeypatch.setitem(sys.modules, "pyarrow", None)
        monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
        sink = SyncParquetSink(tmp_path / "x.parquet")
        with pytest.raises(AlicatSinkDependencyError):
            sink.__enter__()
