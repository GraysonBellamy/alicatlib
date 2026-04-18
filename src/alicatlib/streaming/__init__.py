"""Sample acquisition — ``record()`` emits typed ``Sample`` streams.

Public surface:

- :class:`Sample` — one device poll with full timing provenance.
- :func:`record` — absolute-cadence async context manager.
- :class:`OverflowPolicy` — backpressure control knob.
- :class:`AcquisitionSummary` — per-run counters.
- :class:`PollSource` — Protocol the recorder accepts (satisfied by
  :class:`~alicatlib.manager.AlicatManager`).

See ``docs/design.md`` §5.14.
"""

from __future__ import annotations

from alicatlib.streaming.recorder import (
    AcquisitionSummary,
    OverflowPolicy,
    PollSource,
    record,
)
from alicatlib.streaming.sample import Sample

__all__ = [
    "AcquisitionSummary",
    "OverflowPolicy",
    "PollSource",
    "Sample",
    "record",
]
