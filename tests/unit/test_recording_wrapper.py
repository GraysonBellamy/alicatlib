"""Tests for :class:`Recording` and the mutable :class:`AcquisitionSummary`.

Covers the spec §M contract:

- ``record()`` yields a :class:`Recording` carrying ``stream`` /
  ``summary`` / ``rate_hz``.
- :class:`AcquisitionSummary` is mutable and the recorder updates
  counters in place during the run (live progress polling works).
- ``finished_at`` is ``None`` while in flight and set on CM exit.
- ``Recording`` delegates iteration to ``stream`` so ``async for batch
  in recording`` works without manually dereferencing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import anyio.lowlevel
import pytest

from alicatlib import DeviceResult
from alicatlib.devices.models import StatusCode
from alicatlib.devices.reading import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
    ParsedFrame,
    Reading,
)
from alicatlib.registry import Statistic
from alicatlib.streaming import AcquisitionSummary, Recording, record

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _minimal_format() -> DataFrameFormat:
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


def _reading() -> Reading:
    from datetime import UTC, datetime

    fmt = _minimal_format()
    parsed = ParsedFrame(
        unit_id="A",
        values={"Mass_Flow": 1.0},
        values_by_statistic={Statistic.MASS_FLOW: 1.0},
        status=frozenset[StatusCode](),
    )
    return Reading.from_parsed(
        parsed,
        reading_format=fmt,
        received_at=datetime.now(UTC),
        t_mono_ns=0,
    )


class _StubPollSource:
    async def poll(
        self,
        names: Sequence[str] | None = None,
    ) -> Mapping[str, DeviceResult[Reading]]:
        del names
        return {"dev0": DeviceResult.success(_reading())}


class TestAcquisitionSummary:
    def test_is_mutable(self) -> None:
        """Spec §M flips the dataclass from frozen to mutable."""
        from datetime import UTC, datetime

        summary = AcquisitionSummary(started_at=datetime.now(UTC))
        summary.samples_emitted = 5
        summary.samples_late = 2
        summary.max_drift_ms = 3.14
        assert summary.samples_emitted == 5
        assert summary.samples_late == 2
        assert summary.max_drift_ms == 3.14

    def test_finished_at_starts_none(self) -> None:
        """Live recordings have ``finished_at is None``."""
        from datetime import UTC, datetime

        summary = AcquisitionSummary(started_at=datetime.now(UTC))
        assert summary.finished_at is None


class TestRecordingWrapper:
    @pytest.mark.anyio
    async def test_yields_recording_with_stream_summary_rate(self) -> None:
        async with record(_StubPollSource(), rate_hz=20, duration=0.05) as recording:
            assert isinstance(recording, Recording)
            assert recording.rate_hz == 20
            assert isinstance(recording.summary, AcquisitionSummary)
            # Drain via the recording itself (iteration delegation).
            count = 0
            async for _batch in recording:
                count += 1
            assert count >= 1

    @pytest.mark.anyio
    async def test_summary_updates_in_place_during_run(self) -> None:
        """Live progress polling: the same instance carries growing counters."""
        async with record(_StubPollSource(), rate_hz=50, duration=0.10) as recording:
            summary = recording.summary
            assert summary.finished_at is None
            collected = [summary.samples_emitted async for _batch in recording]
            # Once at least one batch is published, the counter incremented.
            assert collected, "expected at least one batch"
            assert summary.samples_emitted >= 1
        # After CM exit, finished_at is populated.
        assert summary.finished_at is not None

    @pytest.mark.anyio
    async def test_stream_attribute_still_iterable(self) -> None:
        """Backwards-compatible access path: ``recording.stream`` works."""
        async with record(_StubPollSource(), rate_hz=20, duration=0.05) as recording:
            batches = [batch async for batch in recording.stream]
            assert len(batches) >= 1
            assert summary_is_running(recording.summary) or recording.summary.samples_emitted > 0


def summary_is_running(summary: AcquisitionSummary) -> bool:
    return summary.finished_at is None


class TestPipeAcceptsRecording:
    @pytest.mark.anyio
    async def test_pipe_drains_recording(self) -> None:
        """``pipe()`` accepts either a raw stream or a Recording."""
        from alicatlib.sinks import InMemorySink, pipe

        sink = InMemorySink()
        async with sink:
            async with record(_StubPollSource(), rate_hz=50, duration=0.08) as recording:
                # Pass the Recording directly — pipe should unwrap .stream.
                summary = await pipe(recording, sink)
            assert summary.samples_emitted >= 1
            assert summary.finished_at is not None
            assert len(sink.samples) >= 1

    @pytest.mark.anyio
    async def test_pipe_drains_raw_stream(self) -> None:
        """The legacy "pass .stream" path still works."""
        from alicatlib.sinks import InMemorySink, pipe

        sink = InMemorySink()
        async with sink:
            async with record(_StubPollSource(), rate_hz=50, duration=0.08) as recording:
                summary = await pipe(recording.stream, sink)
            assert summary.samples_emitted >= 1


@pytest.mark.anyio
async def test_recording_iteration_yields_same_batches_as_stream() -> None:
    """``async for batch in recording`` must yield the same payloads
    as ``async for batch in recording.stream``."""
    async with record(_StubPollSource(), rate_hz=50, duration=0.06) as recording:
        batches: list[Mapping[str, object]] = [batch async for batch in recording]
    assert all("dev0" in b for b in batches)
    # Sanity: anyio's cancel + drain didn't lose all batches.
    assert batches, "expected at least one batch from the iteration"
    # Tiny await so the parameterised trio backend exits cleanly.
    await anyio.lowlevel.checkpoint()
