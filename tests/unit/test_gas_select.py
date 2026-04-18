"""Tests for :data:`alicatlib.commands.gas.GAS_SELECT`.

encode / decode are pure functions; the transport-level integration test
lives in ``test_command_end_to_end.py``.
"""

from __future__ import annotations

import pytest

from alicatlib.commands import GAS_SELECT, DecodeContext, GasSelectRequest
from alicatlib.errors import AlicatParseError, UnknownGasError
from alicatlib.firmware import FirmwareVersion
from alicatlib.registry import Gas


@pytest.fixture
def ctx_v10() -> DecodeContext:
    """Unit A on 10v05 firmware — the current (numeric-family) path."""
    return DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))


@pytest.fixture
def ctx_gp() -> DecodeContext:
    """Unit A on GP firmware — requires ``$$`` prefix on every command."""
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("GP"),
        command_prefix=b"$$",
    )


# ---------------------------------------------------------------------------
# encode
# ---------------------------------------------------------------------------


class TestEncodeQueryForm:
    def test_no_gas_produces_query(self, ctx_v10: DecodeContext) -> None:
        assert GAS_SELECT.encode(ctx_v10, GasSelectRequest()) == b"AGS\r"

    def test_query_emits_no_save_flag(self, ctx_v10: DecodeContext) -> None:
        # Even if save is explicitly set, gas=None means query form, no flag.
        assert GAS_SELECT.encode(ctx_v10, GasSelectRequest(save=True)) == b"AGS\r"

    def test_gp_prefix_in_query(self, ctx_gp: DecodeContext) -> None:
        assert GAS_SELECT.encode(ctx_gp, GasSelectRequest()) == b"A$$GS\r"


class TestEncodeSetForm:
    def test_enum_member_with_no_save(self, ctx_v10: DecodeContext) -> None:
        assert GAS_SELECT.encode(ctx_v10, GasSelectRequest(gas=Gas.N2)) == b"AGS 8\r"

    def test_string_short_name(self, ctx_v10: DecodeContext) -> None:
        assert GAS_SELECT.encode(ctx_v10, GasSelectRequest(gas="N2")) == b"AGS 8\r"

    def test_string_long_name_coerces(self, ctx_v10: DecodeContext) -> None:
        assert GAS_SELECT.encode(ctx_v10, GasSelectRequest(gas="Nitrogen")) == b"AGS 8\r"

    def test_case_insensitive_coercion(self, ctx_v10: DecodeContext) -> None:
        assert GAS_SELECT.encode(ctx_v10, GasSelectRequest(gas="nitrogen")) == b"AGS 8\r"

    def test_save_true_appends_one(self, ctx_v10: DecodeContext) -> None:
        assert GAS_SELECT.encode(ctx_v10, GasSelectRequest(gas=Gas.N2, save=True)) == b"AGS 8 1\r"

    def test_save_false_appends_zero(self, ctx_v10: DecodeContext) -> None:
        """``save=False`` is distinct from ``save=None`` — must emit the 0 flag."""
        assert GAS_SELECT.encode(ctx_v10, GasSelectRequest(gas=Gas.N2, save=False)) == b"AGS 8 0\r"

    def test_unknown_gas_string_raises_with_suggestions(
        self,
        ctx_v10: DecodeContext,
    ) -> None:
        with pytest.raises(UnknownGasError) as ei:
            GAS_SELECT.encode(ctx_v10, GasSelectRequest(gas="Nitrgen"))  # typo
        assert "Nitrogen" in ei.value.suggestions

    def test_gp_prefix_in_set(self, ctx_gp: DecodeContext) -> None:
        assert GAS_SELECT.encode(ctx_gp, GasSelectRequest(gas=Gas.N2, save=True)) == b"A$$GS 8 1\r"

    def test_unit_id_echoed_verbatim(self) -> None:
        """Unit IDs other than 'A' must flow through unchanged."""
        ctx = DecodeContext(unit_id="Z", firmware=FirmwareVersion.parse("10v05"))
        assert GAS_SELECT.encode(ctx, GasSelectRequest(gas=Gas.AR)) == b"ZGS 1\r"


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------


class TestDecode:
    def test_basic_decode(self, ctx_v10: DecodeContext) -> None:
        """Device reply ``<unit> <code> <short> <long>`` → typed GasState."""
        state = GAS_SELECT.decode(b"A 8 N2 Nitrogen", ctx_v10)
        assert state.unit_id == "A"
        assert state.code == 8
        assert state.gas is Gas.N2
        assert state.label == "N2"
        assert state.long_name == "Nitrogen"

    def test_air_is_code_zero(self, ctx_v10: DecodeContext) -> None:
        state = GAS_SELECT.decode(b"A 0 Air Air", ctx_v10)
        assert state.gas is Gas.AIR

    def test_multi_word_long_name_rejected(self, ctx_v10: DecodeContext) -> None:
        """parse_fields with expected_count=4 rejects 5-field replies.

        A real device response for Air is ``A 0 Air Air`` (two tokens after
        the code). If a device ever sends a long name like
        ``Carbon Monoxide`` split across whitespace, the decoder flags it
        — users should report the device firmware rather than have us
        silently mis-parse.
        """
        with pytest.raises(AlicatParseError):
            GAS_SELECT.decode(b"A 3 CO Carbon Monoxide", ctx_v10)

    def test_non_integer_code_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError) as ei:
            GAS_SELECT.decode(b"A BAD N2 Nitrogen", ctx_v10)
        assert ei.value.field_name == "code"

    def test_too_few_fields_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            GAS_SELECT.decode(b"A 8 N2", ctx_v10)

    def test_multiline_response_is_rejected(self, ctx_v10: DecodeContext) -> None:
        """GS is a LINE command; the session shouldn't hand it a tuple.
        Defense in depth — if some future middleware does, fail loudly.
        """
        with pytest.raises(TypeError):
            GAS_SELECT.decode((b"A 8 N2 Nitrogen",), ctx_v10)

    def test_unknown_code_raises_with_raw_preserved(
        self,
        ctx_v10: DecodeContext,
    ) -> None:
        """Custom mix codes 236-255 are in the registry; an out-of-range code
        (say 9999) is not — the decoder should raise, not invent a Gas member.
        """
        with pytest.raises(UnknownGasError):
            GAS_SELECT.decode(b"A 9999 X X", ctx_v10)
