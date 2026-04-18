"""Tests for :mod:`alicatlib.sinks` — InMemory, CSV, JSONL sinks + pipe().

Focus (design §5.15):

- :func:`sample_to_row` — stable column layout, frame field merge,
  wall-clock ``received_at`` precedence over frame's.
- :class:`InMemorySink` — append semantics, protocol conformance.
- :class:`CsvSink` — first-batch schema lock, header emission,
  unknown-column drop.
- :class:`JsonlSink` — one JSON object per line, no schema lock.
- :func:`pipe` — batched flush by size and by interval, final drain,
  returned :class:`AcquisitionSummary`.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import anyio
import pytest

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
    InMemorySink,
    JsonlSink,
    pipe,
    sample_to_row,
)
from alicatlib.streaming.recorder import AcquisitionSummary
from alicatlib.streaming.sample import Sample
from tests._typing import approx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decimal(v: str) -> float | str | None:
    return float(v)


def _format(field_name: str = "Mass_Flow") -> DataFrameFormat:
    return DataFrameFormat(
        fields=(
            DataFrameField(
                name=field_name,
                raw_name=field_name,
                type_name="decimal",
                statistic=Statistic.MASS_FLOW,
                unit=None,
                conditional=False,
                parser=_decimal,
            ),
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


def _frame(
    unit_id: str = "A",
    field_name: str = "Mass_Flow",
    value: float = 12.5,
    at: datetime | None = None,
) -> DataFrame:
    fmt = _format(field_name)
    parsed = ParsedFrame(
        unit_id=unit_id,
        values={field_name: value},
        values_by_statistic={Statistic.MASS_FLOW: value},
        status=frozenset[StatusCode](),
    )
    when = at if at is not None else datetime.now(UTC)
    return DataFrame.from_parsed(parsed, format=fmt, received_at=when, monotonic_ns=1000)


def _sample(
    device: str = "fuel",
    unit_id: str = "A",
    field_name: str = "Mass_Flow",
    value: float = 12.5,
    at: datetime | None = None,
) -> Sample:
    when = at if at is not None else datetime.now(UTC)
    return Sample(
        device=device,
        unit_id=unit_id,
        monotonic_ns=1000,
        requested_at=when,
        received_at=when + timedelta(milliseconds=5),
        midpoint_at=when + timedelta(milliseconds=2),
        latency_s=0.005,
        frame=_frame(unit_id=unit_id, field_name=field_name, value=value, at=when),
    )


class _BatchStream:
    """Minimal async-iterator stub that yields a pre-built batch list."""

    def __init__(self, batches: list[Mapping[str, Sample]]) -> None:
        self._batches = batches

    def __aiter__(self) -> AsyncIterator[Mapping[str, Sample]]:
        return self._generator()

    async def _generator(self) -> AsyncIterator[Mapping[str, Sample]]:
        for b in self._batches:
            yield b


# ---------------------------------------------------------------------------
# sample_to_row
# ---------------------------------------------------------------------------


class TestSampleToRow:
    def test_emits_all_header_fields(self) -> None:
        row = sample_to_row(_sample())
        expected_keys = {
            "device",
            "unit_id",
            "requested_at",
            "received_at",
            "midpoint_at",
            "latency_s",
            "Mass_Flow",
            "status",
        }
        assert set(row.keys()) == expected_keys

    def test_drops_frames_received_at_in_favour_of_samples(self) -> None:
        """Design §5.15: sample-level received_at wins over frame's."""
        at = datetime(2026, 4, 17, 15, 0, 0, tzinfo=UTC)
        sample = _sample(at=at)
        row = sample_to_row(sample)
        # The sample's received_at is 5 ms later than the frame's.
        assert row["received_at"] == sample.received_at.isoformat()
        assert row["received_at"] != sample.frame.received_at.isoformat()

    def test_empty_status_is_empty_string(self) -> None:
        row = sample_to_row(_sample())
        assert row["status"] == ""

    def test_drops_frame_unit_id_echo(self) -> None:
        """Real ``??D*`` always has the unit-id echo as its first field.

        A 2026-04-17 capture on 10v20 firmware surfaced this:
        the cached :class:`DataFrameFormat` names field 0 ``Unit_ID``
        (snake-cased from the primer's ``Unit ID`` column). When
        :func:`sample_to_row` merged the frame values, the row ended
        up with both ``unit_id`` (sample-level, lowercase) and
        ``Unit_ID`` (frame-level). SQLite is case-insensitive for
        column names and rejected the duplicate at CREATE TABLE. The
        frame's echo is always a duplicate of ``sample.unit_id``, so
        :func:`sample_to_row` drops both casings.
        """
        from datetime import UTC, datetime, timedelta

        from alicatlib.devices.data_frame import (
            DataFrame,
            DataFrameField,
            DataFrameFormat,
            DataFrameFormatFlavor,
            ParsedFrame,
        )
        from alicatlib.devices.models import StatusCode
        from alicatlib.registry import Statistic
        from alicatlib.streaming.sample import Sample

        def _decimal(v: str) -> float:
            return float(v)

        def _text(v: str) -> str:
            return v

        fmt = DataFrameFormat(
            fields=(
                DataFrameField(
                    name="Unit_ID",
                    raw_name="Unit ID",
                    type_name="string",
                    statistic=Statistic.NONE,
                    unit=None,
                    conditional=False,
                    parser=_text,
                ),
                DataFrameField(
                    name="Mass_Flow",
                    raw_name="Mass Flow",
                    type_name="decimal",
                    statistic=Statistic.MASS_FLOW,
                    unit=None,
                    conditional=False,
                    parser=_decimal,
                ),
            ),
            flavor=DataFrameFormatFlavor.DEFAULT,
        )
        when = datetime(2026, 4, 17, 15, 0, 0, tzinfo=UTC)
        parsed = ParsedFrame(
            unit_id="A",
            values={"Unit_ID": "A", "Mass_Flow": 12.5},
            values_by_statistic={Statistic.MASS_FLOW: 12.5},
            status=frozenset[StatusCode](),
        )
        frame = DataFrame.from_parsed(parsed, format=fmt, received_at=when, monotonic_ns=1)
        sample = Sample(
            device="primary",
            unit_id="A",
            monotonic_ns=1,
            requested_at=when,
            received_at=when + timedelta(milliseconds=5),
            midpoint_at=when + timedelta(milliseconds=2),
            latency_s=0.005,
            frame=frame,
        )

        row = sample_to_row(sample)
        assert "Unit_ID" not in row
        # Sample-level unit_id survives.
        assert row["unit_id"] == "A"
        # No other-case collision either.
        case_insensitive_keys = {k.lower() for k in row}
        assert len(case_insensitive_keys) == len(row), f"duplicate keys: {row.keys()}"


# ---------------------------------------------------------------------------
# InMemorySink
# ---------------------------------------------------------------------------


class TestInMemorySink:
    @pytest.mark.anyio
    async def test_context_manager_lifecycle(self) -> None:
        async with InMemorySink() as sink:
            assert sink.is_open
        assert not sink.is_open

    @pytest.mark.anyio
    async def test_write_before_open_raises(self) -> None:
        sink = InMemorySink()
        with pytest.raises(RuntimeError, match="before open"):
            await sink.write_many([_sample()])

    @pytest.mark.anyio
    async def test_preserves_insertion_order_across_batches(self) -> None:
        async with InMemorySink() as sink:
            await sink.write_many([_sample(device="a", value=1.0)])
            await sink.write_many([_sample(device="b", value=2.0), _sample(device="c", value=3.0)])
            assert [s.device for s in sink.samples] == ["a", "b", "c"]
            assert [s.frame.values["Mass_Flow"] for s in sink.samples] == [1.0, 2.0, 3.0]

    @pytest.mark.anyio
    async def test_close_preserves_buffer(self) -> None:
        """Close is just a state flip — samples remain available for inspection."""
        async with InMemorySink() as sink:
            await sink.write_many([_sample()])
        assert len(sink.samples) == 1


# ---------------------------------------------------------------------------
# CsvSink
# ---------------------------------------------------------------------------


class TestCsvSink:
    @pytest.mark.anyio
    async def test_writes_header_on_first_batch(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        async with CsvSink(path) as sink:
            await sink.write_many([_sample()])
        lines = path.read_text().splitlines()
        assert lines[0].startswith("device,unit_id,")
        assert "Mass_Flow" in lines[0]
        assert len(lines) == 2  # header + one row

    @pytest.mark.anyio
    async def test_row_round_trips_via_dictreader(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        async with CsvSink(path) as sink:
            await sink.write_many([_sample(device="fuel", value=12.5)])
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 1
        assert rows[0]["device"] == "fuel"
        assert float(rows[0]["Mass_Flow"]) == approx(12.5)

    @pytest.mark.anyio
    async def test_schema_locks_at_first_batch(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        async with CsvSink(path) as sink:
            await sink.write_many([_sample(field_name="Mass_Flow")])
            # Second batch has a different frame-field name → should be
            # dropped with a WARN (not reshape the header).
            await sink.write_many([_sample(field_name="Abs_Press")])
        assert sink.columns is not None
        assert "Mass_Flow" in sink.columns
        assert "Abs_Press" not in sink.columns

    @pytest.mark.anyio
    async def test_empty_write_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        async with CsvSink(path) as sink:
            await sink.write_many([])
        # File exists but empty — no header written without a sample.
        assert path.exists()
        assert path.read_text() == ""

    @pytest.mark.anyio
    async def test_write_before_open_raises(self, tmp_path: Path) -> None:
        sink = CsvSink(tmp_path / "out.csv")
        with pytest.raises(RuntimeError, match="before open"):
            await sink.write_many([_sample()])

    @pytest.mark.anyio
    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        sink = CsvSink(path)
        await sink.open()
        await sink.close()
        await sink.close()  # second close must not raise

    @pytest.mark.anyio
    async def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        path.write_text("stale content\n")
        async with CsvSink(path) as sink:
            await sink.write_many([_sample()])
        assert "stale content" not in path.read_text()


# ---------------------------------------------------------------------------
# JsonlSink
# ---------------------------------------------------------------------------


class TestJsonlSink:
    @pytest.mark.anyio
    async def test_one_json_object_per_line(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        async with JsonlSink(path) as sink:
            await sink.write_many(
                [_sample(device="a", value=1.0), _sample(device="b", value=2.0)],
            )
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        objs = [json.loads(line) for line in lines]
        assert objs[0]["device"] == "a"
        assert objs[1]["device"] == "b"
        assert objs[0]["Mass_Flow"] == 1.0

    @pytest.mark.anyio
    async def test_no_schema_lock_heterogeneous_frames(self, tmp_path: Path) -> None:
        """JSONL rows can differ in shape without dropping anything."""
        path = tmp_path / "out.jsonl"
        async with JsonlSink(path) as sink:
            await sink.write_many([_sample(field_name="Mass_Flow", value=1.0)])
            await sink.write_many([_sample(field_name="Abs_Press", value=14.7)])
        lines = path.read_text().splitlines()
        objs = [json.loads(line) for line in lines]
        assert "Mass_Flow" in objs[0]
        assert "Abs_Press" in objs[1]
        assert "Abs_Press" not in objs[0]  # row 1 didn't gain a column

    @pytest.mark.anyio
    async def test_write_before_open_raises(self, tmp_path: Path) -> None:
        sink = JsonlSink(tmp_path / "out.jsonl")
        with pytest.raises(RuntimeError, match="before open"):
            await sink.write_many([_sample()])

    @pytest.mark.anyio
    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        sink = JsonlSink(tmp_path / "out.jsonl")
        await sink.open()
        await sink.close()
        await sink.close()

    @pytest.mark.anyio
    async def test_empty_write_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        async with JsonlSink(path) as sink:
            await sink.write_many([])
        assert path.read_text() == ""


# ---------------------------------------------------------------------------
# pipe()
# ---------------------------------------------------------------------------


class TestPipe:
    @pytest.mark.anyio
    async def test_validation_rejects_nonpositive_batch_size(self) -> None:
        stream = _BatchStream([])
        async with InMemorySink() as sink:
            with pytest.raises(ValueError, match="batch_size"):
                await pipe(aiter(stream), sink, batch_size=0)

    @pytest.mark.anyio
    async def test_validation_rejects_nonpositive_flush_interval(self) -> None:
        stream = _BatchStream([])
        async with InMemorySink() as sink:
            with pytest.raises(ValueError, match="flush_interval"):
                await pipe(aiter(stream), sink, flush_interval=0)

    @pytest.mark.anyio
    async def test_drains_stream_into_sink(self) -> None:
        batches: list[Mapping[str, Sample]] = [
            {"fuel": _sample(device="fuel", value=1.0)},
            {"fuel": _sample(device="fuel", value=2.0)},
            {"fuel": _sample(device="fuel", value=3.0)},
        ]
        stream = _BatchStream(batches)
        async with InMemorySink() as sink:
            summary = await pipe(aiter(stream), sink, batch_size=1)
        assert isinstance(summary, AcquisitionSummary)
        assert summary.samples_emitted == 3
        assert [s.frame.values["Mass_Flow"] for s in sink.samples] == [1.0, 2.0, 3.0]

    @pytest.mark.anyio
    async def test_final_flush_drains_partial_buffer(self) -> None:
        """A leftover buffer smaller than batch_size still flushes on stream end."""
        batches: list[Mapping[str, Sample]] = [
            {"fuel": _sample(device="fuel", value=1.0)},
            {"fuel": _sample(device="fuel", value=2.0)},
        ]
        stream = _BatchStream(batches)
        async with InMemorySink() as sink:
            # batch_size=100 → buffer never hits the threshold; only the
            # final flush drains.
            summary = await pipe(aiter(stream), sink, batch_size=100)
        assert summary.samples_emitted == 2
        assert len(sink.samples) == 2

    @pytest.mark.anyio
    async def test_multi_device_batch_emits_one_row_per_device(self) -> None:
        """A single tick with two devices → two samples in order."""
        batches: list[Mapping[str, Sample]] = [
            {
                "fuel": _sample(device="fuel", value=1.0),
                "air": _sample(device="air", value=2.0),
            },
        ]
        stream = _BatchStream(batches)
        async with InMemorySink() as sink:
            summary = await pipe(aiter(stream), sink, batch_size=1)
        assert summary.samples_emitted == 2
        assert {s.device for s in sink.samples} == {"fuel", "air"}

    @pytest.mark.anyio
    async def test_empty_stream_summary(self) -> None:
        stream = _BatchStream([])
        async with InMemorySink() as sink:
            summary = await pipe(aiter(stream), sink)
        assert summary.samples_emitted == 0
        assert summary.samples_late == 0
        assert summary.max_drift_ms == 0.0

    @pytest.mark.anyio
    async def test_csv_pipe_end_to_end(self, tmp_path: Path) -> None:
        """End-to-end: batch stream → pipe() → CsvSink → readback."""
        path = tmp_path / "out.csv"
        batches: list[Mapping[str, Sample]] = [
            {"fuel": _sample(device="fuel", value=1.0)},
            {"fuel": _sample(device="fuel", value=2.0)},
        ]
        stream = _BatchStream(batches)
        async with CsvSink(path) as sink:
            summary = await pipe(aiter(stream), sink, batch_size=1)
        assert summary.samples_emitted == 2
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2
        assert [float(r["Mass_Flow"]) for r in rows] == [1.0, 2.0]

    @pytest.mark.anyio
    async def test_jsonl_pipe_end_to_end(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        batches: list[Mapping[str, Sample]] = [
            {"fuel": _sample(device="fuel", value=1.5)},
            {"air": _sample(device="air", value=2.5)},
        ]
        stream = _BatchStream(batches)
        async with JsonlSink(path) as sink:
            summary = await pipe(aiter(stream), sink, batch_size=1)
        assert summary.samples_emitted == 2
        objs = [json.loads(line) for line in path.read_text().splitlines()]
        assert objs[0]["Mass_Flow"] == 1.5
        assert objs[1]["Mass_Flow"] == 2.5

    @pytest.mark.anyio
    async def test_flush_interval_triggers_even_below_batch_size(self) -> None:
        """If flush_interval elapses before batch_size hits, still flushes."""

        class _SlowStream:
            def __aiter__(self) -> AsyncIterator[Mapping[str, Sample]]:
                return self._gen()

            async def _gen(self) -> AsyncIterator[Mapping[str, Sample]]:
                for i in range(3):
                    yield {"fuel": _sample(device="fuel", value=float(i))}
                    await anyio.sleep(0.05)  # longer than flush_interval below

        stream = _SlowStream()
        async with InMemorySink() as sink:
            summary = await pipe(aiter(stream), sink, batch_size=100, flush_interval=0.01)
        # 3 samples, 100-wide batch never fills — flush must come from the
        # interval threshold for intermediate batches, plus the final drain.
        assert summary.samples_emitted == 3
