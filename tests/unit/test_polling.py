"""Tests for :mod:`alicatlib.commands.polling` — the ``A\\r`` poll command."""

from __future__ import annotations

import pytest

from alicatlib.commands import POLL_DATA, PollData, PollRequest, ResponseMode
from alicatlib.commands.base import Capability, DecodeContext
from alicatlib.devices import DeviceKind
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
    ParsedFrame,
)
from alicatlib.errors import AlicatParseError
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol.parser import parse_float, parse_optional_float
from alicatlib.registry._codes_gen import Statistic
from tests._typing import approx


def _identity(value: str) -> str:
    return value


def _fmt() -> DataFrameFormat:
    return DataFrameFormat(
        fields=(
            DataFrameField(
                name="Unit_ID",
                raw_name="Unit_ID",
                type_name="text",
                statistic=None,
                unit=None,
                conditional=False,
                parser=_identity,
            ),
            DataFrameField(
                name="Mass_Flow",
                raw_name="Mass_Flow",
                type_name="decimal",
                statistic=Statistic.MASS_FLOW,
                unit=None,
                conditional=False,
                parser=lambda s: parse_float(s, field="Mass_Flow"),
            ),
            DataFrameField(
                name="Setpoint",
                raw_name="Setpoint",
                type_name="decimal",
                statistic=Statistic.MASS_FLOW_SETPT,
                unit=None,
                conditional=False,
                parser=lambda s: parse_optional_float(s, field="Setpoint"),
            ),
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


def _ctx(
    *,
    unit_id: str = "A",
    prefix: bytes = b"",
    fmt: DataFrameFormat | None = None,
) -> DecodeContext:
    return DecodeContext(
        unit_id=unit_id,
        firmware=FirmwareVersion.parse("10v05"),
        capabilities=Capability.NONE,
        command_prefix=prefix,
        data_frame_format=fmt,
    )


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------


class TestPollDataEncode:
    def test_encodes_plain(self) -> None:
        """Poll is just `{unit_id}\\r` — no token."""
        assert POLL_DATA.encode(_ctx(), PollRequest()) == b"A\r"

    def test_honours_unit_id(self) -> None:
        assert POLL_DATA.encode(_ctx(unit_id="M"), PollRequest()) == b"M\r"

    def test_threads_gp_prefix(self) -> None:
        """GP devices still need the `$$` prefix even on the empty-token poll."""
        assert POLL_DATA.encode(_ctx(prefix=b"$$"), PollRequest()) == b"A$$\r"


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------


class TestPollDataDecode:
    def test_returns_parsed_frame(self) -> None:
        """Decode yields a pure ParsedFrame — session wraps with timing."""
        parsed = POLL_DATA.decode(b"A 25.5 50.0", _ctx(fmt=_fmt()))
        assert isinstance(parsed, ParsedFrame)
        assert parsed.unit_id == "A"
        assert parsed.values["Mass_Flow"] == approx(25.5)
        assert parsed.values["Setpoint"] == approx(50.0)

    def test_absent_setpoint_round_trips_as_none(self) -> None:
        parsed = POLL_DATA.decode(b"A 25.5 --", _ctx(fmt=_fmt()))
        assert parsed.values["Setpoint"] is None

    def test_missing_format_raises(self) -> None:
        """Design §5.6 invariant: decoding requires a cached DataFrameFormat."""
        with pytest.raises(AlicatParseError) as ei:
            POLL_DATA.decode(b"A 25.5 50.0", _ctx(fmt=None))
        assert ei.value.field_name == "data_frame_format"

    def test_multiline_response_rejected(self) -> None:
        """Poll is a LINE command; a multi-line tuple is a spec mismatch."""
        with pytest.raises(TypeError):
            POLL_DATA.decode((b"A 25.5 50.0", b"extra"), _ctx(fmt=_fmt()))


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class TestPollDataMetadata:
    def test_is_line_response(self) -> None:
        assert POLL_DATA.response_mode is ResponseMode.LINE

    def test_applies_to_all_device_kinds(self) -> None:
        for kind in DeviceKind:
            assert kind in POLL_DATA.device_kinds

    def test_no_firmware_gating(self) -> None:
        """Poll works on every family."""
        assert POLL_DATA.min_firmware is None
        assert POLL_DATA.firmware_families == frozenset()

    def test_not_destructive(self) -> None:
        assert not POLL_DATA.destructive

    def test_singleton(self) -> None:
        assert isinstance(POLL_DATA, PollData)
