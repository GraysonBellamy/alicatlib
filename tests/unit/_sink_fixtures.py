"""Shared :class:`Sample` builders for sink unit tests.

Three sink test modules need to synthesise the same shape of
:class:`~alicatlib.streaming.sample.Sample`, so the factory lives here
rather than being copy-pasted four times (this file + the existing
``test_sinks.py`` factory, which we leave untouched to avoid churning
a green file).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alicatlib.devices.models import StatusCode
from alicatlib.devices.reading import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
    ParsedFrame,
    Reading,
)
from alicatlib.registry import Statistic
from alicatlib.streaming.sample import Sample


def _decimal(v: str) -> float:
    return float(v)


def frame_format(field_name: str = "Mass_Flow") -> DataFrameFormat:
    """Build a one-field DataFrameFormat keyed on ``MASS_FLOW``."""
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


def make_sample(
    *,
    device: str = "fuel",
    unit_id: str = "A",
    field_name: str = "Mass_Flow",
    value: float = 12.5,
    at: datetime | None = None,
) -> Sample:
    """Build one :class:`Sample` with deterministic timing."""
    when = at if at is not None else datetime.now(UTC)
    fmt = frame_format(field_name)
    parsed = ParsedFrame(
        unit_id=unit_id,
        values={field_name: value},
        values_by_statistic={Statistic.MASS_FLOW: value},
        status=frozenset[StatusCode](),
    )
    frame = Reading.from_parsed(
        parsed,
        reading_format=fmt,
        received_at=when,
        t_mono_ns=1000,
    )
    return Sample(
        device=device,
        unit_id=unit_id,
        t_mono_ns=1000,
        requested_at=when,
        received_at=when + timedelta(milliseconds=5),
        t_utc=when + timedelta(milliseconds=2),
        latency_s=0.005,
        reading=frame,
    )
