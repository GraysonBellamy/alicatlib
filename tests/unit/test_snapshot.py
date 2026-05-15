"""Tests for :meth:`Device.snapshot` and :class:`AlicatDeviceSnapshot`.

Snapshot is no-I/O — built from cached :class:`DeviceInfo` + session
counters. These tests cover the wiring (no wire reads, correct field
values, last_error / recoverable_error_count plumbing).
"""

from __future__ import annotations

from datetime import date

import pytest

from alicatlib import AlicatDeviceSnapshot, DeviceSnapshot
from alicatlib.commands import Capability
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.base import Device
from alicatlib.devices.models import DeviceInfo
from alicatlib.devices.session import Session
from alicatlib.errors import AlicatTimeoutError, ErrorContext
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol.client import AlicatProtocolClient
from alicatlib.transport.fake import FakeTransport


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


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
        capabilities=Capability.BAROMETER | Capability.DISPLAY,
    )


async def _make_device() -> Device:
    transport = FakeTransport({}, label="fake://snapshot")
    await transport.open()
    client = AlicatProtocolClient(transport, default_timeout=0.05)
    session = Session(client, unit_id="A", info=_info())
    return Device(session)


@pytest.mark.anyio
async def test_snapshot_makes_no_io() -> None:
    """No wire writes should happen during ``snapshot()``."""
    device = await _make_device()
    transport = device.session._client.transport  # pyright: ignore[reportPrivateUsage]
    assert isinstance(transport, FakeTransport)
    initial_writes = len(transport.writes)

    snap = await device.snapshot()
    assert isinstance(snap, AlicatDeviceSnapshot)
    assert isinstance(snap, DeviceSnapshot)

    # Snapshot is a pure read of cached state — no writes should happen.
    assert len(transport.writes) == initial_writes


@pytest.mark.anyio
async def test_snapshot_carries_cached_identity() -> None:
    device = await _make_device()
    snap = await device.snapshot()

    assert snap.model == "MC-100SCCM-D"
    assert snap.serial == "123456"
    assert snap.firmware == "10v05"  # FirmwareVersion -> str
    assert snap.unit_id == "A"
    assert snap.name == "A"  # falls back to unit_id since Device has no manager name
    assert snap.media == Medium.GAS
    assert snap.capabilities == (Capability.BAROMETER | Capability.DISPLAY)
    assert snap.connected is True


@pytest.mark.anyio
async def test_snapshot_includes_session_counters() -> None:
    """``recoverable_error_count`` and ``last_error`` plumb through."""
    device = await _make_device()
    device.session.recoverable_error_count = 3
    err_ctx = ErrorContext(command_name="poll", unit_id="A")
    device.session._last_error = err_ctx  # pyright: ignore[reportPrivateUsage]

    snap = await device.snapshot()
    assert snap.recoverable_error_count == 3
    assert snap.last_error is err_ctx


@pytest.mark.anyio
async def test_snapshot_connected_false_after_close() -> None:
    device = await _make_device()
    await device.close()
    snap = await device.snapshot()
    assert snap.connected is False


def test_last_error_property_starts_none() -> None:
    """A fresh session reports ``last_error is None``."""
    # Synchronous construction: skip the device.aclose dance.
    transport = FakeTransport({}, label="fake://snapshot")
    # No need to open the transport for this property check.
    client = AlicatProtocolClient(transport, default_timeout=0.05)
    session = Session(client, unit_id="A", info=_info())
    assert session.last_error is None
    assert session.recoverable_error_count == 0


@pytest.mark.anyio
async def test_session_last_error_captured_on_execute_failure() -> None:
    """A failed ``execute()`` enriches and stores the context."""
    transport = FakeTransport({}, label="fake://snapshot-err")
    await transport.open()
    client = AlicatProtocolClient(transport, default_timeout=0.01)
    session = Session(client, unit_id="A", info=_info())

    from alicatlib.commands.system import VE_QUERY, VeRequest

    with pytest.raises(AlicatTimeoutError):
        # No script entry — VE will time out.
        await session.execute(VE_QUERY, VeRequest())

    last_error = session.last_error
    assert last_error is not None
    assert last_error.command_name == "ve_query"
    assert last_error.unit_id == "A"
    await transport.close()
