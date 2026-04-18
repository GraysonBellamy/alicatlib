"""Facade tests for the non-destructive all-device specialty surface."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import Capability
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.base import Device
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.devices.flow_controller import FlowController
from alicatlib.devices.models import (
    AnalogOutputChannel,
    DeviceInfo,
    StatusCode,
    StpNtpMode,
)
from alicatlib.devices.session import Session
from alicatlib.errors import (
    AlicatMissingHardwareError,
    AlicatValidationError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import Statistic
from alicatlib.transport import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _info(
    kind: DeviceKind = DeviceKind.FLOW_CONTROLLER,
    capabilities: Capability = Capability.NONE,
) -> DeviceInfo:
    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model="MC-100SCCM-D",
        serial="123456",
        manufactured="2021-01-01",
        calibrated="2021-02-01",
        calibrated_by="ACS",
        software="10v05",
        firmware=FirmwareVersion.parse("10v05"),
        firmware_date=date(2021, 5, 19),
        kind=kind,
        media=Medium.GAS,
        capabilities=capabilities,
    )


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


async def _make_session(
    script: Mapping[bytes, ScriptedReply] | None = None,
    *,
    kind: DeviceKind = DeviceKind.FLOW_CONTROLLER,
    capabilities: Capability = Capability.NONE,
    with_frame_format: bool = True,
) -> Session:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    return Session(
        client,
        unit_id="A",
        info=_info(kind=kind, capabilities=capabilities),
        data_frame_format=_format() if with_frame_format else None,
    )


# ---------------------------------------------------------------------------
# Data readings — zero_band / average_timing / stp_ntp_*
# ---------------------------------------------------------------------------


class TestZeroBand:
    @pytest.mark.anyio
    async def test_query(self) -> None:
        session = await _make_session({b"ADCZ\r": b"A 0 0.5\r"})
        dev = Device(session)
        state = await dev.zero_band()
        assert state.zero_band == 0.5

    @pytest.mark.anyio
    async def test_set(self) -> None:
        session = await _make_session({b"ADCZ 0 1.0\r": b"A 0 1.0\r"})
        dev = Device(session)
        state = await dev.zero_band(1.0)
        assert state.zero_band == 1.0


class TestAverageTiming:
    @pytest.mark.anyio
    async def test_set(self) -> None:
        session = await _make_session({b"ADCA 5 100\r": b"A 5 100\r"})
        dev = Device(session)
        state = await dev.average_timing(statistic_code=5, averaging_ms=100)
        assert state.averaging_ms == 100


class TestStpNtpReferences:
    @pytest.mark.anyio
    async def test_pressure_query_roundtrips_mode(self) -> None:
        session = await _make_session({b"ADCFRP S\r": b"A 14.696 2 PSIA\r"})
        dev = Device(session)
        state = await dev.stp_ntp_pressure(StpNtpMode.STP)
        # Facade fills `mode` from the request since device doesn't echo it.
        assert state.mode is StpNtpMode.STP
        assert state.pressure == 14.696

    @pytest.mark.anyio
    async def test_temperature_set(self) -> None:
        session = await _make_session({b"ADCFRT N 3 20.0\r": b"A 20.0 3 C\r"})
        dev = Device(session)
        state = await dev.stp_ntp_temperature(StpNtpMode.NTP, temperature=20.0, unit_code=3)
        assert state.mode is StpNtpMode.NTP
        assert state.temperature == 20.0


# ---------------------------------------------------------------------------
# Analog output source — capability-gated
# ---------------------------------------------------------------------------


class TestAnalogOutputSource:
    @pytest.mark.anyio
    async def test_query_requires_capability(self) -> None:
        """Without ANALOG_OUTPUT, command fails pre-I/O."""
        session = await _make_session(capabilities=Capability.NONE)
        dev = Device(session)
        with pytest.raises(AlicatMissingHardwareError):
            await dev.analog_output_source()

    @pytest.mark.anyio
    async def test_query_with_capability(self) -> None:
        session = await _make_session(
            {b"AASOCV 0\r": b"A 5 12 SCCM\r"},
            capabilities=Capability.ANALOG_OUTPUT,
        )
        dev = Device(session)
        state = await dev.analog_output_source()
        assert state.channel is AnalogOutputChannel.PRIMARY
        assert state.value == 5


# ---------------------------------------------------------------------------
# Display — capability-gated
# ---------------------------------------------------------------------------


class TestDisplay:
    @pytest.mark.anyio
    async def test_blink_requires_capability(self) -> None:
        session = await _make_session(capabilities=Capability.NONE)
        dev = Device(session)
        with pytest.raises(AlicatMissingHardwareError):
            await dev.blink_display()

    @pytest.mark.anyio
    async def test_blink_roundtrip(self) -> None:
        session = await _make_session(
            {b"AFFP 5\r": b"A 1\r"},
            capabilities=Capability.DISPLAY,
        )
        dev = Device(session)
        state = await dev.blink_display(5)
        assert state.flashing is True

    @pytest.mark.anyio
    async def test_lock_sets_lck_status(self) -> None:
        session = await _make_session(
            {b"AL\r": b"A 50.0 LCK\r"},
            capabilities=Capability.DISPLAY,
        )
        dev = Device(session)
        result = await dev.lock_display()
        assert result.locked is True
        assert StatusCode.LCK in result.frame.status

    @pytest.mark.anyio
    async def test_unlock_clears_lck_status(self) -> None:
        session = await _make_session(
            {b"AU\r": b"A 50.0\r"},
            capabilities=Capability.DISPLAY,
        )
        dev = Device(session)
        result = await dev.unlock_display()
        assert result.locked is False


# ---------------------------------------------------------------------------
# User data
# ---------------------------------------------------------------------------


class TestUserData:
    @pytest.mark.anyio
    async def test_read(self) -> None:
        session = await _make_session({b"AUD 1\r": b"A 1 hello\r"})
        dev = Device(session)
        state = await dev.user_data(slot=1)
        assert state.value == "hello"

    @pytest.mark.anyio
    async def test_write(self) -> None:
        session = await _make_session({b"AUD 1 hi\r": b"A 1 hi\r"})
        dev = Device(session)
        state = await dev.user_data(slot=1, value="hi")
        assert state.value == "hi"


# ---------------------------------------------------------------------------
# Power-up tare
# ---------------------------------------------------------------------------


class TestPowerUpTare:
    @pytest.mark.anyio
    async def test_query(self) -> None:
        session = await _make_session({b"AZCP\r": b"A 0\r"})
        dev = Device(session)
        state = await dev.power_up_tare()
        assert state.enabled is False

    @pytest.mark.anyio
    async def test_enable(self) -> None:
        session = await _make_session({b"AZCP 1\r": b"A 1\r"})
        dev = Device(session)
        state = await dev.power_up_tare(True)
        assert state.enabled is True


# ---------------------------------------------------------------------------
# Auto-tare (controllers only)
# ---------------------------------------------------------------------------


class TestAutoTareController:
    @pytest.mark.anyio
    async def test_query_on_controller(self) -> None:
        session = await _make_session({b"AZCA\r": b"A 0 0.0\r"})
        dev = FlowController(session)
        state = await dev.auto_tare()
        assert state.enabled is False
        assert state.delay_s == 0.0

    @pytest.mark.anyio
    async def test_enable_with_delay(self) -> None:
        session = await _make_session({b"AZCA 1 1.5\r": b"A 1 1.5\r"})
        dev = FlowController(session)
        state = await dev.auto_tare(enable=True, delay_s=1.5)
        assert state.enabled is True
        assert state.delay_s == 1.5

    @pytest.mark.anyio
    async def test_enable_requires_delay(self) -> None:
        session = await _make_session()
        dev = FlowController(session)
        with pytest.raises(AlicatValidationError):
            await dev.auto_tare(enable=True)
