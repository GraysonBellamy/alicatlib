"""Tests for :func:`alicatlib.units.to_pint`."""

from __future__ import annotations

import pytest

from alicatlib import Unit, to_pint


class TestUnitInput:
    @pytest.mark.parametrize(
        ("unit", "expected"),
        [
            (Unit.SCCM, "cm**3/min"),
            (Unit.SLPM, "L/min"),
            (Unit.KPA, "kPa"),
            (Unit.BAR, "bar"),
            (Unit.MBAR, "mbar"),
            (Unit.PA, "Pa"),
            (Unit.PSI, "psi"),
            (Unit.TORR, "torr"),
            (Unit.MTORR, "millitorr"),
            (Unit.DEG_C, "degC"),
            (Unit.DEG_F, "degF"),
            (Unit.DEG_K, "kelvin"),
            (Unit.S, "s"),
            (Unit.MS, "ms"),
            (Unit.HOUR, "hour"),
        ],
    )
    def test_typed_unit_maps_to_pint_string(self, unit: Unit, expected: str) -> None:
        assert to_pint(unit) == expected

    def test_none_returns_none(self) -> None:
        assert to_pint(None) is None

    def test_sentinel_units_map_to_none(self) -> None:
        assert to_pint(Unit.UNKNOWN) is None
        assert to_pint(Unit.DEFAULT) is None
        assert to_pint(Unit.COUNT) is None
        # h:m:s is a display format, not a pint quantity
        assert to_pint(Unit.H_M_S) is None


class TestStringInput:
    def test_exact_label_round_trips(self) -> None:
        assert to_pint("SLPM") == "L/min"
        assert to_pint("kPa") == "kPa"

    def test_case_insensitive_fallback(self) -> None:
        assert to_pint("slpm") == "L/min"
        assert to_pint("PSI") == "psi"

    def test_unknown_string_returns_none(self) -> None:
        assert to_pint("not_a_unit") is None
        assert to_pint("") is None

    def test_whitespace_stripped(self) -> None:
        assert to_pint(" SLPM ") == "L/min"


class TestLossyByDesign:
    """PSIA / PSIG / PSID all collapse to ``"psi"`` per spec §K."""

    def test_psi_psia_psig_psid_all_psi(self) -> None:
        # alicat's Unit enum lumps these under a single Unit.PSI; the
        # generator does not split absolute / gauge / differential into
        # separate enum members. The spec's "lossy by design" still
        # applies — any future split would still funnel into "psi".
        assert to_pint(Unit.PSI) == "psi"


class TestCoverage:
    """Every Unit member must be present in the mapping table.

    The mapping module asserts this at import time, so the import
    itself is the test; this case-walks the registry for belt-and-
    suspenders coverage in case the assert is ever relaxed.
    """

    def test_every_unit_member_is_mapped(self) -> None:
        from alicatlib.units import _ALICAT_UNIT_TO_PINT  # pyright: ignore[reportPrivateUsage]

        for member in Unit:
            assert member in _ALICAT_UNIT_TO_PINT, f"{member!r} missing from to_pint table"
