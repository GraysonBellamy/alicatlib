"""Tests for ``alicatlib.firmware``."""

from __future__ import annotations

import pytest

from alicatlib.errors import AlicatParseError
from alicatlib.firmware import (
    NUMERIC_FAMILIES,
    FirmwareFamily,
    FirmwareVersion,
)


def _v(family: FirmwareFamily, major: int, minor: int, raw: str | None = None) -> FirmwareVersion:
    resolved_raw = raw if raw is not None else f"{major}v{minor:02d}"
    return FirmwareVersion(family=family, major=major, minor=minor, raw=resolved_raw)


class TestParse:
    @pytest.mark.parametrize(
        ("raw", "family", "major", "minor"),
        [
            ("10v05", FirmwareFamily.V10, 10, 5),
            ("10v5", FirmwareFamily.V10, 10, 5),
            ("10V05", FirmwareFamily.V10, 10, 5),
            ("10.05", FirmwareFamily.V10, 10, 5),
            ("9v01", FirmwareFamily.V8_V9, 9, 1),
            ("8v28", FirmwareFamily.V8_V9, 8, 28),
            ("1v00", FirmwareFamily.V1_V7, 1, 0),
            ("7v99", FirmwareFamily.V1_V7, 7, 99),
            ("100v99", FirmwareFamily.V10, 100, 99),
        ],
    )
    def test_numeric_forms(self, raw: str, family: FirmwareFamily, major: int, minor: int) -> None:
        parsed = FirmwareVersion.parse(raw)
        assert parsed.family is family
        assert parsed.major == major
        assert parsed.minor == minor
        assert parsed.raw == raw

    @pytest.mark.parametrize("raw", ["GP", "gp", "GP-10v05", "Alicat GP build 42"])
    def test_gp_forms_are_gp_family(self, raw: str) -> None:
        parsed = FirmwareVersion.parse(raw)
        assert parsed.family is FirmwareFamily.GP
        assert parsed.major == 0
        assert parsed.minor == 0
        assert parsed.raw == raw

    @pytest.mark.parametrize("raw", ["", "no-version", "10", "v05", "10vX"])
    def test_rejects_unparseable(self, raw: str) -> None:
        with pytest.raises(AlicatParseError):
            FirmwareVersion.parse(raw)

    def test_numeric_forms_with_same_major_minor_are_equal(self) -> None:
        assert FirmwareVersion.parse("10v05") == FirmwareVersion.parse("10v5")
        assert FirmwareVersion.parse("10v05") == FirmwareVersion.parse("10.05")


class TestOrdering:
    def test_ordering_within_family_is_structural(self) -> None:
        # Lexically "10v05" < "9v01"; numerically it is greater. Also across V8_V9 vs V10,
        # which *would* be a family mismatch — use same-family comparisons here.
        assert _v(FirmwareFamily.V10, 10, 5) > _v(FirmwareFamily.V10, 10, 4)
        assert _v(FirmwareFamily.V10, 10, 5) == _v(FirmwareFamily.V10, 10, 5)
        assert _v(FirmwareFamily.V1_V7, 1, 99) < _v(FirmwareFamily.V1_V7, 2, 0)

    def test_cross_family_ordering_raises(self) -> None:
        v10 = _v(FirmwareFamily.V10, 10, 5)
        v9 = _v(FirmwareFamily.V8_V9, 9, 1)
        gp = FirmwareVersion(family=FirmwareFamily.GP, major=0, minor=0, raw="GP")

        with pytest.raises(TypeError, match="across families"):
            _ = v10 < v9
        with pytest.raises(TypeError, match="across families"):
            _ = v9 >= v10
        with pytest.raises(TypeError, match="across families"):
            _ = gp < v10

    def test_cross_family_equality_is_false_not_raising(self) -> None:
        v10 = _v(FirmwareFamily.V10, 10, 5)
        gp = FirmwareVersion(family=FirmwareFamily.GP, major=0, minor=0, raw="GP")
        # Same numeric coords in different families still unequal.
        v10_as_if = FirmwareVersion(family=FirmwareFamily.V10, major=0, minor=0, raw="10v00")
        assert (v10 == gp) is False
        assert (gp == v10_as_if) is False

    def test_hashable_and_frozen(self) -> None:
        v = _v(FirmwareFamily.V10, 10, 5)
        assert hash(v) == hash(_v(FirmwareFamily.V10, 10, 5))
        with pytest.raises(AttributeError):
            v.major = 11  # type: ignore[misc]


class TestFamilies:
    def test_numeric_families_excludes_gp(self) -> None:
        assert FirmwareFamily.GP not in NUMERIC_FAMILIES
        assert FirmwareFamily.V1_V7 in NUMERIC_FAMILIES
        assert FirmwareFamily.V8_V9 in NUMERIC_FAMILIES
        assert FirmwareFamily.V10 in NUMERIC_FAMILIES


class TestStr:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            (_v(FirmwareFamily.V10, 10, 5), "10v05"),
            (_v(FirmwareFamily.V10, 10, 12), "10v12"),
            (_v(FirmwareFamily.V8_V9, 9, 0), "9v00"),
            (
                FirmwareVersion(family=FirmwareFamily.GP, major=0, minor=0, raw="GP"),
                "GP",
            ),
            (
                FirmwareVersion(family=FirmwareFamily.GP, major=0, minor=0, raw="GP-10v05"),
                "GP-10v05",
            ),
        ],
    )
    def test_str(self, version: FirmwareVersion, expected: str) -> None:
        assert str(version) == expected
