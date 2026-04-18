"""Tests for :data:`alicatlib.commands.loop_control.LOOP_CONTROL_VARIABLE`."""

from __future__ import annotations

from pathlib import Path

import pytest

from alicatlib.commands import (
    LOOP_CONTROL_VARIABLE,
    DecodeContext,
    LoopControlVariableRequest,
)
from alicatlib.errors import AlicatParseError, AlicatValidationError
from alicatlib.firmware import FirmwareVersion
from alicatlib.registry import LoopControlVariable, Statistic
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


class TestEncode:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert LOOP_CONTROL_VARIABLE.encode(ctx_v10, LoopControlVariableRequest()) == b"ALV\r"

    def test_set_by_enum(self, ctx_v10: DecodeContext) -> None:
        assert (
            LOOP_CONTROL_VARIABLE.encode(
                ctx_v10,
                LoopControlVariableRequest(variable=LoopControlVariable.MASS_FLOW_SETPT),
            )
            == b"ALV 37\r"
        )

    def test_set_by_int_code(self, ctx_v10: DecodeContext) -> None:
        assert (
            LOOP_CONTROL_VARIABLE.encode(
                ctx_v10,
                LoopControlVariableRequest(variable=37),
            )
            == b"ALV 37\r"
        )

    def test_set_by_statistic(self, ctx_v10: DecodeContext) -> None:
        assert (
            LOOP_CONTROL_VARIABLE.encode(
                ctx_v10,
                LoopControlVariableRequest(variable=Statistic.MASS_FLOW_SETPT),
            )
            == b"ALV 37\r"
        )

    def test_set_by_name_string(self, ctx_v10: DecodeContext) -> None:
        assert (
            LOOP_CONTROL_VARIABLE.encode(
                ctx_v10,
                LoopControlVariableRequest(variable="mass_flow_setpt"),
            )
            == b"ALV 37\r"
        )

    def test_ineligible_statistic_raises(self, ctx_v10: DecodeContext) -> None:
        """A ``Statistic`` that isn't in the LV subset fails at encode."""
        with pytest.raises(AlicatValidationError):
            LOOP_CONTROL_VARIABLE.encode(
                ctx_v10,
                LoopControlVariableRequest(variable=Statistic.MASS_FLOW),
            )

    def test_ineligible_int_raises(self, ctx_v10: DecodeContext) -> None:
        """An int code outside the 8-member LV subset raises."""
        with pytest.raises(AlicatValidationError):
            LOOP_CONTROL_VARIABLE.encode(
                ctx_v10,
                LoopControlVariableRequest(variable=999),
            )

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        assert (
            LOOP_CONTROL_VARIABLE.encode(
                ctx_gp,
                LoopControlVariableRequest(variable=LoopControlVariable.MASS_FLOW_SETPT),
            )
            == b"A$$LV 37\r"
        )


class TestDecode:
    """Real V10 reply shape: `<uid> <stat_code>` (2 fields, no label).

    Verified 2026-04-17 against MC-500SCCM-D 10v20.0-R24 (design §16.6).
    The label is derived from the typed variable's enum value since the
    device doesn't echo one.
    """

    def test_basic(self, ctx_v10: DecodeContext) -> None:
        state = LOOP_CONTROL_VARIABLE.decode(b"A 37", ctx_v10)
        assert state.unit_id == "A"
        assert state.variable is LoopControlVariable.MASS_FLOW_SETPT
        # Label derived from the typed enum's name (LoopControlVariable is
        # an IntEnum whose value is the wire code) since the device doesn't
        # echo one — see design §16.6.
        assert state.label == "MASS_FLOW_SETPT"

    def test_second_pressure_variant(self, ctx_v10: DecodeContext) -> None:
        state = LOOP_CONTROL_VARIABLE.decode(b"A 345", ctx_v10)
        assert state.variable is LoopControlVariable.ABS_PRESS_SECOND_SETPT

    def test_ineligible_code_raises(self, ctx_v10: DecodeContext) -> None:
        """Device returning a non-LV stat code fails loud."""
        with pytest.raises(AlicatValidationError):
            LOOP_CONTROL_VARIABLE.decode(b"A 5", ctx_v10)

    def test_bad_field_count_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            LOOP_CONTROL_VARIABLE.decode(b"A", ctx_v10)

    def test_fixture_round_trips(self, ctx_v10: DecodeContext) -> None:
        script = parse_fixture(_FIXTURES_DIR / "loop_control_variable_mc.txt")
        q_state = LOOP_CONTROL_VARIABLE.decode(
            script[b"ALV\r"].rstrip(b"\r"),
            ctx_v10,
        )
        assert q_state.variable is LoopControlVariable.MASS_FLOW_SETPT
        s_state = LOOP_CONTROL_VARIABLE.decode(
            script[b"ALV 36\r"].rstrip(b"\r"),
            ctx_v10,
        )
        assert s_state.variable is LoopControlVariable.VOL_FLOW_SETPT
