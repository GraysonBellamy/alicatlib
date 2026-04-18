"""Tests for :data:`alicatlib.commands.tare` — ``T`` / ``TP`` / ``PC``.

encode / decode are pure functions; the facade-level tests covering
INFO-log preconditions, ``BAROMETER`` gating, and ``ParsedFrame``
wrapping live in ``test_device_facade.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from alicatlib.commands import (
    TARE_ABSOLUTE_PRESSURE,
    TARE_FLOW,
    TARE_GAUGE_PRESSURE,
    Capability,
    Command,
    DecodeContext,
    TareAbsolutePressureRequest,
    TareFlowRequest,
    TareGaugePressureRequest,
)
from alicatlib.devices import DeviceKind
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.errors import AlicatParseError
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import Statistic
from alicatlib.testing import parse_fixture

if TYPE_CHECKING:
    from alicatlib.devices.data_frame import ParsedFrame

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "responses"


def _mc_frame_format() -> DataFrameFormat:
    """Minimal DataFrameFormat matching ``dataframe_format_mc.txt``."""

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
def ctx_with_format() -> DecodeContext:
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
# Metadata pins — device_kinds + required_capabilities
# ---------------------------------------------------------------------------


class TestTareMetadata:
    def test_tare_flow_flow_devices_only(self) -> None:
        assert TARE_FLOW.device_kinds == frozenset(
            {DeviceKind.FLOW_METER, DeviceKind.FLOW_CONTROLLER},
        )
        assert TARE_FLOW.required_capabilities is Capability.NONE

    def test_tare_gauge_pressure_covers_flow_and_pressure(self) -> None:
        assert DeviceKind.FLOW_METER in TARE_GAUGE_PRESSURE.device_kinds
        assert DeviceKind.FLOW_CONTROLLER in TARE_GAUGE_PRESSURE.device_kinds
        assert DeviceKind.PRESSURE_METER in TARE_GAUGE_PRESSURE.device_kinds
        assert DeviceKind.PRESSURE_CONTROLLER in TARE_GAUGE_PRESSURE.device_kinds
        assert TARE_GAUGE_PRESSURE.required_capabilities is Capability.NONE

    def test_tare_absolute_pressure_requires_tareable_abs_pressure(self) -> None:
        assert TARE_ABSOLUTE_PRESSURE.required_capabilities is Capability.TAREABLE_ABSOLUTE_PRESSURE


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------


class TestEncode:
    def test_tare_flow(self, ctx_with_format: DecodeContext) -> None:
        out = TARE_FLOW.encode(ctx_with_format, TareFlowRequest())
        assert out == b"AT\r"

    def test_tare_gauge_pressure(self, ctx_with_format: DecodeContext) -> None:
        out = TARE_GAUGE_PRESSURE.encode(ctx_with_format, TareGaugePressureRequest())
        assert out == b"ATP\r"

    def test_tare_absolute_pressure(self, ctx_with_format: DecodeContext) -> None:
        out = TARE_ABSOLUTE_PRESSURE.encode(
            ctx_with_format,
            TareAbsolutePressureRequest(),
        )
        assert out == b"APC\r"

    def test_gp_prefix(self, ctx_gp_with_format: DecodeContext) -> None:
        assert TARE_FLOW.encode(ctx_gp_with_format, TareFlowRequest()) == b"A$$T\r"
        assert (
            TARE_GAUGE_PRESSURE.encode(
                ctx_gp_with_format,
                TareGaugePressureRequest(),
            )
            == b"A$$TP\r"
        )
        assert (
            TARE_ABSOLUTE_PRESSURE.encode(
                ctx_gp_with_format,
                TareAbsolutePressureRequest(),
            )
            == b"A$$PC\r"
        )

    def test_unit_id_echoed_verbatim(self) -> None:
        ctx = DecodeContext(
            unit_id="Z",
            firmware=FirmwareVersion.parse("10v05"),
            data_frame_format=_mc_frame_format(),
        )
        assert TARE_FLOW.encode(ctx, TareFlowRequest()) == b"ZT\r"


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------


class TestDecode:
    def test_tare_flow_parses_frame(self, ctx_with_format: DecodeContext) -> None:
        parsed = TARE_FLOW.decode(b"A +14.70 +25.0 +0.0 +0.0 +50.0 N2", ctx_with_format)
        assert parsed.unit_id == "A"
        assert parsed.values["Mass_Flow"] == 0.0
        assert parsed.values["Gas_Label"] == "N2"

    def test_tare_gauge_pressure_parses_frame(
        self,
        ctx_with_format: DecodeContext,
    ) -> None:
        parsed = TARE_GAUGE_PRESSURE.decode(
            b"A +14.70 +25.0 +25.5 +25.5 +50.0 N2",
            ctx_with_format,
        )
        assert parsed.values["Abs_Press"] == 14.70

    def test_missing_format_raises(self) -> None:
        ctx = DecodeContext(
            unit_id="A",
            firmware=FirmwareVersion.parse("10v05"),
            data_frame_format=None,
        )
        with pytest.raises(AlicatParseError) as ei:
            TARE_FLOW.decode(b"A +14.70 +25.0 +0.0 +0.0 +50.0 N2", ctx)
        assert ei.value.field_name == "data_frame_format"

    def test_rejects_multiline(self, ctx_with_format: DecodeContext) -> None:
        with pytest.raises(TypeError):
            TARE_FLOW.decode(
                (b"A +14.70 +25.0 +0.0 +0.0 +50.0 N2",),
                ctx_with_format,
            )

    @pytest.mark.parametrize(
        ("fixture_name", "send_bytes", "command", "request_cls"),
        [
            ("tare_flow_mc.txt", b"AT\r", TARE_FLOW, TareFlowRequest),
            (
                "tare_gauge_pressure_mc.txt",
                b"ATP\r",
                TARE_GAUGE_PRESSURE,
                TareGaugePressureRequest,
            ),
            (
                "tare_absolute_pressure_mc.txt",
                b"APC\r",
                TARE_ABSOLUTE_PRESSURE,
                TareAbsolutePressureRequest,
            ),
        ],
    )
    def test_fixture_round_trip(
        self,
        ctx_with_format: DecodeContext,
        fixture_name: str,
        send_bytes: bytes,
        command: Command[Any, ParsedFrame],
        request_cls: type[Any],
    ) -> None:
        """Each shipped fixture decodes cleanly under the cached format."""
        script = parse_fixture(_FIXTURES_DIR / fixture_name)
        assert send_bytes in script
        # encode round-trip pins the wire shape against the fixture.
        assert command.encode(ctx_with_format, request_cls()) == send_bytes
        reply = script[send_bytes].rstrip(b"\r")
        parsed = command.decode(reply, ctx_with_format)
        assert parsed.unit_id == "A"
