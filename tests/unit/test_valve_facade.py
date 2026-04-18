"""Facade tests for the controller valve / ramp / deadband surface.

Exercises ``hold_valves`` / ``hold_valves_closed`` / ``cancel_valve_hold``
/ ``valve_drive`` / ``ramp_rate`` / ``deadband_limit`` through the
shared :class:`_ControllerMixin`. Flow-controller path is exercised
end-to-end; pressure-controller parity is implicit (same methods, same
implementation — sync parity tests guard the shape).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import Capability
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.devices.flow_controller import FlowController
from alicatlib.devices.models import DeviceInfo, StatusCode, TimeUnit
from alicatlib.devices.session import Session
from alicatlib.errors import AlicatValidationError
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _info() -> DeviceInfo:
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
        kind=DeviceKind.FLOW_CONTROLLER,
        media=Medium.GAS,
        capabilities=Capability.NONE,
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
    with_frame_format: bool = True,
) -> Session:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    return Session(
        client,
        unit_id="A",
        info=_info(),
        data_frame_format=_format() if with_frame_format else None,
    )


# ---------------------------------------------------------------------------
# hold_valves / hold_valves_closed / cancel_valve_hold
# ---------------------------------------------------------------------------


class TestHoldValves:
    @pytest.mark.anyio
    async def test_hp_wraps_frame_with_hld(self) -> None:
        """HP reply frame carries the HLD status bit."""
        session = await _make_session({b"AHP\r": b"A 50.0 HLD\r"})
        dev = FlowController(session)
        result = await dev.hold_valves()
        assert result.frame.unit_id == "A"
        assert result.held is True
        assert StatusCode.HLD in result.frame.status

    @pytest.mark.anyio
    async def test_hc_requires_confirm(self) -> None:
        """HC is destructive — confirm=False rejects pre-I/O."""
        session = await _make_session()
        dev = FlowController(session)
        with pytest.raises(AlicatValidationError):
            await dev.hold_valves_closed()

    @pytest.mark.anyio
    async def test_hc_with_confirm(self) -> None:
        session = await _make_session({b"AHC\r": b"A 0.0 HLD\r"})
        dev = FlowController(session)
        result = await dev.hold_valves_closed(confirm=True)
        assert result.held is True

    @pytest.mark.anyio
    async def test_cancel_clears_hold(self) -> None:
        """C reply frame has no HLD status — ``held`` is False."""
        session = await _make_session({b"AC\r": b"A 50.0\r"})
        dev = FlowController(session)
        result = await dev.cancel_valve_hold()
        assert result.held is False
        assert StatusCode.HLD not in result.frame.status

    @pytest.mark.anyio
    async def test_cancel_without_active_hold_still_ok(self) -> None:
        """Primer notes ``C`` is safe to issue unconditionally."""
        session = await _make_session({b"AC\r": b"A 50.0\r"})
        dev = FlowController(session)
        result = await dev.cancel_valve_hold()
        assert result.held is False


# ---------------------------------------------------------------------------
# valve_drive
# ---------------------------------------------------------------------------


class TestValveDrive:
    @pytest.mark.anyio
    async def test_single_valve(self) -> None:
        session = await _make_session({b"AVD\r": b"A 45.5\r"})
        dev = FlowController(session)
        state = await dev.valve_drive()
        assert state.unit_id == "A"
        assert state.valves == (45.5,)

    @pytest.mark.anyio
    async def test_dual_valve(self) -> None:
        session = await _make_session({b"AVD\r": b"A 60.0 40.0\r"})
        dev = FlowController(session)
        state = await dev.valve_drive()
        assert state.valves == (60.0, 40.0)


# ---------------------------------------------------------------------------
# ramp_rate
# ---------------------------------------------------------------------------


class TestRampRateFacade:
    @pytest.mark.anyio
    async def test_query(self) -> None:
        session = await _make_session({b"ASR\r": b"A 25.0 12 4 SCCM/s\r"})
        dev = FlowController(session)
        state = await dev.ramp_rate()
        assert state.max_ramp == 25.0
        assert state.time_unit is TimeUnit.SECOND

    @pytest.mark.anyio
    async def test_set_basic(self) -> None:
        session = await _make_session({b"ASR 50.0 4\r": b"A 50.0 12 4 SCCM/s\r"})
        dev = FlowController(session)
        state = await dev.ramp_rate(50.0, TimeUnit.SECOND)
        assert state.max_ramp == 50.0

    @pytest.mark.anyio
    async def test_set_disable_requires_time_unit(self) -> None:
        session = await _make_session()
        dev = FlowController(session)
        with pytest.raises(AlicatValidationError):
            await dev.ramp_rate(0.0)


# ---------------------------------------------------------------------------
# deadband_limit
# ---------------------------------------------------------------------------


class TestDeadbandLimitFacade:
    @pytest.mark.anyio
    async def test_query(self) -> None:
        session = await _make_session({b"ALCDB\r": b"A 0.5 2 PSIA\r"})
        dev = FlowController(session)
        state = await dev.deadband_limit()
        assert state.deadband == 0.5
        assert state.unit_label == "PSIA"

    @pytest.mark.anyio
    async def test_set_volatile(self) -> None:
        session = await _make_session({b"ALCDB 0 0.5\r": b"A 0.5 2 PSIA\r"})
        dev = FlowController(session)
        state = await dev.deadband_limit(0.5)
        assert state.deadband == 0.5

    @pytest.mark.anyio
    async def test_set_persisted(self) -> None:
        session = await _make_session({b"ALCDB 1 0.5\r": b"A 0.5 2 PSIA\r"})
        dev = FlowController(session)
        state = await dev.deadband_limit(0.5, save=True)
        assert state.deadband == 0.5
