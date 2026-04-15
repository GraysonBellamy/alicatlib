"""Tests for ``alicatlib.firmware``."""

from __future__ import annotations

import pytest

from alicatlib.errors import AlicatParseError
from alicatlib.firmware import FirmwareVersion


class TestParse:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("10v05", FirmwareVersion(10, 5)),
            ("10v5", FirmwareVersion(10, 5)),
            ("10V05", FirmwareVersion(10, 5)),
            ("10.05", FirmwareVersion(10, 5)),
            ("GP-10v05", FirmwareVersion(10, 5)),
            ("Alicat GP 10v05 build 42", FirmwareVersion(10, 5)),
            ("9v01", FirmwareVersion(9, 1)),
            ("100v99", FirmwareVersion(100, 99)),
        ],
    )
    def test_accepts_known_forms(self, raw: str, expected: FirmwareVersion) -> None:
        assert FirmwareVersion.parse(raw) == expected

    @pytest.mark.parametrize("raw", ["", "no-version", "10", "v05", "10vX"])
    def test_rejects_unparseable(self, raw: str) -> None:
        with pytest.raises(AlicatParseError):
            FirmwareVersion.parse(raw)

    def test_forms_normalize_equal(self) -> None:
        assert FirmwareVersion.parse("10v05") == FirmwareVersion.parse("10v5")
        assert FirmwareVersion.parse("10v05") == FirmwareVersion.parse("10.05")


class TestOrdering:
    def test_ordering_is_structural_not_lexical(self) -> None:
        # Lexically "10v05" < "9v01"; numerically it is greater.
        assert FirmwareVersion(10, 5) > FirmwareVersion(9, 1)
        assert FirmwareVersion(10, 5) > FirmwareVersion(10, 4)
        assert FirmwareVersion(10, 5) == FirmwareVersion(10, 5)
        assert FirmwareVersion(1, 99) < FirmwareVersion(2, 0)

    def test_hashable_and_frozen(self) -> None:
        v = FirmwareVersion(10, 5)
        assert hash(v) == hash(FirmwareVersion(10, 5))
        with pytest.raises(AttributeError):
            v.major = 11  # type: ignore[misc]


class TestStr:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            (FirmwareVersion(10, 5), "10v05"),
            (FirmwareVersion(10, 12), "10v12"),
            (FirmwareVersion(9, 0), "9v00"),
        ],
    )
    def test_str(self, version: FirmwareVersion, expected: str) -> None:
        assert str(version) == expected
