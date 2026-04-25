"""Absolute-target recorder — ``record()`` emits timed :class:`Sample` batches.

:func:`record` is the v1 acquisition primitive. It drives a
:class:`~alicatlib.manager.AlicatManager` (or any
:class:`PollSource`-shaped object — see below) at an absolute-target
cadence and publishes the polled :class:`DataFrame` values into an
:class:`anyio.abc.ObjectReceiveStream` as per-tick
``Mapping[name, Sample]`` batches.

Key invariants (design §5.14):

- **Absolute-target scheduling.** Target times are computed from
  :func:`anyio.current_time` at ``record()``-entry, not from a running
  monotonic; drift across cycles is bounded by one tick and never
  accumulates. ``anyio.sleep_until`` advances to the next target
  slot; overruns skip missed slots and increment ``samples_late``.
- **Structured concurrency.** The producer task lives inside a
  ``create_task_group()`` strictly nested *inside* the async CM body.
  The CM yields the receive stream, user code iterates it, and on
  CM exit the task group is cancelled and joined before the CM
  returns. This matches AnyIO's own warning against yielding from
  inside a task group.
- **Wall-clock provenance.** ``datetime.now(UTC)`` is captured at the
  send/receive boundaries of each device's poll and attached to the
  emitted :class:`Sample` — used for sink timestamps, never for
  scheduling.
- **Backpressure.** ``buffer_size`` sets the memory-object stream
  capacity; :class:`OverflowPolicy` controls what happens when the
  producer wants to enqueue but the consumer is behind.

The recorder consumes a :class:`PollSource` — a narrow Protocol the
:class:`~alicatlib.manager.AlicatManager` already satisfies (its
``poll(names)`` signature matches). Kept as a Protocol so the
recorder is unit-testable against a lightweight stub without standing
up a full manager + transport pipeline.

Design reference: ``docs/design.md`` §5.14.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from time import monotonic_ns
from typing import TYPE_CHECKING, Protocol

import anyio

from alicatlib._logging import get_logger
from alicatlib.streaming.sample import Sample

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    from anyio.streams.memory import MemoryObjectSendStream

    from alicatlib.devices.data_frame import DataFrame
    from alicatlib.manager import DeviceResult

__all__ = [
    "AcquisitionSummary",
    "OverflowPolicy",
    "PollSource",
    "record",
]


_logger = get_logger("streaming")


class OverflowPolicy(Enum):
    """What ``record()`` does when the receive-stream buffer is full.

    The producer runs on an absolute-target schedule; the consumer
    drains at its own pace. Slow consumers create backpressure — this
    knob picks how the recorder responds.
    """

    BLOCK = "block"
    """Await the slow consumer. Default. Silent drops are surprising
    in a data-acquisition setting, so the recorder blocks the producer
    rather than quietly discarding samples. The effective sample rate
    drops to the consumer's drain rate; ``samples_late`` accrues once
    the consumer catches up and the producer can check its schedule."""

    DROP_NEWEST = "drop_newest"
    """Drop the sample that was about to be enqueued. Counted as late."""

    DROP_OLDEST = "drop_oldest"
    """Evict the oldest queued batch, then enqueue. Counted as late."""


@dataclass(frozen=True, slots=True)
class AcquisitionSummary:
    """Per-run summary emitted after ``record()``'s CM exits.

    Attributes:
        started_at: Wall-clock at the first scheduled tick.
        finished_at: Wall-clock at producer shutdown.
        samples_emitted: Count of per-tick batches actually pushed
            onto the receive stream. Partial batches (some devices
            errored under ``ErrorPolicy.RETURN``) still count as one
            emitted batch.
        samples_late: Count of ticks that missed their target slot
            (producer overran the previous tick, or overflow policy
            dropped the batch).
        max_drift_ms: Largest observed positive drift of an emitted
            batch relative to its absolute target, in milliseconds.
            A healthy run stays well under one period; values
            approaching ``1000 / rate_hz`` indicate the device or
            consumer is saturating the schedule.
    """

    started_at: datetime
    finished_at: datetime
    samples_emitted: int
    samples_late: int
    max_drift_ms: float


class PollSource(Protocol):
    """Minimal shape the recorder needs from its dispatcher.

    :class:`~alicatlib.manager.AlicatManager` satisfies this: its
    ``poll(names)`` returns a ``Mapping[str, DeviceResult[DataFrame]]``.
    Using a Protocol keeps :func:`record` testable against a lightweight
    stub without pulling in the whole manager + transport stack.
    """

    async def poll(
        self,
        names: Sequence[str] | None = None,
    ) -> Mapping[str, DeviceResult[DataFrame]]:
        """Poll every named device (or all under management) concurrently.

        Must return a mapping keyed by the manager-assigned device name.
        Successful polls carry the :class:`DataFrame` as ``.value``;
        failed ones carry the :class:`~alicatlib.errors.AlicatError` as
        ``.error`` (per :class:`~alicatlib.manager.ErrorPolicy.RETURN`).
        """
        ...


@asynccontextmanager
async def record(
    source: PollSource,
    *,
    rate_hz: float,
    duration: float | None = None,
    names: Sequence[str] | None = None,
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
) -> AsyncGenerator[AsyncIterator[Mapping[str, Sample]]]:
    """Record polled samples into a receive stream at an absolute cadence.

    Usage::

        async with record(mgr, rate_hz=10, duration=60) as stream:
            async for batch in stream:
                process(batch)

    The CM yields an async iterator of per-tick sample batches. Each
    batch is a ``Mapping[name, Sample]`` — one entry per device that
    polled successfully on that tick. Devices whose :class:`DeviceResult`
    carries an error are omitted from that batch and logged at WARN.

    Args:
        source: Any :class:`PollSource` (typically an
            :class:`~alicatlib.manager.AlicatManager`).
        rate_hz: Target cadence. Absolute targets are computed
            ``target[n] = start + n * (1 / rate_hz)``. Must be > 0.
        duration: Total acquisition duration in seconds. ``None``
            means "until the caller exits the CM".
        names: Subset of device names to poll per tick. ``None`` polls
            everything the source manages.
        overflow: Backpressure policy when the receive-stream buffer
            is full. See :class:`OverflowPolicy`.
        buffer_size: Receive-stream capacity, in per-tick batches.
            ``64`` mirrors the design default.

    Yields:
        An async iterator of per-tick ``Mapping[device_name, Sample]``.

    Raises:
        ValueError: If ``rate_hz <= 0`` or ``duration <= 0`` or
            ``buffer_size < 1``.
    """
    if rate_hz <= 0:
        raise ValueError(f"rate_hz must be > 0, got {rate_hz!r}")
    if duration is not None and duration <= 0:
        raise ValueError(f"duration must be > 0 or None, got {duration!r}")
    if buffer_size < 1:
        raise ValueError(f"buffer_size must be >= 1, got {buffer_size!r}")
    if overflow is OverflowPolicy.DROP_OLDEST:
        # Fail at call site (not deep inside the producer task) so the
        # exception type doesn't come back wrapped in an ExceptionGroup.
        raise NotImplementedError(
            "OverflowPolicy.DROP_OLDEST is not yet implemented; use BLOCK "
            "or DROP_NEWEST for now (design §5.14).",
        )

    period = 1.0 / rate_hz
    total_ticks = None if duration is None else max(1, round(duration * rate_hz))

    send_stream, receive_stream = anyio.create_memory_object_stream[Mapping[str, Sample]](
        max_buffer_size=buffer_size,
    )
    stats = _RunStats()

    started_at = datetime.now(UTC)
    _logger.info(
        "recorder.start",
        extra={
            "rate_hz": rate_hz,
            "duration_s": duration,
            "overflow": overflow.value,
            "buffer_size": buffer_size,
            "names": list(names) if names is not None else None,
        },
    )

    async with anyio.create_task_group() as tg, receive_stream:
        tg.start_soon(
            _run_producer,
            source,
            send_stream,
            period,
            total_ticks,
            names,
            overflow,
            stats,
        )
        try:
            yield receive_stream
        finally:
            # Cancel + drain before the CM returns — producer lifetime
            # is strictly nested inside the ``async with`` per §5.14.
            tg.cancel_scope.cancel()

    finished_at = datetime.now(UTC)
    summary = AcquisitionSummary(
        started_at=started_at,
        finished_at=finished_at,
        samples_emitted=stats.emitted,
        samples_late=stats.late,
        max_drift_ms=stats.max_drift_ms,
    )
    _logger.info(
        "recorder.stop",
        extra={
            "samples_emitted": summary.samples_emitted,
            "samples_late": summary.samples_late,
            "max_drift_ms": summary.max_drift_ms,
            "duration_s": (finished_at - started_at).total_seconds(),
        },
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RunStats:
    """Producer-side counters surfaced via :class:`AcquisitionSummary`."""

    emitted: int = 0
    late: int = 0
    max_drift_ms: float = 0.0


async def _run_producer(
    source: PollSource,
    send_stream: MemoryObjectSendStream[Mapping[str, Sample]],
    period: float,
    total_ticks: int | None,
    names: Sequence[str] | None,
    overflow: OverflowPolicy,
    stats: _RunStats,
) -> None:
    """Drive the absolute-cadence poll loop.

    Scheduling uses :func:`anyio.current_time` (AnyIO's internal
    monotonic) so ``anyio.sleep_until`` interprets targets against
    the same clock. Mixing :func:`time.monotonic` values in here
    would produce subtly wrong sleep durations (design §5.14).
    """
    start = anyio.current_time()
    tick = 0
    try:
        while total_ticks is None or tick < total_ticks:
            target = start + tick * period
            now = anyio.current_time()
            if now > target + period:
                # Overran by more than one full period — skip to the
                # next valid slot rather than trying to catch up.
                missed = int((now - target) / period)
                stats.late += missed
                tick += missed
                target = start + tick * period
            if anyio.current_time() < target:
                await anyio.sleep_until(target)

            requested_at = datetime.now(UTC)
            sent_ns = monotonic_ns()
            results = await source.poll(names)
            received_at = datetime.now(UTC)
            recv_ns = monotonic_ns()

            batch = _build_batch(results, requested_at, received_at, sent_ns, recv_ns)

            drift_s = anyio.current_time() - target
            drift_ms = drift_s * 1_000.0
            stats.max_drift_ms = max(stats.max_drift_ms, drift_ms)

            await _publish(send_stream, batch, overflow, stats)
            tick += 1
    finally:
        await send_stream.aclose()


def _build_batch(
    results: Mapping[str, DeviceResult[DataFrame]],
    requested_at: datetime,
    received_at: datetime,
    sent_ns: int,
    recv_ns: int,
) -> dict[str, Sample]:
    """Convert per-device results into a :class:`Sample` batch.

    Errored devices are dropped from the batch with a WARN log — the
    recorder guarantees every :class:`Sample` carries a :class:`DataFrame`.
    """
    midpoint = _midpoint(requested_at, received_at)
    latency_s = (received_at - requested_at).total_seconds()
    # Use the monotonic received-at as the Sample's scheduling clock;
    # averaging the ns boundary gives a cleaner per-device estimate
    # if we later plumb per-device timing into the manager.
    mono = (sent_ns + recv_ns) // 2
    batch: dict[str, Sample] = {}
    for name, result in results.items():
        if result.error is not None or result.value is None:
            _logger.warning(
                "recorder.device_error",
                extra={"device": name, "error": repr(result.error)},
            )
            continue
        frame = result.value
        batch[name] = Sample(
            device=name,
            unit_id=frame.unit_id,
            monotonic_ns=mono,
            requested_at=requested_at,
            received_at=received_at,
            midpoint_at=midpoint,
            latency_s=latency_s,
            frame=frame,
        )
    return batch


def _midpoint(requested_at: datetime, received_at: datetime) -> datetime:
    """Return the wall-clock midpoint of send/receive boundaries."""
    delta = received_at - requested_at
    return requested_at + delta / 2


async def _publish(
    send_stream: MemoryObjectSendStream[Mapping[str, Sample]],
    batch: Mapping[str, Sample],
    overflow: OverflowPolicy,
    stats: _RunStats,
) -> None:
    """Enqueue ``batch`` per the configured :class:`OverflowPolicy`.

    :attr:`OverflowPolicy.DROP_OLDEST` is filtered at :func:`record`'s
    entry (raises :class:`NotImplementedError` there), so the producer
    only ever sees BLOCK / DROP_NEWEST here.
    """
    if overflow is OverflowPolicy.BLOCK:
        await send_stream.send(batch)
        stats.emitted += 1
        return
    if overflow is OverflowPolicy.DROP_NEWEST:
        try:
            send_stream.send_nowait(batch)
        except anyio.WouldBlock:
            stats.late += 1
            _logger.warning(
                "recorder.drop_newest",
                extra={"policy": overflow.value, "reason": "consumer_backpressure"},
            )
            return
        stats.emitted += 1
        return
    raise AssertionError(f"unreachable overflow policy: {overflow!r}")
