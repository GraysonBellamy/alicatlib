"""Tests for :mod:`alicatlib.commands.valve`.

Covers ``HP``, ``HC``, ``C``, ``VD``: encode shapes (query-only
commands have no set form), destructive-confirm gate on ``HC``,
parse paths (post-op data frames for HP/HC/C; 2–4 field reply for
VD), and firmware-family gating.
"""

from __future__ import annotations

import pytest

from alicatlib.commands import (
    CANCEL_VALVE_HOLD,
    HOLD_VALVES,
    HOLD_VALVES_CLOSED,
    VALVE_DRIVE,
    CancelValveHoldRequest,
    DecodeContext,
    HoldValvesClosedRequest,
    HoldValvesRequest,
    ValveDriveRequest,
)
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.errors import AlicatParseError
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import Statistic


def _format() -> DataFrameFormat:
    def _text(v: str) -> float | str | None:
        return v

    def _decimal(v: str) -> float | str | None:
        return parse_optional_float(v, field="decimal")

    return DataFrameFormat(
        fields=(
            DataFrameField(
                name="Unit_ID",
                raw_name="Unit_ID",
                type_name="text",
                statistic=Statistic.NONE,
                unit=None,
                conditional=False,
                parser=_text,
            ),
            DataFrameField(
                name="Mass_Flow",
                raw_name="Mass_Flow",
                type_name="decimal",
                statistic=Statistic.MASS_FLOW,
                unit=None,
                conditional=False,
                parser=_decimal,
            ),
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


@pytest.fixture
def ctx_v10_with_format() -> DecodeContext:
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("10v05"),
        data_frame_format=_format(),
    )


@pytest.fixture
def ctx_v10_no_format() -> DecodeContext:
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("10v05"),
    )


@pytest.fixture
def ctx_gp() -> DecodeContext:
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("10v05"),
        command_prefix=b"$$",
    )


class TestHoldValvesEncode:
    def test_basic(self, ctx_v10_no_format: DecodeContext) -> None:
        assert HOLD_VALVES.encode(ctx_v10_no_format, HoldValvesRequest()) == b"AHP\r"

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        assert HOLD_VALVES.encode(ctx_gp, HoldValvesRequest()) == b"A$$HP\r"


class TestHoldValvesClosedEncode:
    def test_basic_requires_confirm_at_session_layer(
        self, ctx_v10_no_format: DecodeContext
    ) -> None:
        """Encoder emits bytes regardless — destructive gate lives on Session."""
        assert (
            HOLD_VALVES_CLOSED.encode(ctx_v10_no_format, HoldValvesClosedRequest(confirm=True))
            == b"AHC\r"
        )

    def test_command_is_destructive(self) -> None:
        """Spec-level flag pins the session gate."""
        assert HOLD_VALVES_CLOSED.destructive is True


class TestCancelValveHoldEncode:
    def test_basic(self, ctx_v10_no_format: DecodeContext) -> None:
        assert CANCEL_VALVE_HOLD.encode(ctx_v10_no_format, CancelValveHoldRequest()) == b"AC\r"


class TestValveDriveEncode:
    def test_basic(self, ctx_v10_no_format: DecodeContext) -> None:
        assert VALVE_DRIVE.encode(ctx_v10_no_format, ValveDriveRequest()) == b"AVD\r"

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        assert VALVE_DRIVE.encode(ctx_gp, ValveDriveRequest()) == b"A$$VD\r"


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------


class TestHoldFrameDecode:
    """HP / HC / C share the post-op data-frame decoder."""

    def test_hp_parses_frame(self, ctx_v10_with_format: DecodeContext) -> None:
        parsed = HOLD_VALVES.decode(b"A 50.0", ctx_v10_with_format)
        assert parsed.unit_id == "A"
        assert parsed.values["Mass_Flow"] == 50.0

    def test_c_parses_frame(self, ctx_v10_with_format: DecodeContext) -> None:
        parsed = CANCEL_VALVE_HOLD.decode(b"A 25.0", ctx_v10_with_format)
        assert parsed.unit_id == "A"

    def test_missing_format_raises(self, ctx_v10_no_format: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            HOLD_VALVES.decode(b"A 50.0", ctx_v10_no_format)


class TestValveDriveDecode:
    def test_single_valve(self, ctx_v10_no_format: DecodeContext) -> None:
        state = VALVE_DRIVE.decode(b"A 45.5", ctx_v10_no_format)
        assert state.unit_id == "A"
        assert state.valves == (45.5,)

    def test_dual_valve(self, ctx_v10_no_format: DecodeContext) -> None:
        state = VALVE_DRIVE.decode(b"A 60.0 40.0", ctx_v10_no_format)
        assert state.valves == (60.0, 40.0)

    def test_triple_valve(self, ctx_v10_no_format: DecodeContext) -> None:
        state = VALVE_DRIVE.decode(b"A 60.0 40.0 10.0", ctx_v10_no_format)
        assert state.valves == (60.0, 40.0, 10.0)

    def test_quad_valve_real_hardware(self, ctx_v10_no_format: DecodeContext) -> None:
        """Real 10v20 firmware returns a fixed-width four-column reply.

        Observed on 2026-04-17 on MC-500SCCM-D @ 10v20.0-R24:
        single-valve controller returns ``A 100.00 000.00 000.00
        000.00\\r`` — four valve percentages regardless of physical
        valve count. Design §16.6 already flagged "VD returns four
        columns even on meter/no-valve cases" (medium confidence);
        This test promotes that observation to canonical behavior.
        """
        state = VALVE_DRIVE.decode(b"A 100.00 000.00 000.00 000.00", ctx_v10_no_format)
        assert state.unit_id == "A"
        assert state.valves == (100.0, 0.0, 0.0, 0.0)

    def test_too_few_fields_raises(self, ctx_v10_no_format: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            VALVE_DRIVE.decode(b"A", ctx_v10_no_format)

    def test_too_many_fields_raises(self, ctx_v10_no_format: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            VALVE_DRIVE.decode(b"A 1 2 3 4 5", ctx_v10_no_format)
