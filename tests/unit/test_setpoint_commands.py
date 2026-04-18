"""Tests for :data:`alicatlib.commands.setpoint` — ``LS`` / ``S`` / ``LSS``.

encode / decode are pure; facade-level dispatch, LSS caching,
and BIDIRECTIONAL gating live in ``test_device_facade.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicatlib.commands import (
    SETPOINT,
    SETPOINT_LEGACY,
    SETPOINT_SOURCE,
    DecodeContext,
    SetpointLegacyRequest,
    SetpointRequest,
    SetpointSourceRequest,
)
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.errors import AlicatParseError, AlicatValidationError
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import Statistic
from alicatlib.testing import parse_fixture

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "responses"


def _mc_frame_format() -> DataFrameFormat:
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
    return DataFrameFormat(
        fields=tuple(
            DataFrameField(
                name=n,
                raw_name=n,
                type_name=t,
                statistic=s,
                unit=None,
                conditional=False,
                parser=p,
            )
            for n, t, p, s in names
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


@pytest.fixture
def ctx_v10_with_format() -> DecodeContext:
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("10v05"),
        data_frame_format=_mc_frame_format(),
    )


@pytest.fixture
def ctx_gp_with_format() -> DecodeContext:
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("GP"),
        command_prefix=b"$$",
        data_frame_format=_mc_frame_format(),
    )


# ---------------------------------------------------------------------------
# SETPOINT (LS)
# ---------------------------------------------------------------------------


class TestSetpointEncode:
    def test_query(self, ctx_v10_with_format: DecodeContext) -> None:
        assert SETPOINT.encode(ctx_v10_with_format, SetpointRequest()) == b"ALS\r"

    def test_set_integer_value(self, ctx_v10_with_format: DecodeContext) -> None:
        assert SETPOINT.encode(ctx_v10_with_format, SetpointRequest(value=50.0)) == b"ALS 50.0\r"

    def test_set_float_value(self, ctx_v10_with_format: DecodeContext) -> None:
        assert SETPOINT.encode(ctx_v10_with_format, SetpointRequest(value=75.5)) == b"ALS 75.5\r"

    def test_set_zero(self, ctx_v10_with_format: DecodeContext) -> None:
        """``value=0.0`` is a valid setpoint — valve-closed for MFCs."""
        assert SETPOINT.encode(ctx_v10_with_format, SetpointRequest(value=0.0)) == b"ALS 0.0\r"

    def test_gp_prefix(self, ctx_gp_with_format: DecodeContext) -> None:
        assert SETPOINT.encode(ctx_gp_with_format, SetpointRequest(value=50.0)) == b"A$$LS 50.0\r"


class TestSetpointDecode:
    """Real V10 LS reply: `<uid> <current> <requested> <unit_code> <unit_label>`.

    Verified 2026-04-17 against MC-500SCCM-D 10v20.0-R24 (design §16.6).
    Both ``current`` and ``requested`` are on the wire so the decoder
    returns a fully-populated :class:`SetpointState` (no follow-up frame
    parse needed). ``frame`` is ``None`` on the modern path.
    """

    def test_parses_5_field_reply(self, ctx_v10_with_format: DecodeContext) -> None:
        state = SETPOINT.decode(b"A +078.94 +078.94 12 SCCM", ctx_v10_with_format)
        assert state.unit_id == "A"
        assert state.current == 78.94
        assert state.requested == 78.94
        assert state.unit_label == "SCCM"
        assert state.frame is None

    def test_parses_settling_state(self, ctx_v10_with_format: DecodeContext) -> None:
        """Right after a SET the loop hasn't settled — current lags requested."""
        state = SETPOINT.decode(b"A +000.00 +100.00 12 SCCM", ctx_v10_with_format)
        assert state.current == 0.0
        assert state.requested == 100.0

    def test_rejects_multiline(self, ctx_v10_with_format: DecodeContext) -> None:
        with pytest.raises(TypeError):
            SETPOINT.decode(
                (b"A +078.94 +078.94 12 SCCM",),
                ctx_v10_with_format,
            )

    def test_fixture_round_trips(self, ctx_v10_with_format: DecodeContext) -> None:
        script = parse_fixture(_FIXTURES_DIR / "setpoint_set_mc.txt")
        reply = script[b"ALS 100.0\r"].rstrip(b"\r")
        state = SETPOINT.decode(reply, ctx_v10_with_format)
        assert state.requested == 100.0


# ---------------------------------------------------------------------------
# SETPOINT_LEGACY (S)
# ---------------------------------------------------------------------------


class TestSetpointLegacyEncode:
    def test_set(self) -> None:
        ctx = DecodeContext(
            unit_id="A",
            firmware=FirmwareVersion.parse("7v99"),
            data_frame_format=_mc_frame_format(),
        )
        out = SETPOINT_LEGACY.encode(ctx, SetpointLegacyRequest(value=50.0))
        assert out == b"AS 50.0\r"

    def test_gp_prefix(self, ctx_gp_with_format: DecodeContext) -> None:
        out = SETPOINT_LEGACY.encode(
            ctx_gp_with_format,
            SetpointLegacyRequest(value=50.0),
        )
        assert out == b"A$$S 50.0\r"


class TestSetpointLegacyDecode:
    def test_parses_data_frame(self) -> None:
        ctx = DecodeContext(
            unit_id="A",
            firmware=FirmwareVersion.parse("7v99"),
            data_frame_format=_mc_frame_format(),
        )
        parsed = SETPOINT_LEGACY.decode(b"A +14.70 +25.0 +45.0 +45.0 +75.0 N2", ctx)
        assert parsed.values["Setpoint"] == 75.0


# ---------------------------------------------------------------------------
# SETPOINT_SOURCE (LSS)
# ---------------------------------------------------------------------------


class TestLssEncode:
    def test_query(self, ctx_v10_with_format: DecodeContext) -> None:
        assert SETPOINT_SOURCE.encode(ctx_v10_with_format, SetpointSourceRequest()) == b"ALSS\r"

    def test_set_serial(self, ctx_v10_with_format: DecodeContext) -> None:
        assert (
            SETPOINT_SOURCE.encode(
                ctx_v10_with_format,
                SetpointSourceRequest(mode="S"),
            )
            == b"ALSS S\r"
        )

    def test_set_lowercase_coerces_to_upper(
        self,
        ctx_v10_with_format: DecodeContext,
    ) -> None:
        assert (
            SETPOINT_SOURCE.encode(
                ctx_v10_with_format,
                SetpointSourceRequest(mode="a"),
            )
            == b"ALSS A\r"
        )

    def test_set_with_save(self, ctx_v10_with_format: DecodeContext) -> None:
        assert (
            SETPOINT_SOURCE.encode(
                ctx_v10_with_format,
                SetpointSourceRequest(mode="U", save=True),
            )
            == b"ALSS U 1\r"
        )

    def test_invalid_mode_raises(self, ctx_v10_with_format: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            SETPOINT_SOURCE.encode(
                ctx_v10_with_format,
                SetpointSourceRequest(mode="X"),
            )


class TestLssDecode:
    def test_basic(self, ctx_v10_with_format: DecodeContext) -> None:
        result = SETPOINT_SOURCE.decode(b"A S", ctx_v10_with_format)
        assert result.unit_id == "A"
        assert result.mode == "S"

    def test_preserves_unknown_mode(
        self,
        ctx_v10_with_format: DecodeContext,
    ) -> None:
        """Decode preserves the raw string; set path re-validates.

        Some firmwares have reported extra letters (e.g. ``"N"``); the
        decode should not fail so diagnostics see the real value.
        """
        result = SETPOINT_SOURCE.decode(b"A N", ctx_v10_with_format)
        assert result.mode == "N"

    def test_bad_field_count_raises(self, ctx_v10_with_format: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            SETPOINT_SOURCE.decode(b"A S extra", ctx_v10_with_format)

    def test_fixture_round_trips(self, ctx_v10_with_format: DecodeContext) -> None:
        script = parse_fixture(_FIXTURES_DIR / "setpoint_source_mc.txt")
        assert (
            SETPOINT_SOURCE.decode(
                script[b"ALSS\r"].rstrip(b"\r"),
                ctx_v10_with_format,
            ).mode
            == "S"
        )
        assert (
            SETPOINT_SOURCE.decode(
                script[b"ALSS A\r"].rstrip(b"\r"),
                ctx_v10_with_format,
            ).mode
            == "A"
        )
