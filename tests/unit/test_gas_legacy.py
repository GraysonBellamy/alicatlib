"""Tests for :data:`alicatlib.commands.gas.GAS_SELECT_LEGACY` and
:data:`alicatlib.commands.gas.GAS_LIST`.

encode / decode are pure functions; the facade-level dispatch test
lives in ``test_device_facade.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicatlib.commands import (
    GAS_LIST,
    GAS_SELECT_LEGACY,
    DecodeContext,
    GasListRequest,
    GasSelectLegacyRequest,
)
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.errors import (
    AlicatParseError,
    AlicatUnitIdMismatchError,
    UnknownGasError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import Gas, Statistic
from alicatlib.testing import parse_fixture

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "responses"


def _mc_frame_format() -> DataFrameFormat:
    """Minimal DataFrameFormat matching ``dataframe_format_mc.txt``.

    Mirrors the fields in the shipped ``??D*`` fixture so a legacy-
    gas-select response (a post-op data frame) can round-trip through
    the decoder without a factory-level fixture setup.
    """

    def _text(value: str) -> float | str | None:
        return value

    def _decimal(value: str) -> float | str | None:
        return parse_optional_float(value, field="decimal")

    names = [
        ("Unit_ID", "text", _text, Statistic.NONE),
        ("Abs_Press", "decimal", _decimal, Statistic.ABS_PRESS),
        ("Flow_Temp", "decimal", _decimal, Statistic.TEMP_STREAM),
        ("Vol_Flow", "decimal", _decimal, Statistic.VOL_FLOW),
        ("Mass_Flow", "decimal", _decimal, Statistic.MASS_FLOW),
        ("Setpoint", "decimal", _decimal, Statistic.SETPT),
        ("Gas_Label", "text", _text, None),
    ]
    fields = tuple(
        DataFrameField(
            name=name,
            raw_name=name,
            type_name=type_name,
            statistic=stat,
            unit=None,
            conditional=False,
            parser=parser,
        )
        for name, type_name, parser, stat in names
    )
    return DataFrameFormat(fields=fields, flavor=DataFrameFormatFlavor.DEFAULT)


@pytest.fixture
def ctx_v8_with_format() -> DecodeContext:
    """V8_V9 firmware (pre-10v05) with a cached data-frame format.

    Legacy G applies to every non-(V10 ≥ 10v05) device; V8_V9 is the
    representative numeric-family case here. Data-frame format is
    required because legacy G's response *is* a data frame.
    """
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("8v33"),
        data_frame_format=_mc_frame_format(),
    )


@pytest.fixture
def ctx_gp_with_format() -> DecodeContext:
    """GP firmware — legacy G + ``$$`` prefix + cached frame format."""
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("GP"),
        command_prefix=b"$$",
        data_frame_format=_mc_frame_format(),
    )


# ---------------------------------------------------------------------------
# GAS_SELECT_LEGACY — encode
# ---------------------------------------------------------------------------


class TestLegacyEncode:
    def test_set_enum(self, ctx_v8_with_format: DecodeContext) -> None:
        out = GAS_SELECT_LEGACY.encode(
            ctx_v8_with_format,
            GasSelectLegacyRequest(gas=Gas.N2),
        )
        assert out == b"AG 8\r"

    def test_set_string(self, ctx_v8_with_format: DecodeContext) -> None:
        out = GAS_SELECT_LEGACY.encode(
            ctx_v8_with_format,
            GasSelectLegacyRequest(gas="N2"),
        )
        assert out == b"AG 8\r"

    def test_set_long_name(self, ctx_v8_with_format: DecodeContext) -> None:
        out = GAS_SELECT_LEGACY.encode(
            ctx_v8_with_format,
            GasSelectLegacyRequest(gas="Nitrogen"),
        )
        assert out == b"AG 8\r"

    def test_gp_prefix(self, ctx_gp_with_format: DecodeContext) -> None:
        out = GAS_SELECT_LEGACY.encode(
            ctx_gp_with_format,
            GasSelectLegacyRequest(gas=Gas.N2),
        )
        assert out == b"A$$G 8\r"

    def test_unknown_gas_raises(self, ctx_v8_with_format: DecodeContext) -> None:
        with pytest.raises(UnknownGasError):
            GAS_SELECT_LEGACY.encode(
                ctx_v8_with_format,
                GasSelectLegacyRequest(gas="NotARealGas"),
            )

    def test_unit_id_echoed_verbatim(self) -> None:
        ctx = DecodeContext(
            unit_id="Z",
            firmware=FirmwareVersion.parse("7v03"),
            data_frame_format=_mc_frame_format(),
        )
        out = GAS_SELECT_LEGACY.encode(ctx, GasSelectLegacyRequest(gas=Gas.AR))
        assert out == b"ZG 1\r"


# ---------------------------------------------------------------------------
# GAS_SELECT_LEGACY — decode
# ---------------------------------------------------------------------------


class TestLegacyDecode:
    def test_decodes_data_frame(self, ctx_v8_with_format: DecodeContext) -> None:
        raw = b"A +14.70 +25.0 +25.5 +25.5 +50.0 N2"
        parsed = GAS_SELECT_LEGACY.decode(raw, ctx_v8_with_format)
        assert parsed.unit_id == "A"
        assert parsed.values["Gas_Label"] == "N2"
        assert parsed.values["Mass_Flow"] == 25.5

    def test_missing_format_raises(self) -> None:
        ctx = DecodeContext(
            unit_id="A",
            firmware=FirmwareVersion.parse("8v33"),
            data_frame_format=None,
        )
        with pytest.raises(AlicatParseError) as ei:
            GAS_SELECT_LEGACY.decode(b"A +14.70 +25.0 +25.5 +25.5 +50.0 N2", ctx)
        assert ei.value.field_name == "data_frame_format"

    def test_rejects_multiline(self, ctx_v8_with_format: DecodeContext) -> None:
        with pytest.raises(TypeError):
            GAS_SELECT_LEGACY.decode(
                (b"A +14.70 +25.0 +25.5 +25.5 +50.0 N2",),
                ctx_v8_with_format,
            )

    def test_fixture_round_trip(self, ctx_v8_with_format: DecodeContext) -> None:
        """Drive ``GAS_SELECT_LEGACY.decode`` from the shipped fixture."""
        script = parse_fixture(_FIXTURES_DIR / "gas_select_legacy_n2.txt")
        # Shape of one reply line (drop the trailing `\r` terminator).
        assert b"AG 8\r" in script
        reply = script[b"AG 8\r"].rstrip(b"\r")
        parsed = GAS_SELECT_LEGACY.decode(reply, ctx_v8_with_format)
        assert parsed.values["Gas_Label"] == "N2"


# ---------------------------------------------------------------------------
# GAS_LIST — encode / decode
# ---------------------------------------------------------------------------


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


class TestGasListEncode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        assert GAS_LIST.encode(ctx_v10, GasListRequest()) == b"A??G*\r"

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        assert GAS_LIST.encode(ctx_gp, GasListRequest()) == b"A$$??G*\r"


class TestGasListDecode:
    """Real V10 ``??G*`` shape: rows ``<uid> G<NN>      <short_name>``.

    Verified 2026-04-17 against MC-500SCCM-D 10v20.0-R24 (design §16.6).
    The G<NN> row index is the per-device gas code; the right-aligned
    short name is the only label on the wire.
    """

    def test_basic_decode(self, ctx_v10: DecodeContext) -> None:
        lines = (
            b"A G00      Air",
            b"A G01       Ar",
            b"A G07       He",
            b"A G08       N2",
            b"A G11       O2",
        )
        result = GAS_LIST.decode(lines, ctx_v10)
        assert result[0] == "Air"
        assert result[8] == "N2"
        assert result[11] == "O2"
        assert result[7] == "He"
        assert len(result) == 5

    def test_unit_id_mismatch_raises(self, ctx_v10: DecodeContext) -> None:
        lines = (b"A G00      Air", b"B G01       Ar")
        with pytest.raises(AlicatUnitIdMismatchError):
            GAS_LIST.decode(lines, ctx_v10)

    def test_rejects_single_line(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(TypeError):
            GAS_LIST.decode(b"A G00      Air", ctx_v10)

    def test_fixture_round_trip(self, ctx_v10: DecodeContext) -> None:
        """Parse the shipped ``gas_list_mc.txt`` into the expected dict."""
        script = parse_fixture(_FIXTURES_DIR / "gas_list_mc.txt")
        # Split the scripted reply back into the tuple of CR-stripped lines.
        raw = script[b"A??G*\r"]
        lines = tuple(line for line in raw.split(b"\r") if line)
        result = GAS_LIST.decode(lines, ctx_v10)
        assert result[8] == "N2"


# ---------------------------------------------------------------------------
# is_complete predicate
# ---------------------------------------------------------------------------


class TestGasListIsComplete:
    def _pred(self) -> object:
        pred = GAS_LIST.is_complete
        assert pred is not None
        return pred

    def test_header_then_count_lines_is_complete(self) -> None:
        pred = self._pred()
        assert callable(pred)
        # Header declares 3 → complete after header + 3 entry lines (4 total).
        assert (
            pred(
                (
                    b"A G01 3",
                    b"A G02 0 Air Air",
                    b"A G03 8 N2 Nitrogen",
                    b"A G04 10 O2 Oxygen",
                )
            )
            is True
        )

    def test_not_complete_before_count(self) -> None:
        pred = self._pred()
        assert callable(pred)
        assert pred((b"A G01 3", b"A G02 0 Air Air")) is False

    def test_empty_not_complete(self) -> None:
        pred = self._pred()
        assert callable(pred)
        assert pred(()) is False

    def test_no_header_falls_through(self) -> None:
        """Without a digits-only count, the predicate returns False and
        the protocol client's idle-timeout fallback takes over."""
        pred = self._pred()
        assert callable(pred)
        assert pred((b"A G01 0 Air Air", b"A G02 8 N2 Nitrogen")) is False
