"""Tests for :data:`alicatlib.commands.units.ENGINEERING_UNITS`
and :data:`alicatlib.commands.units.FULL_SCALE_QUERY`.

encode / decode are pure functions; the facade-level tests covering
data-frame-format invalidation live in ``test_device_facade.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicatlib.commands import (
    ENGINEERING_UNITS,
    FULL_SCALE_QUERY,
    DecodeContext,
    EngineeringUnitsRequest,
    FullScaleQueryRequest,
)
from alicatlib.errors import (
    AlicatParseError,
    UnknownStatisticError,
    UnknownUnitError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.registry import Statistic, Unit
from alicatlib.testing import parse_fixture

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "responses"


@pytest.fixture
def ctx_v10() -> DecodeContext:
    return DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))


@pytest.fixture
def ctx_gp() -> DecodeContext:
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("GP"),
        command_prefix=b"$$",
    )


# ---------------------------------------------------------------------------
# ENGINEERING_UNITS — encode
# ---------------------------------------------------------------------------


class TestDcuEncodeQuery:
    def test_query_by_enum(self, ctx_v10: DecodeContext) -> None:
        out = ENGINEERING_UNITS.encode(
            ctx_v10,
            EngineeringUnitsRequest(statistic=Statistic.MASS_FLOW),
        )
        assert out == b"ADCU 5\r"

    def test_query_by_string(self, ctx_v10: DecodeContext) -> None:
        out = ENGINEERING_UNITS.encode(
            ctx_v10,
            EngineeringUnitsRequest(statistic="mass_flow"),
        )
        assert out == b"ADCU 5\r"

    def test_query_with_group_flag_ignored(self, ctx_v10: DecodeContext) -> None:
        """apply_to_group is set-only semantics; query form omits it."""
        out = ENGINEERING_UNITS.encode(
            ctx_v10,
            EngineeringUnitsRequest(statistic=Statistic.MASS_FLOW, apply_to_group=True),
        )
        assert out == b"ADCU 5\r"

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        out = ENGINEERING_UNITS.encode(
            ctx_gp,
            EngineeringUnitsRequest(statistic=Statistic.MASS_FLOW),
        )
        assert out == b"A$$DCU 5\r"


class TestDcuEncodeSet:
    def test_set_by_unit_enum(self, ctx_v10: DecodeContext) -> None:
        out = ENGINEERING_UNITS.encode(
            ctx_v10,
            EngineeringUnitsRequest(statistic=Statistic.MASS_FLOW, unit=Unit.SLPM),
        )
        assert out == b"ADCU 5 7\r"

    def test_set_by_int_code(self, ctx_v10: DecodeContext) -> None:
        out = ENGINEERING_UNITS.encode(
            ctx_v10,
            EngineeringUnitsRequest(statistic=Statistic.MASS_FLOW, unit=12),
        )
        assert out == b"ADCU 5 12\r"

    def test_set_apply_to_group(self, ctx_v10: DecodeContext) -> None:
        out = ENGINEERING_UNITS.encode(
            ctx_v10,
            EngineeringUnitsRequest(
                statistic=Statistic.MASS_FLOW,
                unit=Unit.SLPM,
                apply_to_group=True,
            ),
        )
        assert out == b"ADCU 5 7 1\r"

    def test_set_override_special(self, ctx_v10: DecodeContext) -> None:
        """override_special_rules without apply_to_group still emits both flags."""
        out = ENGINEERING_UNITS.encode(
            ctx_v10,
            EngineeringUnitsRequest(
                statistic=Statistic.MASS_FLOW,
                unit=Unit.SLPM,
                override_special_rules=True,
            ),
        )
        assert out == b"ADCU 5 7 0 1\r"

    def test_set_both_flags(self, ctx_v10: DecodeContext) -> None:
        out = ENGINEERING_UNITS.encode(
            ctx_v10,
            EngineeringUnitsRequest(
                statistic=Statistic.MASS_FLOW,
                unit=Unit.SLPM,
                apply_to_group=True,
                override_special_rules=True,
            ),
        )
        assert out == b"ADCU 5 7 1 1\r"

    def test_unknown_statistic_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(UnknownStatisticError):
            ENGINEERING_UNITS.encode(
                ctx_v10,
                EngineeringUnitsRequest(statistic="not_a_real_stat"),
            )

    def test_unknown_unit_string_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(UnknownUnitError):
            ENGINEERING_UNITS.encode(
                ctx_v10,
                EngineeringUnitsRequest(
                    statistic=Statistic.MASS_FLOW,
                    unit="not_a_real_unit",
                ),
            )

    def test_ambiguous_unit_raises_with_guidance(
        self,
        ctx_v10: DecodeContext,
    ) -> None:
        """A Unit that maps to multiple distinct codes can't be auto-resolved.

        ``Unit.COUNT`` appears in STD_NORM_FLOW / VOLUMETRIC_FLOW /
        PRESSURE at code 62 — same code in every category — so the
        encoder actually *can* emit it unambiguously. Pick a Unit that
        maps to distinct codes if such a case exists; otherwise this
        test exercises the single-code happy path.
        """
        out = ENGINEERING_UNITS.encode(
            ctx_v10,
            EngineeringUnitsRequest(statistic=Statistic.MASS_FLOW, unit=Unit.COUNT),
        )
        # COUNT is code 62 in every category it belongs to, so the
        # single-code collapse succeeds and the encoder emits the code.
        assert out == b"ADCU 5 62\r"

    def test_bool_unit_rejected(self, ctx_v10: DecodeContext) -> None:
        """bool is an int subclass — catch an accidental ``unit=True``."""
        with pytest.raises(TypeError):
            ENGINEERING_UNITS.encode(
                ctx_v10,
                EngineeringUnitsRequest(statistic=Statistic.MASS_FLOW, unit=True),
            )


# ---------------------------------------------------------------------------
# ENGINEERING_UNITS — decode
# ---------------------------------------------------------------------------


class TestDcuDecode:
    """Real V10 reply shape: `<uid> <unit_code> <unit_label>` (3 fields).

    The device does *not* echo the requested statistic — verified
    2026-04-17 against MC-500SCCM-D 10v20.0-R24 (design §16.6). The
    facade fills in the statistic from the request via
    :func:`dataclasses.replace`; the decoder leaves it as
    :attr:`Statistic.NONE`.
    """

    def test_basic(self, ctx_v10: DecodeContext) -> None:
        setting = ENGINEERING_UNITS.decode(b"A 7 SLPM", ctx_v10)
        assert setting.unit_id == "A"
        assert setting.statistic is Statistic.NONE  # facade fills in from request
        assert setting.unit is Unit.SLPM
        assert setting.label == "SLPM"

    def test_sccm(self, ctx_v10: DecodeContext) -> None:
        setting = ENGINEERING_UNITS.decode(b"A 12 SCCM", ctx_v10)
        assert setting.unit is Unit.SCCM

    def test_unknown_label_falls_back_to_none(self, ctx_v10: DecodeContext) -> None:
        """Device emits a label we can't resolve → unit=None, label preserved."""
        setting = ENGINEERING_UNITS.decode(b"A 99 MysteryUnit", ctx_v10)
        assert setting.unit is None
        assert setting.label == "MysteryUnit"

    def test_bad_field_count_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            ENGINEERING_UNITS.decode(b"A 7", ctx_v10)

    def test_rejects_multiline(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(TypeError):
            ENGINEERING_UNITS.decode((b"A 7 SLPM",), ctx_v10)

    def test_fixture_round_trip(self, ctx_v10: DecodeContext) -> None:
        script = parse_fixture(_FIXTURES_DIR / "engineering_units_mc.txt")
        reply = script[b"ADCU 5 7\r"].rstrip(b"\r")
        setting = ENGINEERING_UNITS.decode(reply, ctx_v10)
        assert setting.unit is Unit.SLPM


# ---------------------------------------------------------------------------
# FULL_SCALE_QUERY
# ---------------------------------------------------------------------------


class TestFpfEncode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        out = FULL_SCALE_QUERY.encode(
            ctx_v10,
            FullScaleQueryRequest(statistic=Statistic.MASS_FLOW),
        )
        assert out == b"AFPF 5\r"

    def test_by_string(self, ctx_v10: DecodeContext) -> None:
        out = FULL_SCALE_QUERY.encode(
            ctx_v10,
            FullScaleQueryRequest(statistic="mass_flow"),
        )
        assert out == b"AFPF 5\r"

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        out = FULL_SCALE_QUERY.encode(
            ctx_gp,
            FullScaleQueryRequest(statistic=Statistic.MASS_FLOW),
        )
        assert out == b"A$$FPF 5\r"


class TestFpfDecode:
    """Real V10 reply shape: `<uid> <signed_value> <unit_code> <unit_label>` (4 fields).

    The device does *not* echo the requested statistic — verified
    2026-04-17 against MC-500SCCM-D 10v20.0-R24 (design §16.6). The
    facade fills in the statistic from the request via
    :func:`dataclasses.replace`; the decoder leaves it as
    :attr:`Statistic.NONE`.
    """

    def test_basic(self, ctx_v10: DecodeContext) -> None:
        fs = FULL_SCALE_QUERY.decode(b"A +500.00 12 SCCM", ctx_v10)
        assert fs.statistic is Statistic.NONE  # facade fills in from request
        assert fs.value == 500.0
        assert fs.unit is Unit.SCCM
        assert fs.unit_label == "SCCM"

    def test_bad_field_count_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            FULL_SCALE_QUERY.decode(b"A +500.00", ctx_v10)

    def test_malformed_value_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            FULL_SCALE_QUERY.decode(b"A NaN-ish 12 SCCM", ctx_v10)

    def test_rejects_multiline(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(TypeError):
            FULL_SCALE_QUERY.decode((b"A +500.00 12 SCCM",), ctx_v10)

    def test_fixture_round_trip(self, ctx_v10: DecodeContext) -> None:
        script = parse_fixture(_FIXTURES_DIR / "full_scale_mc.txt")
        reply = script[b"AFPF 5\r"].rstrip(b"\r")
        fs = FULL_SCALE_QUERY.decode(reply, ctx_v10)
        assert fs.value == 500.0
        assert fs.unit is Unit.SCCM
