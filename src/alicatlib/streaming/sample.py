"""Timed sample — one device reading with send/receive provenance.

A :class:`Sample` is what the recorder emits into its memory-object
stream. It pairs a :class:`Reading` (the measurement) with enough
timing to reconstruct the acquisition timeline after the fact:
``t_mono_ns`` and ``t_utc`` are the canonical join keys per the
cross-lib contract (§C), and ``requested_at`` / ``received_at`` /
``latency_s`` are the I/O-boundary provenance fields.

``t_utc`` is the best point-estimate of the acquisition instant on
the device: halfway between when the poll byte left the host and when
the full reply arrived. That's what downstream plots and correlations
should use when aligning Alicat data against other sensor streams.

Design reference: ``docs/design.md`` §5.14.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from alicatlib.devices.reading import Reading

__all__ = ["Sample"]


@dataclass(frozen=True, slots=True)
class Sample:
    """One device poll with full timing provenance.

    Attributes:
        device: The manager-assigned name (from ``AlicatManager.add``).
            Stable downstream identifier that follows the value into sinks.
        unit_id: Bus-level single-letter unit id of the polled device.
            Kept separate from ``device`` so a user renaming the
            manager key doesn't lose the physical addressing context.
        t_mono_ns: :func:`time.monotonic_ns` at the acquisition
            midpoint. Canonical join key per the cross-lib §C contract;
            never displayed, since the absolute value has no calendar
            meaning.
        t_utc: Wall-clock ``datetime`` (UTC, tz-aware) for the
            acquisition midpoint — ``(requested_at + received_at) / 2``.
            Use this when aligning Alicat samples against other sensor
            streams.
        t_midpoint_mono_ns: Optional monotonic-ns midpoint of an
            integration window. ``None`` for single polled samples (the
            common case); populated only when a sample summarises a
            multi-sample window.
        requested_at: Wall-clock ``datetime`` (UTC) captured just
            before the poll bytes leave the host. I/O-boundary
            provenance — keep alongside ``t_utc`` so callers can see
            the dispatch instant separately from the acquisition
            midpoint.
        received_at: Wall-clock ``datetime`` (UTC) captured just after
            the reply line is read. I/O-boundary provenance.
        latency_s: ``(received_at - requested_at).total_seconds()`` —
            precomputed for convenience; equivalent to
            ``received_at - requested_at`` but avoids the subtraction
            at every downstream call site.
        reading: The :class:`Reading` returned by the device's poll.
    """

    device: str
    unit_id: str
    t_mono_ns: int
    t_utc: datetime
    requested_at: datetime
    received_at: datetime
    latency_s: float
    reading: Reading
    t_midpoint_mono_ns: int | None = None
