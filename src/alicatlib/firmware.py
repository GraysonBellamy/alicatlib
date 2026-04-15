"""Firmware version parsing and ordering.

Alicat reports firmware as strings like ``"10v05"``, ``"10v5"``,
``"GP-10v05"``, or (historically) ``"10.05"``. Comparing these as floats or as
raw strings is fragile: ``"10v05"`` sorts *before* ``"9v01"`` lexically. This
module normalises any of those forms into a structured, orderable dataclass.

Design reference: ``docs/design.md`` §5.10.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self

from alicatlib.errors import AlicatParseError

_FIRMWARE_RE = re.compile(r"(?P<major>\d+)\s*[vV.]\s*(?P<minor>\d+)")


@dataclass(frozen=True, order=True, slots=True)
class FirmwareVersion:
    """Structured firmware version, e.g. ``FirmwareVersion(10, 5)``.

    Parsing accepts any substring containing ``<digits> v <digits>`` (case
    insensitive) or ``<digits> . <digits>``. Prefixes like ``"GP-"`` are
    ignored.

    Ordering is by ``(major, minor)`` — ``FirmwareVersion(10, 5) >
    FirmwareVersion(9, 20)``.
    """

    major: int
    minor: int

    @classmethod
    def parse(cls, software: str) -> Self:
        """Parse ``software`` into a :class:`FirmwareVersion`.

        Args:
            software: Firmware string as reported by the device, e.g. ``"10v05"``.

        Returns:
            The parsed version.

        Raises:
            AlicatParseError: If ``software`` does not contain a recognisable
                ``major v minor`` or ``major.minor`` pair.
        """
        match = _FIRMWARE_RE.search(software)
        if match is None:
            raise AlicatParseError(
                f"Could not parse firmware from {software!r}",
                field_name="software",
                expected="<major>v<minor>",
                actual=software,
            )

        major = int(match.group("major"))
        minor = int(match.group("minor"))
        return cls(major=major, minor=minor)

    def __str__(self) -> str:
        return f"{self.major}v{self.minor:02d}"
