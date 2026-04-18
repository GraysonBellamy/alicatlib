"""Tests for :mod:`alicatlib.commands.system`.

Identification commands: ``VE``, ``??M*``, ``??D*``. These feed the
identification pipeline in the factory. Metadata assertions pin firmware
gating so the session's gating logic has a stable contract to check against.
"""

from __future__ import annotations

from datetime import date

import pytest

from alicatlib.commands import (
    DATA_FRAME_FORMAT_QUERY,
    MANUFACTURING_INFO,
    VE_QUERY,
    DataFrameFormatQuery,
    DataFrameFormatRequest,
    ManufacturingInfoCommand,
    ManufacturingInfoRequest,
    ResponseMode,
    VeCommand,
    VeRequest,
    VeResult,
)
from alicatlib.commands.base import DecodeContext
from alicatlib.commands.system import MIN_FIRMWARE_MANUFACTURING_INFO
from alicatlib.devices import DeviceKind
from alicatlib.devices.data_frame import DataFrameFormat
from alicatlib.devices.models import ManufacturingInfo
from alicatlib.firmware import FirmwareFamily, FirmwareVersion


def _ctx(*, unit_id: str = "A", prefix: bytes = b"") -> DecodeContext:
    return DecodeContext(
        unit_id=unit_id,
        firmware=FirmwareVersion.parse("10v05"),
        command_prefix=prefix,
    )


# ---------------------------------------------------------------------------
# VE_QUERY
# ---------------------------------------------------------------------------


class TestVeQueryEncode:
    def test_encodes_plain(self) -> None:
        assert VE_QUERY.encode(_ctx(), VeRequest()) == b"AVE\r"

    def test_gp_prefix_threaded_through(self) -> None:
        """GP devices need `$$` between unit_id and token — inherited from context."""
        assert VE_QUERY.encode(_ctx(prefix=b"$$"), VeRequest()) == b"A$$VE\r"

    def test_uses_supplied_unit_id(self) -> None:
        assert VE_QUERY.encode(_ctx(unit_id="B"), VeRequest()) == b"BVE\r"


class TestVeQueryDecode:
    def test_parses_v10_with_date(self) -> None:
        result = VE_QUERY.decode(b"A 10v05 2021-05-19", _ctx())
        assert isinstance(result, VeResult)
        assert result.unit_id == "A"
        assert result.firmware == FirmwareVersion(FirmwareFamily.V10, 10, 5, "10v05")
        assert result.firmware_date == date(2021, 5, 19)

    def test_parses_gp_without_date(self) -> None:
        """GP firmware often reports no date — must not raise."""
        result = VE_QUERY.decode(b"A GP", _ctx())
        assert result.firmware.family is FirmwareFamily.GP
        assert result.firmware_date is None

    def test_unit_id_captured_from_first_token(self) -> None:
        result = VE_QUERY.decode(b"B 9v00 2013-07-15", _ctx())
        assert result.unit_id == "B"

    def test_multiline_response_rejected(self) -> None:
        """A LINE command must not be given a tuple of lines."""
        with pytest.raises(TypeError):
            VE_QUERY.decode((b"A 10v05", b"extra"), _ctx())


class TestVeQueryMetadata:
    def test_is_line_response(self) -> None:
        assert VE_QUERY.response_mode is ResponseMode.LINE

    def test_applies_to_all_device_kinds(self) -> None:
        """VE is the anchor of identification — applies before device kind is known."""
        for kind in DeviceKind:
            assert kind in VE_QUERY.device_kinds

    def test_no_firmware_gating(self) -> None:
        """VE must work across every family including GP — never gate it."""
        assert VE_QUERY.min_firmware is None
        assert VE_QUERY.firmware_families == frozenset()


# ---------------------------------------------------------------------------
# MANUFACTURING_INFO (??M*)
# ---------------------------------------------------------------------------


class TestManufacturingInfoEncode:
    def test_encodes_plain(self) -> None:
        assert MANUFACTURING_INFO.encode(_ctx(), ManufacturingInfoRequest()) == b"A??M*\r"

    def test_gp_prefix_threaded_through(self) -> None:
        """The session gates GP away from ??M*, but the encoder shouldn't lie about the prefix."""
        assert (
            MANUFACTURING_INFO.encode(_ctx(prefix=b"$$"), ManufacturingInfoRequest())
            == b"A$$??M*\r"
        )


class TestManufacturingInfoDecode:
    def _lines(self) -> tuple[bytes, ...]:
        return (
            b"A M01 Alicat Scientific",
            b"A M04 MC-100SCCM-D",
            b"A M05 123456",
        )

    def test_returns_manufacturing_info(self) -> None:
        info = MANUFACTURING_INFO.decode(self._lines(), _ctx())
        assert isinstance(info, ManufacturingInfo)
        assert info.unit_id == "A"
        assert info.by_code[4] == "MC-100SCCM-D"

    def test_single_line_response_rejected(self) -> None:
        """??M* is multiline by design; a single line is a spec mismatch."""
        with pytest.raises(TypeError):
            MANUFACTURING_INFO.decode(b"A M01 Alicat", _ctx())


class TestManufacturingInfoMetadata:
    def test_is_lines_response(self) -> None:
        assert MANUFACTURING_INFO.response_mode is ResponseMode.LINES

    def test_expected_lines_is_ten(self) -> None:
        """Per design §5.9, ??M* returns a 10-line table."""
        assert MANUFACTURING_INFO.expected_lines == 10

    def test_min_firmware_relaxed_to_none(self) -> None:
        """Session-level firmware floor is removed; factory uses try-and-recover.

        On 2026-04-17 a real 8v17 device was observed responding to
        ``??M*`` despite the primer's 8v28 floor (design §16.6). The
        gating moved into the factory's family-by-family reachability
        check, which falls back to ``model_hint`` on `?` / timeout.
        """
        assert MANUFACTURING_INFO.min_firmware is None
        # The constant is preserved for documentation reference.
        assert (
            FirmwareVersion(FirmwareFamily.V8_V9, 8, 28, "8v28") == MIN_FIRMWARE_MANUFACTURING_INFO
        )

    def test_gp_included(self) -> None:
        """GP works on ??M* — confirmed by a GP07R100 capture (design §16.6.8).

        GP firmware doesn't implement VE, so the factory's GP-path falls
        back to ``??M*`` for identification. Gating GP *out* of ??M* would
        block that path. The parser handles the GP dialect's ``\\x08``-
        wrapped payloads + single-digit M-codes alongside the canonical
        shape.
        """
        assert FirmwareFamily.GP in MANUFACTURING_INFO.firmware_families

    def test_v1_v7_included(self) -> None:
        """1v–7v works on ??M* — confirmed by 5v12 capture (design §16.6.2),
        contrary to the primer's `8v28+` annotation. The factory's
        try-and-recover wrap handles devices that nevertheless reject.
        """
        assert FirmwareFamily.V1_V7 in MANUFACTURING_INFO.firmware_families

    def test_v8_v9_and_v10_included(self) -> None:
        assert FirmwareFamily.V8_V9 in MANUFACTURING_INFO.firmware_families
        assert FirmwareFamily.V10 in MANUFACTURING_INFO.firmware_families


# ---------------------------------------------------------------------------
# DATA_FRAME_FORMAT_QUERY (??D*)
# ---------------------------------------------------------------------------


class TestDataFrameFormatQueryEncode:
    def test_encodes_plain(self) -> None:
        assert DATA_FRAME_FORMAT_QUERY.encode(_ctx(), DataFrameFormatRequest()) == b"A??D*\r"


class TestDataFrameFormatQueryDecode:
    def test_returns_data_frame_format(self) -> None:
        fmt = DATA_FRAME_FORMAT_QUERY.decode(
            (
                b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________",
                b"A D01 700 Unit ID                    string          1",
                b"A D02 005 Mass Flow                  s decimal     7/2 012 02 SCCM",
            ),
            _ctx(),
        )
        assert isinstance(fmt, DataFrameFormat)
        assert fmt.names() == ("Unit_ID", "Mass_Flow")

    def test_single_line_response_rejected(self) -> None:
        with pytest.raises(TypeError):
            DATA_FRAME_FORMAT_QUERY.decode(
                b"A D01 005 Mass Flow                  s decimal     7/2 012 02 SCCM",
                _ctx(),
            )


class TestDataFrameFormatQueryMetadata:
    def test_is_lines_response(self) -> None:
        assert DATA_FRAME_FORMAT_QUERY.response_mode is ResponseMode.LINES

    def test_is_complete_predicate_set(self) -> None:
        """Design §5.4 invariant: every LINES command must declare termination."""
        assert DATA_FRAME_FORMAT_QUERY.is_complete is not None

    def test_expected_lines_cap_set(self) -> None:
        """Safety cap prevents runaway on noise."""
        assert DATA_FRAME_FORMAT_QUERY.expected_lines == 64


class TestDataFrameFormatIsCompletePredicate:
    """Exercise the count-header terminator in isolation.

    Without hardware captures we can't be sure every firmware emits a
    count header, so the predicate is tolerant — terminating early when
    the header exists, falling through to idle-timeout otherwise.
    """

    def test_terminates_when_count_header_satisfied(self) -> None:
        pred = DATA_FRAME_FORMAT_QUERY.is_complete
        assert pred is not None
        lines = [b"A D01 3", b"A D02 f1 decimal", b"A D03 f2 decimal", b"A D04 f3 decimal"]
        assert pred(lines) is True

    def test_waits_when_count_header_not_yet_satisfied(self) -> None:
        pred = DATA_FRAME_FORMAT_QUERY.is_complete
        assert pred is not None
        lines = [b"A D01 3", b"A D02 f1 decimal"]
        assert pred(lines) is False

    def test_false_when_first_line_has_no_count(self) -> None:
        """No count header → predicate can't decide → client falls through to idle-timeout."""
        pred = DATA_FRAME_FORMAT_QUERY.is_complete
        assert pred is not None
        lines = [b"A D01 Unit_ID text", b"A D02 Mass_Flow decimal"]
        assert pred(lines) is False

    def test_false_on_empty(self) -> None:
        pred = DATA_FRAME_FORMAT_QUERY.is_complete
        assert pred is not None
        assert pred([]) is False


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------


class TestSingletons:
    def test_ve_singleton(self) -> None:
        assert isinstance(VE_QUERY, VeCommand)

    def test_manufacturing_info_singleton(self) -> None:
        assert isinstance(MANUFACTURING_INFO, ManufacturingInfoCommand)

    def test_data_frame_format_singleton(self) -> None:
        assert isinstance(DATA_FRAME_FORMAT_QUERY, DataFrameFormatQuery)
