"""Sync wrappers for :func:`alicatlib.streaming.record` and :func:`alicatlib.sinks.pipe`.

:func:`record` — sync context manager wrapping the async recorder. The
produced iterator is blocking; on CM exit the underlying async task
group is cancelled and joined by the portal.

:func:`pipe` — sync drain loop matching :func:`alicatlib.sinks.pipe`'s
batch / time flush semantics. Rebuilt in sync-land rather than wrapping
the async driver so buffering stays under sync control and the time
threshold uses :func:`time.monotonic`, not :func:`anyio.current_time`.

Both entry points accept :class:`SyncAlicatManager` / :class:`SyncSinkAdapter`
instances — internally they reach for the wrapped async objects so the
recorder / sink plumbing runs on the shared portal.

Design reference: ``docs/design.md`` §5.14, §5.15, §5.16.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from alicatlib.streaming.recorder import (
    AcquisitionSummary,
    OverflowPolicy,
    PollSource,
)
from alicatlib.streaming.recorder import (
    record as async_record,
)
from alicatlib.sync.manager import SyncAlicatManager
from alicatlib.sync.portal import SyncAsyncIterator, SyncPortal
from alicatlib.sync.sinks import SyncSinkAdapter

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Mapping, Sequence

    from alicatlib.sinks.base import SampleSink
    from alicatlib.streaming.sample import Sample

__all__ = [
    "AcquisitionSummary",
    "OverflowPolicy",
    "pipe",
    "record",
]


def _resolve_poll_source(
    source: SyncAlicatManager | PollSource,
) -> PollSource:
    """Return the async :class:`PollSource` inside ``source``.

    The sync manager wraps an :class:`AlicatManager` which itself
    satisfies :class:`PollSource`. Pre-existing async sources pass
    through unchanged.
    """
    if isinstance(source, SyncAlicatManager):
        inner = source._mgr  # pyright: ignore[reportPrivateUsage]
        if inner is None:
            raise RuntimeError("SyncAlicatManager is not open")
        return inner
    return source


def _resolve_portal(
    explicit: SyncPortal | None,
    source: SyncAlicatManager | PollSource,
    sink: SyncSinkAdapter | SampleSink | None,
) -> SyncPortal | None:
    """Pick the portal that recording + sink I/O share.

    Preference order:

    1. ``explicit`` (caller-provided).
    2. ``source``'s portal, if it is a :class:`SyncAlicatManager`.
    3. ``sink``'s portal, if it is a :class:`SyncSinkAdapter`.
    4. ``None`` — caller gets a fresh per-call portal.
    """
    if explicit is not None:
        return explicit
    if isinstance(source, SyncAlicatManager):
        return source.portal
    if isinstance(sink, SyncSinkAdapter):
        try:
            return sink.portal
        except RuntimeError:
            return None
    return None


@contextmanager
def record(
    source: SyncAlicatManager | PollSource,
    *,
    rate_hz: float,
    duration: float | None = None,
    names: Sequence[str] | None = None,
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
    portal: SyncPortal | None = None,
) -> Generator[Iterator[Mapping[str, Sample]]]:
    """Sync :func:`alicatlib.streaming.record`.

    The yielded iterator is a blocking bridge to the recorder's
    receive stream. Breaking out of the loop or exiting the ``with``
    cancels the producer task.

    If ``source`` is a :class:`SyncAlicatManager`, its portal is
    reused — the recorder and manager must share an event loop.
    Pass ``portal=`` to override; pass a raw :class:`AlicatManager`
    and the recorder owns its own portal.
    """
    poll_source = _resolve_poll_source(source)
    with ExitStack() as stack:
        active_portal = _resolve_portal(portal, source, None) or stack.enter_context(SyncPortal())
        async_cm = async_record(
            poll_source,
            rate_hz=rate_hz,
            duration=duration,
            names=names,
            overflow=overflow,
            buffer_size=buffer_size,
        )
        async_stream = stack.enter_context(active_portal.wrap_async_context_manager(async_cm))
        sync_iter = stack.enter_context(active_portal.wrap_async_iter(async_stream))
        yield sync_iter


def pipe(
    stream: Iterator[Mapping[str, Sample]],
    sink: SyncSinkAdapter | SampleSink,
    *,
    batch_size: int = 64,
    flush_interval: float = 1.0,
    portal: SyncPortal | None = None,
) -> AcquisitionSummary:
    """Sync :func:`alicatlib.sinks.pipe`.

    Drains a sync iterator of per-tick batches into ``sink`` with the
    same buffered-flush semantics as the async driver: flush when the
    buffer reaches ``batch_size`` samples or ``flush_interval`` seconds
    have passed since the last flush.

    ``sink`` may be a :class:`SyncSinkAdapter` (already open) or a raw
    async :class:`SampleSink`. In the async case a ``portal`` must be
    reachable — either passed explicitly or derived from a
    :class:`SyncSinkAdapter` — so writes can be dispatched.

    Time thresholds use :func:`time.monotonic` (wall-clock-ish,
    independent of the portal's event loop) because the sink cares
    about persistence freshness, not scheduling precision.

    The returned :class:`AcquisitionSummary` carries ``samples_emitted``
    (count actually handed to the sink); the ``samples_late`` and
    ``max_drift_ms`` fields stay at zero — those are recorder-layer
    concepts and the recorder logs its own values via the
    ``alicatlib.streaming`` logger on CM exit.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")
    if flush_interval <= 0:
        raise ValueError(f"flush_interval must be > 0, got {flush_interval!r}")

    if isinstance(sink, SyncSinkAdapter):
        flush = sink.write_many
    else:
        resolved: SyncPortal | None = portal
        if resolved is None and isinstance(stream, SyncAsyncIterator):
            resolved = stream._portal  # pyright: ignore[reportPrivateUsage]
        if resolved is None:
            raise RuntimeError(
                "pipe: passing an async SampleSink requires a portal — "
                "wrap the sink in a SyncSinkAdapter or pass portal=.",
            )
        async_sink = sink
        active: SyncPortal = resolved

        def flush(samples: Sequence[Sample]) -> None:
            active.call(async_sink.write_many, samples)

    started_at = datetime.now(UTC)
    emitted = 0
    buffer: list[Sample] = []
    last_flush = time.monotonic()

    for batch in stream:
        buffer.extend(batch.values())
        now = time.monotonic()
        if len(buffer) >= batch_size or (now - last_flush) >= flush_interval:
            flush(buffer)
            emitted += len(buffer)
            buffer.clear()
            last_flush = now

    if buffer:
        flush(buffer)
        emitted += len(buffer)
        buffer.clear()

    finished_at = datetime.now(UTC)
    return AcquisitionSummary(
        started_at=started_at,
        finished_at=finished_at,
        samples_emitted=emitted,
        samples_late=0,
        max_drift_ms=0.0,
    )
