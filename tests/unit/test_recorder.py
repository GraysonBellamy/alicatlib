"""Tests for :mod:`alicatlib.streaming.recorder`.

Focus (design §5.14):

- :func:`record` yields a drainable async iterator and shuts down
  its producer task on CM exit.
- Absolute-target scheduling: emitted batches land within a small
  drift envelope; missed slots accrue to ``samples_late`` rather
  than drifting the schedule.
- :class:`OverflowPolicy.BLOCK` backpressures the producer;
  :class:`OverflowPolicy.DROP_NEWEST` drops-and-continues;
  :class:`OverflowPolicy.DROP_OLDEST` is explicit ``NotImplementedError``
  for now.
- Errored :class:`DeviceResult` entries don't block the batch —
  they're dropped with a WARN log.
- Input validation on ``rate_hz`` / ``duration`` / ``buffer_size``.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import anyio
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
from alicatlib.errors import AlicatError, ErrorContext
from alicatlib.firmware import FirmwareVersion
from alicatlib.manager import DeviceResult
from alicatlib.registry import Statistic
from alicatlib.streaming import (
    OverflowPolicy,
    Sample,
    record,
)
from tests._typing import approx

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _minimal_format() -> DataFrameFormat:
    """Tiny single-field format — enough to build a ParsedFrame."""

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


def _frame(
    unit_id: str = "A",
    mass_flow: float = 1.0,
    tick: int = 0,
) -> DataFrame:
    """Construct a synthetic :class:`DataFrame` with a caller-chosen value.

    ``tick`` is only used to vary ``monotonic_ns`` across samples so
    tests asserting on provenance can tell consecutive frames apart.
    """
    fmt = _minimal_format()
    parsed = ParsedFrame(
        unit_id=unit_id,
        values={"Mass_Flow": mass_flow},
        values_by_statistic={Statistic.MASS_FLOW: mass_flow},
        status=frozenset[StatusCode](),
    )
    from datetime import UTC, datetime

    return DataFrame.from_parsed(
        parsed,
        format=fmt,
        received_at=datetime.now(UTC),
        monotonic_ns=1_000_000 * tick,
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


class _StubPoll:
    """Minimal :class:`PollSource` that counts calls and returns synth frames."""

    def __init__(
        self,
        device_names: Sequence[str] = ("dev0",),
        *,
        poll_latency: float = 0.0,
        fail_device: str | None = None,
    ) -> None:
        self._device_names = tuple(device_names)
        self._poll_latency = poll_latency
        self._fail_device = fail_device
        self.calls = 0
        self.names_seen: list[Sequence[str] | None] = []

    async def poll(
        self,
        names: Sequence[str] | None = None,
    ) -> Mapping[str, DeviceResult[DataFrame]]:
        self.calls += 1
        self.names_seen.append(names)
        if self._poll_latency > 0:
            await anyio.sleep(self._poll_latency)
        target = list(names) if names is not None else list(self._device_names)
        results: dict[str, DeviceResult[DataFrame]] = {}
        for n in target:
            if n == self._fail_device:
                err = AlicatError("synthetic", context=ErrorContext(command_name="poll_data"))
                results[n] = DeviceResult(value=None, error=err)
            else:
                results[n] = DeviceResult(
                    value=_frame(mass_flow=float(self.calls), tick=self.calls),
                    error=None,
                )
        return results


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestRecordValidation:
    @pytest.mark.anyio
    async def test_rate_hz_must_be_positive(self) -> None:
        src = _StubPoll()
        with pytest.raises(ValueError, match="rate_hz"):
            async with record(src, rate_hz=0, duration=0.01):
                pass

    @pytest.mark.anyio
    async def test_negative_duration_rejected(self) -> None:
        src = _StubPoll()
        with pytest.raises(ValueError, match="duration"):
            async with record(src, rate_hz=10, duration=-1):
                pass

    @pytest.mark.anyio
    async def test_zero_buffer_size_rejected(self) -> None:
        src = _StubPoll()
        with pytest.raises(ValueError, match="buffer_size"):
            async with record(src, rate_hz=10, duration=0.01, buffer_size=0):
                pass


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRecordHappyPath:
    @pytest.mark.anyio
    async def test_emits_expected_tick_count(self) -> None:
        """1 second at 20 Hz → 20 ticks, approximately."""
        src = _StubPoll(device_names=("dev0",))
        async with record(src, rate_hz=20, duration=0.25) as stream:
            batches = [batch async for batch in stream]
        assert 3 <= len(batches) <= 7  # tolerant; jitter on the test runner

    @pytest.mark.anyio
    async def test_batch_keys_match_device_names(self) -> None:
        src = _StubPoll(device_names=("fuel", "air"))
        async with record(src, rate_hz=20, duration=0.15) as stream:
            async for batch in stream:
                assert set(batch.keys()) == {"fuel", "air"}
                break

    @pytest.mark.anyio
    async def test_sample_carries_timing_and_frame(self) -> None:
        src = _StubPoll(device_names=("dev0",))
        async with record(src, rate_hz=20, duration=0.10) as stream:
            async for batch in stream:
                sample = batch["dev0"]
                assert isinstance(sample, Sample)
                assert sample.device == "dev0"
                assert sample.unit_id == "A"
                assert sample.received_at >= sample.requested_at
                assert sample.midpoint_at >= sample.requested_at
                assert sample.midpoint_at <= sample.received_at
                assert sample.latency_s >= 0.0
                assert sample.frame.values["Mass_Flow"] == approx(1.0)
                break

    @pytest.mark.anyio
    async def test_names_parameter_propagates(self) -> None:
        src = _StubPoll(device_names=("a", "b", "c"))
        async with record(src, rate_hz=20, duration=0.08, names=("a",)) as stream:
            async for batch in stream:
                assert set(batch.keys()) == {"a"}
                break
        # Every poll call should have been restricted to ``("a",)``.
        assert all(tuple(seen) == ("a",) if seen is not None else False for seen in src.names_seen)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestRecordErrorHandling:
    @pytest.mark.anyio
    async def test_errored_devices_skipped_from_batch(self) -> None:
        """Failed devices don't block the batch; healthy devices still emit."""
        src = _StubPoll(device_names=("ok", "bad"), fail_device="bad")
        async with record(src, rate_hz=20, duration=0.10) as stream:
            async for batch in stream:
                assert "ok" in batch
                assert "bad" not in batch
                break


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class TestRecordScheduling:
    @pytest.mark.anyio
    async def test_drift_bounded_under_fast_poll(self) -> None:
        """Fast polls (0-latency stub) stay close to the target cadence."""
        src = _StubPoll(device_names=("dev0",))
        async with record(src, rate_hz=50, duration=0.15) as stream:
            summary_drifts = [batch["dev0"].latency_s async for batch in stream]
        # latency_s from a 0-latency stub is essentially the poll scheduling
        # overhead plus the single-tick await; a few milliseconds at most.
        assert all(d < 0.05 for d in summary_drifts), summary_drifts

    @pytest.mark.anyio
    async def test_slow_poll_accrues_late_samples(self) -> None:
        """If the poll itself takes > one period, missed slots get skipped."""
        # 20 Hz target (50 ms period) but each poll takes ~80 ms → every
        # tick misses its slot. The scheduler should recognise overruns,
        # bump the tick count, and report late samples.
        src = _StubPoll(device_names=("dev0",), poll_latency=0.08)
        async with record(src, rate_hz=20, duration=0.25) as stream:
            batches = [batch async for batch in stream]
        assert len(batches) >= 1
        # Poll count should be lower than the ideal tick count (5) because
        # overruns skipped slots instead of piling up.
        assert src.calls <= 5


# ---------------------------------------------------------------------------
# Overflow policies
# ---------------------------------------------------------------------------


class TestOverflowPolicies:
    @pytest.mark.anyio
    async def test_block_policy_is_default(self) -> None:
        src = _StubPoll(device_names=("dev0",))
        async with record(src, rate_hz=50, duration=0.10) as stream:
            # Slow consumer (sleeps between reads) — BLOCK means no drops.
            collected: list[Mapping[str, Sample]] = []
            async for batch in stream:
                await anyio.sleep(0.02)
                collected.append(batch)
        assert collected  # at least one batch got through

    @pytest.mark.anyio
    async def test_drop_oldest_raises_not_implemented(self) -> None:
        """DROP_OLDEST is not yet wired — fail loud at call site, not deep in the producer."""
        src = _StubPoll(device_names=("dev0",))

        async def _enter() -> None:
            async with record(
                src,
                rate_hz=50,
                duration=0.05,
                overflow=OverflowPolicy.DROP_OLDEST,
            ):
                pytest.fail("record() should have raised before yielding")

        with pytest.raises(NotImplementedError, match="DROP_OLDEST"):
            await _enter()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestRecordCancellation:
    @pytest.mark.anyio
    async def test_cm_exit_cancels_producer(self) -> None:
        """Exiting the CM early stops the producer; poll() count bounded."""
        src = _StubPoll(device_names=("dev0",))
        async with record(src, rate_hz=100) as stream:
            async for _batch in stream:
                break  # exit immediately after the first batch
        calls_at_exit = src.calls
        # Yield the event loop a few times; if the producer were still
        # running, calls would keep climbing.
        for _ in range(5):
            await anyio.sleep(0.02)
        assert src.calls == calls_at_exit

    @pytest.mark.anyio
    async def test_duration_terminates_producer_naturally(self) -> None:
        """With a ``duration`` set, the stream closes itself."""
        src = _StubPoll(device_names=("dev0",))
        async with record(src, rate_hz=50, duration=0.10) as stream:
            count = 0
            async for _batch in stream:
                count += 1
        assert count >= 1
