"""Tests for ``alicatlib.registry`` — generated enums and alias registries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from alicatlib.errors import UnknownGasError, UnknownStatisticError, UnknownUnitError
from alicatlib.registry import (
    Gas,
    Statistic,
    Unit,
    UnitCategory,
    gas_registry,
    statistic_registry,
    unit_registry,
)
from alicatlib.registry._codes_gen import (
    GAS_ALIASES,
    STATISTIC_ALIASES,
    UNIT_ALIASES,
)

CODES_JSON_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "alicatlib"
    / "registry"
    / "data"
    / "codes.json"
)


@pytest.fixture(scope="module")
def codes_data() -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(CODES_JSON_PATH.read_text(encoding="utf-8"))
    return parsed


# ---------------------------------------------------------------------------
# Round-trip invariants — generated data must be internally consistent.
# ---------------------------------------------------------------------------


class TestGasRoundTrip:
    def test_every_gas_roundtrips_through_code(self) -> None:
        for gas in Gas:
            assert gas_registry.by_code(gas.code) is gas

    def test_every_code_is_unique(self) -> None:
        codes = [g.code for g in Gas]
        assert len(codes) == len(set(codes))

    def test_every_value_is_unique(self) -> None:
        values = [g.value for g in Gas]
        assert len(values) == len(set(values))

    def test_known_gas_codes_match_primer(self) -> None:
        # Spot-check against Primer Appendix C.
        assert Gas.AIR.code == 0
        assert Gas.AR.code == 1
        assert Gas.N2.code == 8
        assert Gas.CO2.code == 4
        assert Gas.NC4H10.code == 13
        assert Gas.R_507A.code == 117
        assert Gas.HENE_9.code == 183
        assert Gas.D_2.code == 210
        assert Gas.MIX_255.code == 255

    def test_known_long_names_match_primer(self) -> None:
        assert Gas.AIR.display_name == "Air (Clean Dry)"
        assert Gas.NC4H10.display_name == "Normal Butane"
        assert Gas.DME.display_name == "Dimethylether (C2H6O)"
        assert Gas.H2S.display_name == "Hydrogen Sulfide"

    def test_temp_ext_vfr_min_added(self) -> None:
        """Primer A-3 lists code 294 (Temperature, external VFR minimum)."""
        stat = statistic_registry.by_code(294)
        assert stat is Statistic.TEMP_EXT_VFR_MIN
        assert "minimum" in stat.display_name


class TestStatisticRoundTrip:
    def test_every_statistic_roundtrips_through_code(self) -> None:
        for stat in Statistic:
            assert statistic_registry.by_code(stat.code) is stat

    def test_every_code_is_unique(self) -> None:
        codes = [s.code for s in Statistic]
        assert len(codes) == len(set(codes))

    def test_every_value_is_unique(self) -> None:
        values = [s.value for s in Statistic]
        assert len(values) == len(set(values))


class TestUnitRoundTrip:
    def test_every_unit_has_at_least_one_category(self) -> None:
        for unit in Unit:
            assert unit.categories, f"{unit} has no categories"

    def test_category_code_roundtrip(self) -> None:
        for unit in Unit:
            for cat in unit.categories:
                assert unit_registry.by_code(unit.code, category=cat) is unit

    def test_shared_code_seven_differs_by_category(self) -> None:
        """Code 7 maps to different Units depending on Appendix B section."""
        assert unit_registry.by_code(7, category=UnitCategory.STD_NORM_FLOW) is Unit.SLPM
        assert unit_registry.by_code(7, category=UnitCategory.PRESSURE) is Unit.BAR
        assert unit_registry.by_code(7, category=UnitCategory.TOTAL_STD_NORM_VOLUME) is Unit.SM3

    def test_default_unit_belongs_to_every_applicable_category(self) -> None:
        """Primer B-1/3/4/5/6/7/8 all list code 0 = default; B-2 does not."""
        default_cats = Unit.DEFAULT.categories
        assert UnitCategory.TRUE_MASS_FLOW not in default_cats
        assert UnitCategory.STD_NORM_FLOW in default_cats
        assert UnitCategory.PRESSURE in default_cats
        assert UnitCategory.TEMPERATURE in default_cats

    def test_every_category_code_pair_is_unique(self) -> None:
        seen: set[tuple[UnitCategory, int]] = set()
        for unit in Unit:
            for cat in unit.categories:
                key = (cat, unit.code)
                assert key not in seen, f"duplicate ({cat}, {unit.code}) for {unit}"
                seen.add(key)


# ---------------------------------------------------------------------------
# Coerce behavior — the user-facing entry point.
# ---------------------------------------------------------------------------


class TestCoerce:
    def test_coerce_enum_is_identity(self) -> None:
        assert gas_registry.coerce(Gas.N2) is Gas.N2

    def test_coerce_canonical_value(self) -> None:
        assert gas_registry.coerce("N2") is Gas.N2

    def test_coerce_alias(self) -> None:
        assert gas_registry.coerce("Nitrogen") is Gas.N2
        assert gas_registry.coerce("Argon") is Gas.AR

    def test_coerce_case_insensitive_alias(self) -> None:
        assert gas_registry.coerce("nitrogen") is Gas.N2
        assert gas_registry.coerce("ARGON") is Gas.AR

    def test_coerce_case_insensitive_value(self) -> None:
        assert gas_registry.coerce("n2") is Gas.N2

    def test_coerce_unknown_raises_with_suggestion(self) -> None:
        with pytest.raises(UnknownGasError) as ei:
            gas_registry.coerce("Nitrgen")
        assert "Nitrogen" in ei.value.suggestions

    def test_coerce_nonstring_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            gas_registry.coerce(42)  # type: ignore[arg-type]

    def test_statistic_coerce_accepts_legacy_alias(self) -> None:
        assert statistic_registry.coerce("Mass_Flow") is Statistic.MASS_FLOW
        assert statistic_registry.coerce("mass_flow") is Statistic.MASS_FLOW

    def test_unit_coerce_accepts_aliases(self) -> None:
        assert unit_registry.coerce("std_liter_per_min") is Unit.SLPM
        assert unit_registry.coerce("Celsius") is Unit.DEG_C
        assert unit_registry.coerce("pascal") is Unit.PA


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


class TestErrors:
    def test_unknown_gas_code_raises(self) -> None:
        with pytest.raises(UnknownGasError):
            gas_registry.by_code(9999)

    def test_unknown_statistic_code_raises(self) -> None:
        with pytest.raises(UnknownStatisticError):
            statistic_registry.by_code(9999)

    def test_unknown_unit_code_raises(self) -> None:
        with pytest.raises(UnknownUnitError):
            unit_registry.by_code(999, category=UnitCategory.PRESSURE)


# ---------------------------------------------------------------------------
# Alias integrity — cross-check against codes.json source of truth.
# ---------------------------------------------------------------------------


class TestAliasIntegrity:
    def test_no_duplicate_gas_aliases(self, codes_data: dict[str, Any]) -> None:
        seen: dict[str, int] = {}
        for entry in codes_data["gases"]:
            for alias in entry["aliases"]:
                assert alias not in seen or seen[alias] == entry["code"], (
                    f"gas alias {alias!r} owned by both {seen[alias]} and {entry['code']}"
                )
                seen[alias] = entry["code"]

    def test_every_alias_resolves(self) -> None:
        for alias, gas in GAS_ALIASES.items():
            assert gas_registry.coerce(alias) is gas
        for alias, stat in STATISTIC_ALIASES.items():
            assert statistic_registry.coerce(alias) is stat
        for alias, unit in UNIT_ALIASES.items():
            assert unit_registry.coerce(alias) is unit


# ---------------------------------------------------------------------------
# Generator idempotency — the committed _codes_gen.py must be current.
# ---------------------------------------------------------------------------


class TestGeneratorIdempotency:
    def test_gen_codes_check_passes(self) -> None:
        """If this fails, run ``python scripts/gen_codes.py`` and commit the result."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "scripts/gen_codes.py", "--check"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent.parent,
            check=False,
        )
        assert result.returncode == 0, (
            f"gen_codes.py --check failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
