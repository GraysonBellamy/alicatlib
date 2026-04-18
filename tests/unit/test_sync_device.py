"""Tests for the sync device facade.

Two layers:

* **Direct wrapping** — construct an async :class:`Device` /
  :class:`FlowController` against a :class:`FakeTransport`, wrap it in
  a :class:`SyncDevice` / :class:`SyncFlowController`, and exercise the
  same methods that ``test_device_facade.py`` does on the async side.
* **End-to-end** — drive :meth:`Alicat.open` with a pre-built
  :class:`FakeTransport` (mirroring
  ``test_factory.TestOpenDeviceWithTransport``) so the portal +
  identification + wrapper path is covered as one pipeline.
"""

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
from alicatlib.devices.models import DeviceInfo
from alicatlib.devices.session import Session
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import Gas, Statistic
from alicatlib.sync import (
    Alicat,
    SyncDevice,
    SyncFlowController,
    SyncPortal,
)
from alicatlib.transport import FakeTransport
from tests._typing import approx

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


def _info(
    model: str = "MC-100SCCM-D",
    firmware: str = "10v05",
) -> DeviceInfo:
    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model=model,
        serial="123456",
        manufactured="2021-01-01",
        calibrated="2021-02-01",
        calibrated_by="ACS",
        software=firmware,
        firmware=FirmwareVersion.parse(firmware),
        firmware_date=date(2021, 5, 19),
        kind=DeviceKind.FLOW_CONTROLLER,
        media=Medium.GAS,
        capabilities=Capability.NONE,
    )


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


async def _make_session(
    script: Mapping[bytes, ScriptedReply] | None = None,
    *,
    firmware: str = "10v05",
    with_frame_format: bool = False,
) -> Session:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    return Session(
        client,
        unit_id="A",
        info=_info(firmware=firmware),
        data_frame_format=_mc_frame_format() if with_frame_format else None,
    )


def _mfg_lines(
    *,
    manufacturer: str = "Alicat Scientific",
    model: str = "MC-100SCCM-D",
    serial: str = "123456",
    manufactured: str = "01/01/2021",
    calibrated: str = "02/01/2021",
    calibrated_by: str = "ACS",
    software: str = "10v05",
) -> bytes:
    return b"".join(
        [
            f"A M00 {manufacturer}\r".encode("ascii"),
            b"A M01 www.example.com\r",
            b"A M02 Ph   555-000-0000\r",
            b"A M03 info@example.com\r",
            f"A M04 Model Number {model}\r".encode("ascii"),
            f"A M05 Serial Number {serial}\r".encode("ascii"),
            f"A M06 Date Manufactured {manufactured}\r".encode("ascii"),
            f"A M07 Date Calibrated   {calibrated}\r".encode("ascii"),
            f"A M08 Calibrated By     {calibrated_by}\r".encode("ascii"),
            f"A M09 Software Revision {software}\r".encode("ascii"),
        ],
    )


def _df_lines() -> bytes:
    return b"".join(
        [
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________\r",
            b"A D01 700 Unit ID                    string          1\r",
            b"A D02 002 Abs Press                  s decimal     7/2 010 02 PSIA\r",
            b"A D03 005 Mass Flow                  s decimal     7/2 012 02 SCCM\r",
            b"A D04 037 Mass Flow Setpt            s decimal     7/2 012 02 SCCM\r",
        ],
    )


def _happy_script(
    firmware: str = "10v05 Jan  9 2025,15:04:07",
    model: str = "MC-100SCCM-D",
) -> dict[bytes, bytes]:
    return {
        b"AVE\r": f"A   {firmware}\r".encode("ascii"),
        b"A??M*\r": _mfg_lines(model=model),
        b"A??D*\r": _df_lines(),
        b"A\r": b"A +14.62 +25.50 +050.00 +025.00 +050.00 N2\r",
        b"AGS 8\r": b"A 8 N2 Nitrogen\r",
        b"AGS\r": b"A 8 N2 Nitrogen\r",
    }


# ---------------------------------------------------------------------------
# SyncDevice — direct construction against an async Device.
# ---------------------------------------------------------------------------


class TestSyncDeviceDirect:
    @pytest.mark.anyio
    @pytest.mark.parametrize("anyio_backend", ["asyncio"])
    async def test_properties_passthrough(self) -> None:
        session = await _make_session()
        async_dev = Device(session)
        with SyncPortal() as portal:
            sync_dev = SyncDevice(async_dev, portal)
            assert sync_dev.info.model == "MC-100SCCM-D"
            assert sync_dev.unit_id == "A"
            assert sync_dev.session is session
            assert sync_dev.portal is portal

    def test_gas_query_through_portal(self) -> None:
        async def run() -> None:  # pyright: ignore[reportUnusedFunction]
            pass

        with SyncPortal() as portal:
            session = portal.call(_make_session, {b"AGS\r": b"A 8 N2 Nitrogen\r"})
            async_dev = Device(session)
            sync_dev = SyncDevice(async_dev, portal)

            state = sync_dev.gas()
            assert state.gas is Gas.N2

    def test_gas_set_through_portal(self) -> None:
        with SyncPortal() as portal:
            session = portal.call(_make_session, {b"AGS 8\r": b"A 8 N2 Nitrogen\r"})
            sync_dev = SyncDevice(Device(session), portal)
            state = sync_dev.gas(Gas.N2)
            assert state.gas is Gas.N2

    def test_context_manager_closes_session(self) -> None:
        with SyncPortal() as portal:
            session = portal.call(_make_session)
            with SyncDevice(Device(session), portal):
                assert not session.closed
            assert session.closed

    def test_close_is_idempotent(self) -> None:
        with SyncPortal() as portal:
            session = portal.call(_make_session)
            sync_dev = SyncDevice(Device(session), portal)
            sync_dev.close()
            sync_dev.close()
            assert session.closed


# ---------------------------------------------------------------------------
# SyncStreamingSession — streaming through the portal.
# ---------------------------------------------------------------------------


class TestSyncStreamingSession:
    """Regression cover for the portal-wrapped streaming context.

    A capture on 2026-04-17 found a real-hardware crash in
    ``SyncDevice.stream()``: routing ``__aenter__`` through
    ``portal.call`` wraps it in a fresh ``CancelScope``, but
    :meth:`StreamingSession.__aenter__` enters a long-lived task group
    that outlives the entry call — when the portal's wrapping scope
    exits, the nested task-group scope is still open, producing
    ``RuntimeError: Attempted to exit a cancel scope that isn't the
    current task's current cancel scope``. The fix routes the
    streaming CM through :meth:`SyncPortal.wrap_async_context_manager`
    instead (anyio owns the portal-side scope for the full CM
    lifetime). Prior to the fix this was a FakeTransport-coverage gap
    — the unit suite had no ``SyncDevice.stream()`` test at all.
    """

    def test_enter_iterate_exit_through_portal(self) -> None:
        """``with sync_dev.stream(...) as s: for frame in s: ...`` round-trips."""
        with SyncPortal() as portal:
            session = portal.call(_make_session, with_frame_format=True)
            fake = session._client.transport  # pyright: ignore[reportPrivateUsage]
            assert isinstance(fake, FakeTransport)
            sync_dev = SyncDevice(Device(session), portal)

            # Pre-feed a few data frames for the producer to parse.
            fake.feed(b"A +0.00 +14.50 +22.0 +0.0 +50.0 +50.0 N2\r")
            fake.feed(b"A +0.00 +14.50 +22.1 +0.0 +50.0 +50.0 N2\r")
            fake.feed(b"A +0.00 +14.50 +22.2 +0.0 +50.0 +50.0 N2\r")

            collected: list[float | str | None] = []
            with sync_dev.stream() as stream:
                for frame in stream:
                    collected.append(frame.values.get("Mass_Flow"))
                    if len(collected) == 3:
                        break

            assert len(collected) == 3
            # After exit, the streaming latch is cleared — a follow-up
            # sync call must not raise StreamingModeError.
            assert not session._client.is_streaming  # pyright: ignore[reportPrivateUsage]

    def test_exit_is_idempotent(self) -> None:
        """Double-exit (e.g. exception path + explicit exit) is safe."""
        with SyncPortal() as portal:
            session = portal.call(_make_session, with_frame_format=True)
            fake = session._client.transport  # pyright: ignore[reportPrivateUsage]
            assert isinstance(fake, FakeTransport)
            sync_dev = SyncDevice(Device(session), portal)
            fake.feed(b"A +0.00 +14.50 +22.0 +0.0 +50.0 +50.0 N2\r")

            stream = sync_dev.stream()
            stream.__enter__()
            stream.__exit__(None, None, None)
            # Second exit is a no-op (don't raise).
            stream.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# SyncFlowController — controller surface.
# ---------------------------------------------------------------------------


class TestSyncFlowController:
    def test_setpoint_set_modern(self) -> None:
        """Modern (10v05) LS reply has ``current`` + ``requested`` on the wire."""
        script = {b"ALS 50.0\r": b"A +0.00 +50.0 12 SCCM\r"}
        with SyncPortal() as portal:
            session = portal.call(_make_session, script, with_frame_format=True)
            sync_dev = SyncFlowController(FlowController(session), portal)
            state = sync_dev.setpoint(50.0)
            assert state.requested == approx(50.0)

    def test_setpoint_source_query(self) -> None:
        script = {b"ALSS\r": b"A S\r"}
        with SyncPortal() as portal:
            session = portal.call(_make_session, script)
            sync_dev = SyncFlowController(FlowController(session), portal)
            mode = sync_dev.setpoint_source()
            assert mode == "S"


# ---------------------------------------------------------------------------
# Alicat.open — end-to-end through FakeTransport.
# ---------------------------------------------------------------------------


class TestAlicatOpen:
    def test_happy_path_yields_flow_controller(self) -> None:
        fake = FakeTransport(_happy_script())
        with Alicat.open(fake) as dev:
            assert isinstance(dev, SyncFlowController)
            assert dev.info.model == "MC-100SCCM-D"
            assert dev.info.kind is DeviceKind.FLOW_CONTROLLER
            assert dev.unit_id == "A"

    def test_poll_end_to_end(self) -> None:
        fake = FakeTransport(_happy_script())
        with Alicat.open(fake) as dev:
            frame = dev.poll()
            assert frame.unit_id == "A"
            assert frame.values["Abs_Press"] == approx(14.62)

    def test_shared_portal_keeps_running_across_two_contexts(self) -> None:
        """A shared portal stays open — each ``Alicat.open`` reuses it."""
        with SyncPortal() as portal:
            fake1 = FakeTransport(_happy_script())
            with Alicat.open(fake1, portal=portal) as dev1:
                assert dev1.info.model == "MC-100SCCM-D"
            assert portal.running is True

            fake2 = FakeTransport(_happy_script())
            with Alicat.open(fake2, portal=portal) as dev2:
                assert dev2.info.model == "MC-100SCCM-D"
            assert portal.running is True

    def test_owned_portal_stops_after_exit(self) -> None:
        """Default (no ``portal=``) means the CM owns the portal."""
        fake = FakeTransport(_happy_script())
        with Alicat.open(fake) as dev:
            captured_portal = dev.portal
            assert captured_portal.running is True
        assert captured_portal.running is False

    def test_exception_during_body_still_closes(self) -> None:
        fake = FakeTransport(_happy_script())
        captured: dict[str, SyncPortal] = {}

        def _body() -> None:
            with Alicat.open(fake) as dev:
                captured["portal"] = dev.portal
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            _body()

        assert captured["portal"].running is False
