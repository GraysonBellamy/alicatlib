"""Firmware version parsing and family-aware ordering.

Alicat firmware evolves in four distinct *families* (per the Alicat Serial
Primer, p. 4): ``GP`` (oldest; no ``Nv`` number, requires ``$$`` prefix on every
command), ``1v``-``7v``, ``8v``-``9v``, and ``10v``. Cross-family ordering is
meaningless — "GP supports X" is a separate fact from "10v05 supports X" —
so this module models the family as a first-class enum and refuses to order
versions across families. Attempting ``FirmwareVersion(GP, 0, 0) < FirmwareVersion(V10, 10, 5)``
raises ``TypeError`` at the comparison site; gating code in
:class:`alicatlib.devices.session.Session` catches that and surfaces it as a
typed :class:`alicatlib.errors.AlicatFirmwareError` with
``reason="family_not_supported"``.

Design reference: ``docs/design.md`` §5.10.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Self

from alicatlib.errors import AlicatParseError


class FirmwareFamily(Enum):
    """Hardware/firmware lineage. See module docstring and design §5.10."""

    GP = "GP"
    V1_V7 = "1v-7v"
    V8_V9 = "8v-9v"
    V10 = "10v"


NUMERIC_FAMILIES: frozenset[FirmwareFamily] = frozenset(
    {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10}
)
"""The non-GP families — useful when gating commands that require any ``Nv``
firmware (i.e. anything that ships with a numeric version and no ``$$`` prefix)."""


_NUMERIC_RE = re.compile(r"(?P<major>\d+)\s*[vV.]\s*(?P<minor>\d+)")
# Match a standalone ``GP`` token OR a ``GP<version-suffix>`` run like
# ``GP07R100`` (observed on a GP07R100 MC-100SCCM-D, 2012 vintage —
# design §16.6.8). ``\b`` alone is insufficient: "GP07" has no word
# boundary between P and 0 since both are word characters. We match
# "GP" preceded by a boundary/start and followed by either a boundary
# OR a digit (the GP07R100-style version pattern).
_GP_PREFIX_RE = re.compile(r"(?:^|\b)GP(?=\b|\d)", re.IGNORECASE)


# Family boundaries per the Alicat Serial Primer (p. 4): 1v–7v is one board
# lineage, 8v–9v another, 10v the current lineage. Cross-lineage upgrades are
# not possible, which is why these families are modelled as discrete and not
# ordered across each other.
_V1_V7_MAX_MAJOR = 7
_V8_V9_MIN_MAJOR = 8
_V8_V9_MAX_MAJOR = 9
_V10_MIN_MAJOR = 10


def _family_for_major(major: int) -> FirmwareFamily:
    if 1 <= major <= _V1_V7_MAX_MAJOR:
        return FirmwareFamily.V1_V7
    if _V8_V9_MIN_MAJOR <= major <= _V8_V9_MAX_MAJOR:
        return FirmwareFamily.V8_V9
    if major >= _V10_MIN_MAJOR:
        return FirmwareFamily.V10
    raise AlicatParseError(
        f"Firmware major version {major} is not in any known family",
        field_name="major",
        expected=">=1",
        actual=major,
    )


@dataclass(frozen=True, slots=True)
class FirmwareVersion:
    """Family-scoped firmware version.

    Warning — ordering is intentionally family-gated. ``__lt__`` / ``__le__`` /
    ``__gt__`` / ``__ge__`` raise :class:`TypeError` when the operands have
    different families. ``__eq__`` returns ``False`` on family mismatch rather
    than raising (so sets and dict lookups stay well-behaved). This asymmetry
    is deliberate: silent cross-family comparison is the worse failure mode.

    Canonical gating pattern (see :class:`alicatlib.devices.session.Session`)::

        if cmd.firmware_families and fw.family not in cmd.firmware_families:
            raise AlicatFirmwareError(reason="family_not_supported", ...)
        if cmd.min_firmware and fw < cmd.min_firmware:       # safe: same family
            raise AlicatFirmwareError(reason="firmware_too_old", ...)

    Attributes:
        family: The firmware family (``GP`` / ``V1_V7`` / ``V8_V9`` / ``V10``).
        major: Numeric major; ``0`` for GP.
        minor: Numeric minor; ``0`` for GP.
        raw: The original string as reported by the device, preserved for
            diagnostics (e.g. ``"GP"``, ``"GP-10v05"``, ``"10v05"``).
    """

    family: FirmwareFamily
    major: int
    minor: int
    raw: str

    @classmethod
    def parse(cls, software: str) -> Self:
        """Parse ``software`` into a :class:`FirmwareVersion`.

        Accepts any of the historical shapes: ``"GP"``, ``"GP-10v05"``,
        ``"1v00"``, ``"7v99"``, ``"10v05"``, ``"10v5"``, ``"10.05"``, or those
        substrings embedded in a longer response.

        GP detection: if the string contains a standalone ``GP`` token, the
        family is :attr:`FirmwareFamily.GP`, regardless of any trailing
        ``Nv<major>v<minor>`` suffix. ``major`` / ``minor`` are ``0`` for GP
        (the Nv suffix, when present, is purely cosmetic on GP hardware).

        Args:
            software: Firmware string as reported by the device.

        Returns:
            The parsed version.

        Raises:
            AlicatParseError: If ``software`` contains neither a ``GP`` token
                nor a recognisable ``<major>v<minor>`` / ``<major>.<minor>`` pair.
        """
        is_gp = _GP_PREFIX_RE.search(software) is not None
        numeric_match = _NUMERIC_RE.search(software)

        if is_gp:
            return cls(family=FirmwareFamily.GP, major=0, minor=0, raw=software)

        if numeric_match is None:
            raise AlicatParseError(
                f"Could not parse firmware from {software!r}",
                field_name="software",
                expected="GP or <major>v<minor>",
                actual=software,
            )

        major = int(numeric_match.group("major"))
        minor = int(numeric_match.group("minor"))
        family = _family_for_major(major)
        return cls(family=family, major=major, minor=minor, raw=software)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FirmwareVersion):
            return NotImplemented
        # Family mismatch returns False rather than raising — keeps sets/dicts happy.
        if self.family is not other.family:
            return False
        return (self.major, self.minor) == (other.major, other.minor)

    def __hash__(self) -> int:
        return hash((self.family, self.major, self.minor))

    def _check_same_family(self, other: FirmwareVersion) -> None:
        if self.family is not other.family:
            raise TypeError(
                "cannot order firmware across families: "
                f"{self.family.value!r} vs {other.family.value!r} — "
                "check family membership before comparing"
            )

    def __lt__(self, other: FirmwareVersion) -> bool:
        self._check_same_family(other)
        return (self.major, self.minor) < (other.major, other.minor)

    def __le__(self, other: FirmwareVersion) -> bool:
        self._check_same_family(other)
        return (self.major, self.minor) <= (other.major, other.minor)

    def __gt__(self, other: FirmwareVersion) -> bool:
        self._check_same_family(other)
        return (self.major, self.minor) > (other.major, other.minor)

    def __ge__(self, other: FirmwareVersion) -> bool:
        self._check_same_family(other)
        return (self.major, self.minor) >= (other.major, other.minor)

    def __str__(self) -> str:
        if self.family is FirmwareFamily.GP:
            return self.raw if self.raw.upper().startswith("GP") else "GP"
        return f"{self.major}v{self.minor:02d}"
