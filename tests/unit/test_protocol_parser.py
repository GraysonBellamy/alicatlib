"""Tests for :mod:`alicatlib.protocol.parser` and :mod:`alicatlib.protocol.framing`."""

from __future__ import annotations

from datetime import date

import pytest

from alicatlib.devices.data_frame import DataFrameFormatFlavor
from alicatlib.devices.models import ManufacturingInfo, StatusCode
from alicatlib.errors import (
    AlicatParseError,
    AlicatUnitIdMismatchError,
    UnknownGasError,
    UnknownStatisticError,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.framing import EOL, strip_eol
from alicatlib.protocol.parser import (
    parse_ascii,
    parse_bool_code,
    parse_data_frame,
    parse_data_frame_table,
    parse_enum_code,
    parse_fields,
    parse_float,
    parse_int,
    parse_manufacturing_info,
    parse_optional_float,
    parse_status_codes,
    parse_ve_response,
)
from alicatlib.registry import Unit
from alicatlib.registry.gases import Gas, gas_registry
from alicatlib.registry.statistics import Statistic, statistic_registry
from tests._typing import approx

# ---------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------


class TestFraming:
    def test_eol_is_cr(self) -> None:
        assert EOL == b"\r"

    def test_strip_eol_removes_trailing_cr(self) -> None:
        assert strip_eol(b"A ok\r") == b"A ok"

    def test_strip_eol_idempotent(self) -> None:
        assert strip_eol(b"A ok") == b"A ok"

    def test_strip_eol_removes_only_trailing(self) -> None:
        """An EOL mid-string is untouched; only the trailing one is stripped."""
        assert strip_eol(b"line1\rline2\r") == b"line1\rline2"

    def test_strip_eol_custom_separator(self) -> None:
        assert strip_eol(b"hello\n", eol=b"\n") == b"hello"


# ---------------------------------------------------------------------------
# parse_ascii
# ---------------------------------------------------------------------------


class TestParseAscii:
    def test_decodes_ascii(self) -> None:
        assert parse_ascii(b"A +0.000 N2") == "A +0.000 N2"

    def test_rejects_non_ascii_with_raw_preserved(self) -> None:
        """Non-ASCII bytes indicate line noise; the error must preserve them."""
        with pytest.raises(AlicatParseError) as ei:
            parse_ascii(b"A \xff\xfe")
        assert ei.value.context.raw_response == b"A \xff\xfe"


# ---------------------------------------------------------------------------
# parse_fields
# ---------------------------------------------------------------------------


class TestParseFields:
    def test_splits_whitespace(self) -> None:
        fields = parse_fields("A +0.000 +25.0 N2", command="poll")
        assert fields == ["A", "+0.000", "+25.0", "N2"]

    def test_collapses_runs_of_whitespace(self) -> None:
        fields = parse_fields("A    +0.000  N2", command="poll")
        assert fields == ["A", "+0.000", "N2"]

    def test_enforces_expected_count(self) -> None:
        with pytest.raises(AlicatParseError) as ei:
            parse_fields("A +0.000 N2", command="gas_select", expected_count=4)
        assert ei.value.context.command_name == "gas_select"
        assert ei.value.expected == 4
        assert ei.value.actual == 3

    def test_expected_count_match_returns_fields(self) -> None:
        assert parse_fields("A 5 N2 Nitrogen", command="gas_select", expected_count=4) == [
            "A",
            "5",
            "N2",
            "Nitrogen",
        ]

    def test_no_count_check_returns_all(self) -> None:
        assert parse_fields("one two three", command="x") == ["one", "two", "three"]


# ---------------------------------------------------------------------------
# parse_int / parse_float
# ---------------------------------------------------------------------------


class TestParseInt:
    def test_parses_positive(self) -> None:
        assert parse_int("42", field="code") == 42

    def test_parses_negative(self) -> None:
        assert parse_int("-42", field="code") == -42

    def test_rejects_float(self) -> None:
        with pytest.raises(AlicatParseError) as ei:
            parse_int("3.14", field="code")
        assert ei.value.field_name == "code"

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(AlicatParseError):
            parse_int("", field="code")


class TestParseFloat:
    def test_parses_positive(self) -> None:
        assert parse_float("3.14", field="flow") == approx(3.14)

    def test_parses_negative_with_sign(self) -> None:
        assert parse_float("-0.001", field="flow") == approx(-0.001)

    def test_parses_plus_signed(self) -> None:
        """Alicat reports positive values with a leading ``+`` — must parse."""
        assert parse_float("+12.5", field="flow") == approx(12.5)

    def test_parses_scientific(self) -> None:
        assert parse_float("1.2e-3", field="flow") == approx(0.0012)

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(AlicatParseError) as ei:
            parse_float("abc", field="flow")
        assert ei.value.field_name == "flow"


# ---------------------------------------------------------------------------
# parse_optional_float
# ---------------------------------------------------------------------------


class TestParseOptionalFloat:
    def test_returns_none_for_absent_sentinel(self) -> None:
        """`--` on the wire means the field is unavailable; map it to None."""
        assert parse_optional_float("--", field="setpoint") is None

    def test_parses_zero_as_float_not_none(self) -> None:
        """`0` is a valid value, not 'missing' — must not collapse to None."""
        assert parse_optional_float("0", field="setpoint") == approx(0.0)

    def test_parses_negative(self) -> None:
        assert parse_optional_float("-1.5", field="flow") == approx(-1.5)

    def test_delegates_to_parse_float_for_garbage(self) -> None:
        with pytest.raises(AlicatParseError):
            parse_optional_float("abc", field="flow")

    def test_empty_string_is_not_absent(self) -> None:
        """Empty string is invalid, distinct from the `--` sentinel."""
        with pytest.raises(AlicatParseError):
            parse_optional_float("", field="flow")


# ---------------------------------------------------------------------------
# parse_bool_code
# ---------------------------------------------------------------------------


class TestParseBoolCode:
    def test_default_mapping_true(self) -> None:
        assert parse_bool_code("1", field="save") is True

    def test_default_mapping_false(self) -> None:
        assert parse_bool_code("0", field="save") is False

    def test_custom_mapping(self) -> None:
        assert parse_bool_code("Y", field="enabled", mapping={"Y": True, "N": False}) is True
        assert parse_bool_code("N", field="enabled", mapping={"Y": True, "N": False}) is False

    def test_rejects_unknown_code(self) -> None:
        with pytest.raises(AlicatParseError) as ei:
            parse_bool_code("2", field="save")
        assert ei.value.field_name == "save"
        assert ei.value.actual == "2"

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(AlicatParseError):
            parse_bool_code("", field="save")

    def test_case_sensitive_by_default(self) -> None:
        """Default mapping is exact-match; custom callers can normalize themselves."""
        with pytest.raises(AlicatParseError):
            parse_bool_code("y", field="enabled", mapping={"Y": True, "N": False})


# ---------------------------------------------------------------------------
# parse_enum_code
# ---------------------------------------------------------------------------


class TestParseEnumCode:
    def test_resolves_gas_code(self) -> None:
        """Gas code 8 is N2 — the canonical registry round-trip."""
        assert parse_enum_code("8", field="gas", registry=gas_registry) is Gas.N2

    def test_resolves_statistic_code(self) -> None:
        got = parse_enum_code("13", field="stat", registry=statistic_registry)
        assert got is Statistic.VALVE_DRIVE

    def test_unknown_code_surfaces_as_parse_error(self) -> None:
        """Unknown codes from the device are protocol errors, not config errors."""
        with pytest.raises(AlicatParseError) as ei:
            parse_enum_code("9999999", field="gas", registry=gas_registry)
        assert ei.value.field_name == "gas"
        assert ei.value.actual == 9999999
        # Registry-level error is preserved as __cause__ for debuggability.
        assert isinstance(ei.value.__cause__, UnknownGasError)

    def test_unknown_statistic_code_cause_is_statistic_error(self) -> None:
        with pytest.raises(AlicatParseError) as ei:
            parse_enum_code("9999999", field="stat", registry=statistic_registry)
        assert isinstance(ei.value.__cause__, UnknownStatisticError)

    def test_non_integer_fails_before_registry_lookup(self) -> None:
        """`parse_int` failure short-circuits — no lookup attempted."""
        with pytest.raises(AlicatParseError) as ei:
            parse_enum_code("abc", field="gas", registry=gas_registry)
        assert ei.value.field_name == "gas"


# ---------------------------------------------------------------------------
# parse_ve_response
# ---------------------------------------------------------------------------


class TestParseVeResponse:
    def test_parses_v10_with_date(self) -> None:
        fw, fw_date = parse_ve_response(b"A 10v05 2021-05-19")
        assert fw == FirmwareVersion(FirmwareFamily.V10, 10, 5, "10v05")
        assert fw_date == date(2021, 5, 19)

    def test_parses_v8_v9_with_date(self) -> None:
        fw, fw_date = parse_ve_response(b"A 9v00 2013-07-15")
        assert fw.family is FirmwareFamily.V8_V9
        assert (fw.major, fw.minor) == (9, 0)
        assert fw_date == date(2013, 7, 15)

    def test_parses_v8_v9_with_month_name_date(self) -> None:
        """8v17 hardware emits ``<Mon> <DD> <YYYY>,<HH:MM:SS>`` not ISO.

        Captured during 8v17 hardware validation (design §16.1 #4); the
        extra time component is ignored.
        """
        fw, fw_date = parse_ve_response(
            b"B   8v17.0-R23 Nov 27 2019,15:28:45",
        )
        assert fw.family is FirmwareFamily.V8_V9
        assert (fw.major, fw.minor) == (8, 17)
        assert fw_date == date(2019, 11, 27)

    def test_preserves_revision_suffix_in_raw(self) -> None:
        """``FirmwareVersion.raw`` preserves the ``.N-RNN`` revision suffix.

        Captures taken 2026-04-17 (design §16.6) confirmed every captured
        device's VE reply carries a ``<major>v<minor>.0-R<NN>`` shape;
        diagnostics / logs / sink rows want the full string, even though
        gating only needs ``major`` and ``minor``.
        """
        fw, _ = parse_ve_response(b"A   10v20.0-R24 Jan  9 2025,15:04:07")
        assert fw.raw == "10v20.0-R24"
        assert (fw.major, fw.minor) == (10, 20)

    def test_preserves_revision_suffix_v1_v7(self) -> None:
        fw, _ = parse_ve_response(b"A 5v12.0-R22 May  4 2015,13:58:14")
        assert fw.raw == "5v12.0-R22"

    def test_preserves_revision_suffix_v8_v9(self) -> None:
        fw, _ = parse_ve_response(b"A 8v17.0-R23 Jun 21 2019,11:13:06")
        assert fw.raw == "8v17.0-R23"

    def test_bare_firmware_without_suffix_raw_is_just_version(self) -> None:
        """Older / custom builds without the ``.N-RNN`` suffix stay unchanged."""
        fw, _ = parse_ve_response(b"A 10v05 2021-05-19")
        assert fw.raw == "10v05"

    def test_month_name_date_is_case_insensitive(self) -> None:
        _fw, fw_date = parse_ve_response(b"A 8v17 nov 27 2019")
        assert fw_date == date(2019, 11, 27)

    def test_iso_date_takes_precedence_over_month_name(self) -> None:
        """If both formats are present, ISO wins (it's the modern one)."""
        _fw, fw_date = parse_ve_response(b"A 10v05 2021-05-19 Nov 27 2019")
        assert fw_date == date(2021, 5, 19)

    def test_malformed_month_name_date_raises(self) -> None:
        with pytest.raises(AlicatParseError) as ei:
            parse_ve_response(b"A 8v17 Feb 31 2019")
        assert ei.value.field_name == "firmware_date"

    def test_parses_v1_v7_without_date(self) -> None:
        """Older firmware may omit the date; `None` is the expected shape."""
        fw, fw_date = parse_ve_response(b"A 7v99")
        assert fw.family is FirmwareFamily.V1_V7
        assert (fw.major, fw.minor) == (7, 99)
        assert fw_date is None

    def test_parses_gp(self) -> None:
        """GP devices report a standalone `GP` token with no numeric suffix."""
        fw, fw_date = parse_ve_response(b"A GP")
        assert fw.family is FirmwareFamily.GP
        assert fw_date is None

    def test_parses_gp_with_nv_suffix(self) -> None:
        """Some GP devices report a cosmetic `GP-<major>v<minor>`; family stays GP."""
        fw, _ = parse_ve_response(b"A GP-10v05 2008-04-01")
        assert fw.family is FirmwareFamily.GP

    def test_missing_firmware_token_raises(self) -> None:
        with pytest.raises(AlicatParseError) as ei:
            parse_ve_response(b"A something went wrong")
        assert ei.value.field_name == "firmware"
        assert ei.value.context.command_name == "VE"
        assert ei.value.context.raw_response == b"A something went wrong"

    def test_non_ascii_raises(self) -> None:
        """Line noise in the VE response is a parse error, not a silent degrade."""
        with pytest.raises(AlicatParseError):
            parse_ve_response(b"A \xff\xfe 10v05")

    def test_malformed_date_raises(self) -> None:
        """A date-shaped-but-invalid token is protocol corruption — surface it."""
        with pytest.raises(AlicatParseError) as ei:
            parse_ve_response(b"A 10v05 2021-13-45")
        assert ei.value.field_name == "firmware_date"

    def test_tolerant_to_surrounding_whitespace(self) -> None:
        fw, fw_date = parse_ve_response(b"   A 10v05 2021-05-19   ")
        assert fw.major == 10
        assert fw_date == date(2021, 5, 19)


# ---------------------------------------------------------------------------
# parse_status_codes
# ---------------------------------------------------------------------------


class TestParseStatusCodes:
    def test_empty_tokens(self) -> None:
        assert parse_status_codes([]) == frozenset()

    def test_collects_known_codes(self) -> None:
        assert parse_status_codes(["HLD", "MOV"]) == frozenset({StatusCode.HLD, StatusCode.MOV})

    def test_skips_unknown_tokens(self) -> None:
        """Tolerant: non-status tokens (gas labels, numbers) are silently dropped."""
        got = parse_status_codes(["HLD", "N2", "123", "MOV"])
        assert got == frozenset({StatusCode.HLD, StatusCode.MOV})

    def test_duplicate_codes_collapse(self) -> None:
        """frozenset semantics: the wire doesn't guarantee uniqueness of a code in a line."""
        assert parse_status_codes(["HLD", "HLD"]) == frozenset({StatusCode.HLD})

    def test_result_is_frozenset(self) -> None:
        """Return type is explicitly frozen — callers can stash it in a DataFrame."""
        got = parse_status_codes(["HLD"])
        assert isinstance(got, frozenset)


# ---------------------------------------------------------------------------
# parse_manufacturing_info
# ---------------------------------------------------------------------------


class TestParseManufacturingInfo:
    def _lines(self) -> list[bytes]:
        return [
            b"A M01 Alicat Scientific",
            b"A M02 www.alicat.com",
            b"A M03 +1 520-290-6060",
            b"A M04 MC-100SCCM-D",
            b"A M05 123456",
            b"A M06 2021-01-01",
            b"A M07 2021-02-01",
            b"A M08 ACS",
            b"A M09 10v05, 2021-05-19",
            b"A M10 ",
        ]

    def test_happy_path(self) -> None:
        info = parse_manufacturing_info(self._lines())
        assert isinstance(info, ManufacturingInfo)
        assert info.unit_id == "A"
        assert info.by_code[1] == "Alicat Scientific"
        assert info.by_code[4] == "MC-100SCCM-D"
        assert info.by_code[5] == "123456"

    def test_by_code_covers_all_ten(self) -> None:
        info = parse_manufacturing_info(self._lines())
        assert set(info.by_code) == set(range(1, 11))

    def test_empty_payload_preserved_as_empty_string(self) -> None:
        """M10 with no payload stays as ``""`` — preserves the "code was emitted" fact."""
        info = parse_manufacturing_info(self._lines())
        assert info.by_code[10] == ""

    def test_get_convenience(self) -> None:
        info = parse_manufacturing_info(self._lines())
        assert info.get(4) == "MC-100SCCM-D"
        assert info.get(99) is None

    def test_empty_response_raises(self) -> None:
        with pytest.raises(AlicatParseError):
            parse_manufacturing_info([])

    def test_all_blank_lines_raises(self) -> None:
        with pytest.raises(AlicatParseError):
            parse_manufacturing_info([b"", b"   ", b"\r\n"])

    def test_malformed_line_raises(self) -> None:
        """A line that doesn't match <uid> M<NN> <payload> surfaces — not silently skipped."""
        with pytest.raises(AlicatParseError) as ei:
            parse_manufacturing_info([b"A M01 OK", b"garbage line"])
        assert ei.value.field_name == "manufacturing_info_line"

    def test_unit_id_mismatch_raises(self) -> None:
        """Mixed unit_ids across lines — a sign of cross-device buffer bleed."""
        with pytest.raises(AlicatUnitIdMismatchError):
            parse_manufacturing_info([b"A M01 first", b"B M02 second"])

    def test_duplicate_code_raises(self) -> None:
        with pytest.raises(AlicatParseError) as ei:
            parse_manufacturing_info([b"A M01 first", b"A M01 second"])
        assert ei.value.field_name == "manufacturing_info_code"

    def test_blank_lines_skipped(self) -> None:
        """Blank lines between valid entries are tolerated."""
        info = parse_manufacturing_info([b"A M01 first", b"", b"\r\n", b"A M02 second"])
        assert info.by_code[1] == "first"
        assert info.by_code[2] == "second"

    def test_by_code_is_read_only(self) -> None:
        info = parse_manufacturing_info(self._lines())
        with pytest.raises(TypeError):
            info.by_code[1] = "mutated"  # type: ignore[index]


# ---------------------------------------------------------------------------
# parse_data_frame_table
# ---------------------------------------------------------------------------


class TestParseDataFrameTable:
    """Tests use the canonical Alicat ``??D*`` dialect captured from real
    8v17 + V10 hardware on 2026-04-17 (design §16.6). Each line is::

        <uid> D<NN> <stat_code> <name (with internal spaces)> <type> <width>
            [<unit_code> <precision> <unit_label>]

    A leading ``D00 ID_ NAME...`` row is the column header and is skipped.
    Conditional rows carry a leading ``*`` on the field name and a per-flag
    mnemonic in the NOTES column.
    """

    def _lines(self) -> list[bytes]:
        """Real ??D* capture (subset) from MC-500SCCM-D, 10v20.0-R24."""
        return [
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________",
            b"A D01 700 Unit ID                    string          1",
            b"A D02 002 Abs Press                  s decimal     7/2 010 02 PSIA",
            b"A D03 003 Flow Temp                  s decimal     7/2 002 02 `C",
            b"A D04 004 Volu Flow                  s decimal     7/2 012 02 CCM",
            b"A D05 005 Mass Flow                  s decimal     7/2 012 02 SCCM",
            b"A D06 037 Mass Flow Setpt            s decimal     7/2 012 02 SCCM",
            b"A D07 703 Gas                        string          6",
            b"A D08 701 *Error                     string          3 ADC",
            b"A D09 702 *Status                    string          3 OPL",
        ]

    def test_builds_format(self) -> None:
        fmt = parse_data_frame_table(self._lines())
        assert fmt.flavor is DataFrameFormatFlavor.DEFAULT
        # Column-header row (D00) is skipped; status flag row collapses to
        # a single conditional Status field on the canonical name.
        assert fmt.names() == (
            "Unit_ID",
            "Abs_Press",
            "Flow_Temp",
            "Volu_Flow",
            "Mass_Flow",
            "Mass_Flow_Setpt",
            "Gas",
            "Error",
            "Status",
        )

    def test_required_and_conditional_split(self) -> None:
        fmt = parse_data_frame_table(self._lines())
        required = [f for f in fmt.fields if not f.conditional]
        conditional = [f for f in fmt.fields if f.conditional]
        assert [f.name for f in required] == [
            "Unit_ID",
            "Abs_Press",
            "Flow_Temp",
            "Volu_Flow",
            "Mass_Flow",
            "Mass_Flow_Setpt",
            "Gas",
        ]
        assert [f.name for f in conditional] == ["Error", "Status"]

    def test_statistic_linkage_populated_when_known(self) -> None:
        fmt = parse_data_frame_table(self._lines())
        by_name = {f.name: f for f in fmt.fields}
        assert by_name["Mass_Flow"].statistic is Statistic.MASS_FLOW
        assert by_name["Abs_Press"].statistic is Statistic.ABS_PRESS
        assert by_name["Volu_Flow"].statistic is Statistic.VOL_FLOW

    def test_statistic_none_for_unknown_raw_name(self) -> None:
        """Unknown wire name → statistic=None, not an error."""
        fmt = parse_data_frame_table(
            [b"A D01 999 VendorWidget_XYZ            decimal     7/2 010 02 PSIA"],
        )
        assert fmt.fields[0].statistic is None

    def test_decimal_parser_handles_dash_sentinel(self) -> None:
        """The bound parser on a ``decimal`` field returns None for ``--``."""
        fmt = parse_data_frame_table(
            [b"A D01 005 Mass Flow                  s decimal     7/2 012 02 SCCM"],
        )
        assert fmt.fields[0].parser("--") is None
        assert fmt.fields[0].parser("1.5") == approx(1.5)

    def test_text_parser_is_identity(self) -> None:
        fmt = parse_data_frame_table([b"A D01 700 Unit ID                    string          1"])
        assert fmt.fields[0].parser("A") == "A"

    def test_unit_bound_inline_from_notes_column(self) -> None:
        """Unit bound at ??D* parse time when a `<code> <prec> <label>` trailer is present."""
        fmt = parse_data_frame_table(
            [b"A D01 005 Mass Flow                  s decimal     7/2 012 02 SCCM"],
        )
        assert fmt.fields[0].unit is Unit.SCCM

    def test_unit_none_when_no_unit_trailer(self) -> None:
        """String-typed rows (Unit ID, Gas) carry no NOTES unit."""
        fmt = parse_data_frame_table([b"A D01 700 Unit ID                    string          1"])
        assert fmt.fields[0].unit is None

    def test_column_header_row_skipped(self) -> None:
        """The leading `D00 ID_` row is the column header, not a field."""
        lines = [
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________",
            b"A D01 005 Mass Flow                  s decimal     7/2 012 02 SCCM",
        ]
        fmt = parse_data_frame_table(lines)
        assert fmt.names() == ("Mass_Flow",)

    def test_internal_spaces_in_name_canonicalised_with_underscores(self) -> None:
        fmt = parse_data_frame_table(
            [b"A D01 037 Mass Flow Setpt            s decimal     7/2 012 02 SCCM"],
        )
        assert fmt.fields[0].name == "Mass_Flow_Setpt"
        assert fmt.fields[0].raw_name == "Mass Flow Setpt"

    def test_signed_type_marker_handled(self) -> None:
        """`s decimal` (signed) and bare `decimal` use the same numeric parser."""
        fmt = parse_data_frame_table(
            [b"A D01 005 Mass Flow                  s decimal     7/2 012 02 SCCM"],
        )
        assert fmt.fields[0].type_name == "s decimal"
        assert fmt.fields[0].parser("-1.5") == approx(-1.5)

    def test_empty_response_raises(self) -> None:
        with pytest.raises(AlicatParseError):
            parse_data_frame_table([])

    def test_only_header_line_raises(self) -> None:
        """Header-only (no field rows) → AlicatParseError."""
        header = b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________"
        with pytest.raises(AlicatParseError):
            parse_data_frame_table([header])

    def test_non_ascii_line_rejects(self) -> None:
        line = b"A D01 005 \xff\xfe                  s decimal     7/2 012 02 SCCM"
        with pytest.raises(AlicatParseError):
            parse_data_frame_table([line])


class TestParseDataFrameTableV1V7:
    """V1_V7 ??D* dialect — captured 2026-04-17 from a 5v12 controller.

    The dialect uses different columns than V8+ (see design §16.6.2):
    no statistic-code column, ``signed`` / ``char`` / ``string`` types,
    a UNITS column with the engineering label, and no ``*`` marker on
    conditional rows. Conditional rows are recognised by name (``Error``,
    ``Status``).
    """

    def _lines(self) -> list[bytes]:
        """Real 5v12 capture (subset)."""
        return [
            b"A  D00 NAME_______ TYPE_____ MinVal_  MaxVal_  UNITS__",
            b"A  D01 Unit ID     char         A         Z         na",
            b"A  D02 Pressure    signed    +000.00  +160.00     PSIA",
            b"A  D03 Temperature signed    -010.00  +050.00        C",
            b"A  D04 Volumetric  signed    +0000.0  +0500.0      CCM",
            b"A  D05 Mass        signed    +0000.0  +0500.0     SCCM",
            b"A  D06 SetPoint    signed    +0000.0  +0500.0     SCCM",
            b"A  D07 Gas         string        Air       D2       na",
            b"A  D08 Error       string         na      ADC       na",
            b"A  D09 Status      string         na      LCK       na",
        ]

    def test_dialect_auto_detected(self) -> None:
        fmt = parse_data_frame_table(self._lines())
        assert fmt.flavor is DataFrameFormatFlavor.LEGACY

    def test_field_names_canonicalised(self) -> None:
        fmt = parse_data_frame_table(self._lines())
        assert fmt.names() == (
            "Unit_ID",
            "Pressure",
            "Temperature",
            "Volumetric",
            "Mass",
            "SetPoint",
            "Gas",
            "Error",
            "Status",
        )

    def test_units_bound_from_units_column(self) -> None:
        fmt = parse_data_frame_table(self._lines())
        by_name = {f.name: f for f in fmt.fields}
        assert by_name["Pressure"].unit is Unit.PSI  # PSIA aliased to PSI
        assert by_name["Temperature"].unit is Unit.DEG_C  # `C aliased to °C
        assert by_name["Mass"].unit is Unit.SCCM
        assert by_name["Volumetric"].unit is Unit.CCM

    def test_na_unit_resolves_to_none(self) -> None:
        fmt = parse_data_frame_table(self._lines())
        by_name = {f.name: f for f in fmt.fields}
        # Unit ID and Gas have UNITS=na; Status flags have UNITS=na too.
        assert by_name["Unit_ID"].unit is None
        assert by_name["Gas"].unit is None
        assert by_name["Status"].unit is None

    def test_conditional_recognised_by_name(self) -> None:
        """V1_V7 lacks the V8+ `*<name>` marker; we recognise by known names."""
        fmt = parse_data_frame_table(self._lines())
        by_name = {f.name: f for f in fmt.fields}
        assert by_name["Pressure"].conditional is False
        assert by_name["Mass"].conditional is False
        assert by_name["Error"].conditional is True
        assert by_name["Status"].conditional is True

    def test_signed_type_uses_decimal_parser(self) -> None:
        fmt = parse_data_frame_table(self._lines())
        by_name = {f.name: f for f in fmt.fields}
        assert by_name["Pressure"].type_name == "signed"
        assert by_name["Pressure"].parser("-1.5") == approx(-1.5)
        assert by_name["Pressure"].parser("+14.62") == approx(14.62)

    def test_char_type_uses_text_parser(self) -> None:
        fmt = parse_data_frame_table(self._lines())
        by_name = {f.name: f for f in fmt.fields}
        assert by_name["Unit_ID"].type_name == "char"
        assert by_name["Unit_ID"].parser("A") == "A"

    def test_field_internal_spaces_canonicalised(self) -> None:
        """`Unit ID` becomes `Unit_ID` (consistent with V8+ behaviour)."""
        fmt = parse_data_frame_table(self._lines())
        by_name = {f.name: f for f in fmt.fields}
        assert by_name["Unit_ID"].name == "Unit_ID"
        assert by_name["Unit_ID"].raw_name == "Unit ID"

    def test_poll_round_trips_via_format_parse(self) -> None:
        """V1_V7 poll output parses through the same DataFrameFormat.parse path."""
        fmt = parse_data_frame_table(self._lines())
        poll = b"A +014.52 +021.50 +0000.1 +0000.2 0116.3      N2"
        parsed = parse_data_frame(poll, fmt)
        assert parsed.unit_id == "A"
        assert parsed.values["Pressure"] == approx(14.52)
        assert parsed.values["Temperature"] == approx(21.50)
        assert parsed.values["Mass"] == approx(0.2)
        assert parsed.values["SetPoint"] == approx(116.3)
        assert parsed.values["Gas"] == "N2"


# ---------------------------------------------------------------------------
# parse_data_frame (delegator)
# ---------------------------------------------------------------------------


class TestParseDataFrame:
    def _fmt_lines(self) -> list[bytes]:
        return [
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________",
            b"A D01 700 Unit ID                    string          1",
            b"A D02 005 Mass Flow                  s decimal     7/2 012 02 SCCM",
        ]

    def test_delegates_to_format_parse(self) -> None:
        """parse_data_frame is a thin free-function alias for DataFrameFormat.parse."""
        fmt = parse_data_frame_table(self._fmt_lines())
        parsed = parse_data_frame(b"A 1.5", fmt)
        assert parsed.unit_id == "A"
        assert parsed.values["Mass_Flow"] == approx(1.5)
        assert parsed.status == frozenset()

    def test_surfaces_format_parse_errors(self) -> None:
        fmt = parse_data_frame_table(self._fmt_lines())
        with pytest.raises(AlicatParseError):
            parse_data_frame(b"", fmt)
