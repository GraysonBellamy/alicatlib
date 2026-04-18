"""Sink Protocol, sample → row helper, and the ``pipe()`` driver.

A :class:`SampleSink` is the minimal shape the recorder's downstream
consumer needs: :meth:`open`, :meth:`write_many`, :meth:`close`, and
the matching async context-manager methods. The in-tree sinks
(:class:`~alicatlib.sinks.memory.InMemorySink`,
:class:`~alicatlib.sinks.csv.CsvSink`,
:class:`~alicatlib.sinks.jsonl.JsonlSink`) all satisfy this Protocol;
third-party sinks (Parquet, Postgres, Kafka, …) can slot in without
touching library code.

:func:`pipe` is the v1 acquisition glue. It reads per-tick batches out
of the recorder's receive stream, buffers them up to ``batch_size``
(or ``flush_interval`` seconds, whichever comes first), and calls
``sink.write_many`` to flush. On stream exhaustion it drains any
remaining buffer and returns an :class:`AcquisitionSummary` with
``samples_emitted`` reflecting the count actually handed to the sink.

Design reference: ``docs/design.md`` §5.15.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import anyio

from alicatlib._logging import get_logger
from alicatlib.streaming.recorder import AcquisitionSummary

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence
    from types import TracebackType
    from typing import Self

    from alicatlib.streaming.sample import Sample

__all__ = [
    "SampleSink",
    "pipe",
    "sample_to_row",
]


_logger = get_logger("sinks")


class SampleSink(Protocol):
    """Minimal shape of an acquisition sink.

    Sinks own their storage handle lifecycle. Concrete implementations
    typically follow this call sequence:

    1. ``await sink.open()`` — allocate file descriptors, DB connections,
       etc. Safe to call again on an already-open sink.
    2. ``await sink.write_many(samples)`` — one or more times. ``samples``
       is a :class:`~collections.abc.Sequence` so the sink knows the full
       batch up front (useful for CSV column inference, Parquet row
       groups, Postgres parameterised inserts).
    3. ``await sink.close()`` — flush and release the handle. Idempotent.

    The async context-manager methods provide a ``async with sink:``
    shape for the common case of "open → write → close" in one block.
    """

    async def open(self) -> None:
        """Allocate the sink's backing resource (file handle, DB conn, …)."""
        ...

    async def write_many(self, samples: Sequence[Sample]) -> None:
        """Append ``samples`` to the sink.

        ``Sequence`` (not ``Iterable``) because every in-tree sink wants
        ``len()`` — CSV schema inference, batched parameterised inserts,
        Parquet row-group bookkeeping.
        """
        ...

    async def close(self) -> None:
        """Flush and release the backing resource. Idempotent."""
        ...

    async def __aenter__(self) -> Self:
        """Open the sink and return ``self`` for chaining."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the sink on exit."""
        ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def sample_to_row(sample: Sample) -> dict[str, float | str | int | None]:
    """Flatten a :class:`Sample` into a single row dict for tabular sinks.

    Schema layout (stable across samples):

    - ``device`` — manager-assigned name.
    - ``unit_id`` — bus-level single-letter id.
    - ``requested_at`` / ``received_at`` / ``midpoint_at`` — ISO 8601.
    - ``latency_s`` — poll round-trip, seconds.
    - *frame fields* — everything from :meth:`DataFrame.as_dict` *except*
      the frame's own ``received_at`` (superseded by the sample-level
      value so all rows have the same ``received_at`` semantics).
    - ``status`` — comma-joined sorted status codes (empty string when
      no flags active), from :meth:`DataFrame.as_dict`.

    The frame's own ``received_at`` is dropped so the row's ``received_at``
    consistently means "recorder-observed reply time" across rows —
    otherwise multi-device rows would mix frame-level and sample-level
    timings.
    """
    row: dict[str, float | str | int | None] = {
        "device": sample.device,
        "unit_id": sample.unit_id,
        "requested_at": sample.requested_at.isoformat(),
        "received_at": sample.received_at.isoformat(),
        "midpoint_at": sample.midpoint_at.isoformat(),
        "latency_s": sample.latency_s,
    }
    frame_dict = sample.frame.as_dict()
    frame_dict.pop("received_at", None)
    # The first ??D* field is the unit-id echo (design §5.6). It
    # duplicates ``sample.unit_id`` verbatim and collides case-
    # insensitively with the ``unit_id`` column in strict backends like
    # SQLite (hardware-validation finding, 2026-04-17: captured parser names
    # the field ``Unit_ID`` while the sample-level column is
    # ``unit_id`` — SQLite treats them as a duplicate column).
    for key in ("Unit_ID", "unit_id"):
        frame_dict.pop(key, None)
    row.update(frame_dict)
    return row


# ---------------------------------------------------------------------------
# pipe() driver
# ---------------------------------------------------------------------------


async def pipe(
    stream: AsyncIterator[Mapping[str, Sample]],
    sink: SampleSink,
    *,
    batch_size: int = 64,
    flush_interval: float = 1.0,
) -> AcquisitionSummary:
    r"""Drain ``stream`` into ``sink`` with buffered flushes.

    Reads per-tick batches from the recorder and accumulates the
    individual :class:`Sample`\ s into a list. A flush happens when
    either threshold is first crossed:

    - the buffer reaches ``batch_size`` samples, or
    - ``flush_interval`` seconds have elapsed since the last flush.

    On stream exhaustion any leftover buffer is flushed before the
    summary is returned.

    The ``samples_late`` / ``max_drift_ms`` fields on the returned
    summary stay at zero here — those are recorder-layer concepts.
    The recorder emits its own summary via the ``alicatlib.streaming``
    logger on CM exit; this summary is the sink-side view.

    Args:
        stream: The async iterator yielded by
            :func:`~alicatlib.streaming.record`.
        sink: Any :class:`SampleSink`. Must already be open.
        batch_size: Buffer threshold in samples (not batches). Defaults
            to ``64`` to match the design default.
        flush_interval: Time threshold in seconds between flushes.
            Wall-clock only, not anyio-clock — sinks care about
            persistence freshness, not scheduling precision.

    Returns:
        An :class:`AcquisitionSummary` with ``samples_emitted`` set to
        the count actually handed to the sink.

    Raises:
        ValueError: On non-positive ``batch_size`` or ``flush_interval``.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")
    if flush_interval <= 0:
        raise ValueError(f"flush_interval must be > 0, got {flush_interval!r}")

    started_at = datetime.now(UTC)
    emitted = 0
    buffer: list[Sample] = []
    last_flush = anyio.current_time()

    async def _flush() -> None:
        nonlocal emitted
        if not buffer:
            return
        await sink.write_many(buffer)
        emitted += len(buffer)
        buffer.clear()

    async for batch in stream:
        buffer.extend(batch.values())
        now = anyio.current_time()
        if len(buffer) >= batch_size or (now - last_flush) >= flush_interval:
            await _flush()
            last_flush = now

    await _flush()
    finished_at = datetime.now(UTC)
    _logger.info(
        "sinks.pipe_done",
        extra={
            "sink": type(sink).__name__,
            "samples_emitted": emitted,
            "duration_s": (finished_at - started_at).total_seconds(),
        },
    )
    return AcquisitionSummary(
        started_at=started_at,
        finished_at=finished_at,
        samples_emitted=emitted,
        samples_late=0,
        max_drift_ms=0.0,
    )
