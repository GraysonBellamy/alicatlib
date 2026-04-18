"""Timed sample — one device reading with send/receive provenance.

A :class:`Sample` is what the recorder emits into its memory-object
stream. It pairs a :class:`DataFrame` (the measurement) with enough
timing to reconstruct the acquisition timeline after the fact:
``monotonic_ns`` for drift analysis, ``requested_at`` /
``received_at`` / ``midpoint_at`` for wall-clock provenance, and
``latency_s`` for quick per-sample latency checks.

The midpoint is the best point-estimate of the acquisition instant on
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

    from alicatlib.devices.data_frame import DataFrame

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
        monotonic_ns: :func:`time.monotonic_ns` at the read site. Used
            for scheduling / drift analysis only — never displayed,
            since the absolute value has no calendar meaning.
        requested_at: Wall-clock ``datetime`` (UTC) captured just
            before the poll bytes leave the host.
        received_at: Wall-clock ``datetime`` (UTC) captured just after
            the reply line is read.
        midpoint_at: ``(requested_at + received_at) / 2`` — the
            design-preferred point estimate of the sample instant. Use
            this when aligning Alicat samples against other sensor
            streams.
        latency_s: ``(received_at - requested_at).total_seconds()`` —
            precomputed for convenience; equivalent to
            ``received_at - requested_at`` but avoids the subtraction
            at every downstream call site.
        frame: The :class:`DataFrame` returned by the device's poll.
    """

    device: str
    unit_id: str
    monotonic_ns: int
    requested_at: datetime
    received_at: datetime
    midpoint_at: datetime
    latency_s: float
    frame: DataFrame
