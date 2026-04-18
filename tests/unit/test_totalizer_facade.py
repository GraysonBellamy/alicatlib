"""Facade tests for the totalizer surface."""

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
from alicatlib.devices.models import (
    DeviceInfo,
    TotalizerId,
    TotalizerLimitMode,
    TotalizerMode,
)
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


def _info(kind: DeviceKind = DeviceKind.FLOW_METER) -> DeviceInfo:
    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model="M-100SCCM-D",
        serial="123456",
        manufactured="2021-01-01",
        calibrated="2021-02-01",
        calibrated_by="ACS",
        software="10v05",
        firmware=FirmwareVersion.parse("10v05"),
        firmware_date=date(2021, 5, 19),
        kind=kind,
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
# totalizer_config
# ---------------------------------------------------------------------------


class TestTotalizerConfigFacade:
    @pytest.mark.anyio
    async def test_query_roundtrips_totalizer(self) -> None:
        """Facade fills `totalizer` from the request since the wire reply doesn't echo it."""
        session = await _make_session({b"ATC 1\r": b"A 5 2 1 8 2\r"})
        dev = Device(session)
        config = await dev.totalizer_config(TotalizerId.FIRST)
        assert config.totalizer is TotalizerId.FIRST
        assert config.enabled is True
        assert config.mode is TotalizerMode.BIDIRECTIONAL

    @pytest.mark.anyio
    async def test_query_second_roundtrips(self) -> None:
        session = await _make_session({b"ATC 2\r": b"A 1 0 0 7 0\r"})
        dev = Device(session)
        config = await dev.totalizer_config(TotalizerId.SECOND)
        assert config.totalizer is TotalizerId.SECOND
        assert config.enabled is False  # flow_statistic_code=1 → disabled

    @pytest.mark.anyio
    async def test_disable(self) -> None:
        session = await _make_session({b"ATC 1 1\r": b"A 1 0 0 7 0\r"})
        dev = Device(session)
        config = await dev.totalizer_config(TotalizerId.FIRST, flow_statistic_code=1)
        assert config.enabled is False

    @pytest.mark.anyio
    async def test_full_set(self) -> None:
        session = await _make_session({b"ATC 1 5 2 1 8 2\r": b"A 5 2 1 8 2\r"})
        dev = Device(session)
        config = await dev.totalizer_config(
            TotalizerId.FIRST,
            flow_statistic_code=5,
            mode=TotalizerMode.BIDIRECTIONAL,
            limit_mode=TotalizerLimitMode.ROLLOVER,
            digits=8,
            decimal_place=2,
        )
        assert config.flow_statistic_code == 5
        assert config.mode is TotalizerMode.BIDIRECTIONAL


# ---------------------------------------------------------------------------
# totalizer_reset / totalizer_reset_peak — destructive, token-collision-safe
# ---------------------------------------------------------------------------


class TestTotalizerResetFacade:
    @pytest.mark.anyio
    async def test_reset_requires_confirm(self) -> None:
        session = await _make_session()
        dev = Device(session)
        with pytest.raises(AlicatValidationError):
            await dev.totalizer_reset(TotalizerId.FIRST)

    @pytest.mark.anyio
    async def test_reset_with_confirm_roundtrips(self) -> None:
        """Reset emits ``T 1\\r`` (never the bare ``T\\r`` tare form)."""
        session = await _make_session({b"AT 1\r": b"A 0.000\r"})
        dev = Device(session)
        result = await dev.totalizer_reset(TotalizerId.FIRST, confirm=True)
        assert result.frame.unit_id == "A"

    @pytest.mark.anyio
    async def test_reset_peak_requires_confirm(self) -> None:
        session = await _make_session()
        dev = Device(session)
        with pytest.raises(AlicatValidationError):
            await dev.totalizer_reset_peak(TotalizerId.FIRST)

    @pytest.mark.anyio
    async def test_reset_peak_with_confirm_roundtrips(self) -> None:
        session = await _make_session({b"ATP 2\r": b"A 0.000\r"})
        dev = Device(session)
        result = await dev.totalizer_reset_peak(TotalizerId.SECOND, confirm=True)
        assert result.frame.unit_id == "A"


# ---------------------------------------------------------------------------
# totalizer_save
# ---------------------------------------------------------------------------


class TestTotalizerSaveFacade:
    @pytest.mark.anyio
    async def test_query(self) -> None:
        session = await _make_session({b"ATCR\r": b"A 1\r"})
        dev = Device(session)
        state = await dev.totalizer_save()
        assert state.enabled is True

    @pytest.mark.anyio
    async def test_enable(self) -> None:
        session = await _make_session({b"ATCR 1\r": b"A 1\r"})
        dev = Device(session)
        state = await dev.totalizer_save(True)
        assert state.enabled is True
