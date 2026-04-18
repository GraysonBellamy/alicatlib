"""Tests for :meth:`Session.change_unit_id` and :meth:`Session.change_baud_rate`.

Exercises the bounded cancellation shield semantics: write outside the
shield, verify inside, escalate to ``AlicatTimeoutError`` (unit-id) or
``SessionState.BROKEN`` (baud) when the shielded reconciliation fails.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import Capability
from alicatlib.devices import DeviceKind
from alicatlib.devices.medium import Medium
from alicatlib.devices.models import DeviceInfo
from alicatlib.devices.session import (
    SUPPORTED_BAUDRATES,
    Session,
    SessionState,
)
from alicatlib.errors import (
    AlicatConnectionError,
    AlicatTimeoutError,
    AlicatValidationError,
    InvalidUnitIdError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.transport import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _info(unit_id: str = "A", firmware: str = "10v05") -> DeviceInfo:
    return DeviceInfo(
        unit_id=unit_id,
        manufacturer="Alicat",
        model="MC-100SCCM-D",
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


async def _make_session_pair(
    script: Mapping[bytes, ScriptedReply] | None = None,
    *,
    unit_id: str = "A",
    firmware: str = "10v05",
) -> tuple[Session, FakeTransport]:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    client = AlicatProtocolClient(fake, default_timeout=0.1)
    session = Session(
        client,
        unit_id=unit_id,
        info=_info(unit_id=unit_id, firmware=firmware),
        port_label="fake://test",
    )
    return session, fake


# ---------------------------------------------------------------------------
# change_unit_id
# ---------------------------------------------------------------------------


class TestChangeUnitIdGuards:
    @pytest.mark.anyio
    async def test_confirm_false_raises_and_emits_no_bytes(self) -> None:
        session, fake = await _make_session_pair()
        with pytest.raises(AlicatValidationError):
            await session.change_unit_id("B")
        assert fake.writes == ()
        assert session.unit_id == "A"

    @pytest.mark.anyio
    async def test_invalid_unit_id_raises(self) -> None:
        session, fake = await _make_session_pair()
        with pytest.raises(InvalidUnitIdError):
            await session.change_unit_id("5", confirm=True)
        assert fake.writes == ()

    @pytest.mark.anyio
    async def test_same_unit_id_raises(self) -> None:
        session, fake = await _make_session_pair()
        with pytest.raises(AlicatValidationError):
            await session.change_unit_id("A", confirm=True)
        assert fake.writes == ()


class TestChangeUnitIdHappyPath:
    @pytest.mark.anyio
    async def test_rename_updates_cached_unit_id(self) -> None:
        # Rename write has no ack; then VE on the new unit id succeeds.
        session, fake = await _make_session_pair(
            {
                b"A@ B\r": b"",  # no reply on the rename (primer)
                b"BVE\r": b"B 10v05 2021-05-19\r",
            },
        )
        await session.change_unit_id("B", confirm=True)
        assert session.unit_id == "B"
        assert session.info.unit_id == "B"
        # Both writes reached the wire: rename + verify.
        assert fake.writes == (b"A@ B\r", b"BVE\r")


class TestChangeUnitIdFailureMode:
    @pytest.mark.anyio
    async def test_verify_wrong_unit_id_leaves_cache_unchanged(self) -> None:
        """Device replies to VE at the OLD unit id — rename didn't take."""
        session, _fake = await _make_session_pair(
            {
                b"A@ B\r": b"",
                # Reply carries the old unit id — device still named A.
                b"BVE\r": b"A 10v05 2021-05-19\r",
            },
        )
        with pytest.raises(AlicatTimeoutError):
            await session.change_unit_id("B", confirm=True)
        # Cache unchanged — the rename may or may not have landed, the
        # library refuses to guess.
        assert session.unit_id == "A"
        assert session.info.unit_id == "A"

    @pytest.mark.anyio
    async def test_verify_timeout_leaves_cache_unchanged(self) -> None:
        """VE reply never arrives — rename-verify times out, cache unchanged."""
        # Rename write accepted; VE at new uid gets no reply.
        session, _fake = await _make_session_pair({b"A@ B\r": b""})
        with pytest.raises(AlicatTimeoutError):
            await session.change_unit_id("B", confirm=True)
        assert session.unit_id == "A"


# ---------------------------------------------------------------------------
# change_baud_rate
# ---------------------------------------------------------------------------


class TestChangeBaudRateGuards:
    @pytest.mark.anyio
    async def test_confirm_false_raises_and_emits_no_bytes(self) -> None:
        session, fake = await _make_session_pair()
        with pytest.raises(AlicatValidationError):
            await session.change_baud_rate(38400)
        assert fake.writes == ()

    @pytest.mark.anyio
    async def test_unsupported_baud_raises(self) -> None:
        session, fake = await _make_session_pair()
        with pytest.raises(AlicatValidationError):
            await session.change_baud_rate(12345, confirm=True)
        assert fake.writes == ()

    @pytest.mark.anyio
    async def test_every_supported_baud_would_pass_guard(self) -> None:
        """Sanity-check the supported set documented in SUPPORTED_BAUDRATES."""
        assert {2400, 9600, 19200, 38400, 57600, 115200} == set(SUPPORTED_BAUDRATES)


class TestChangeBaudRateHappyPath:
    @pytest.mark.anyio
    async def test_retunes_transport_and_verifies(self) -> None:
        session, fake = await _make_session_pair(
            {
                b"ANCB 38400\r": b"A NCB 38400\r",
                b"AVE\r": b"A 10v05 2021-05-19\r",
            },
        )
        await session.change_baud_rate(38400, confirm=True)
        # Transport was reopened exactly once at the new baud.
        assert fake.reopen_count == 1
        assert fake.last_reopen_baud == 38400
        assert session.state is SessionState.OPERATIONAL

    @pytest.mark.anyio
    async def test_gp_prefix_in_ncb_uses_poll_verify(self) -> None:
        """GP writes ``$$NCB``; verify is a prefix-less poll (``A\\r``).

        GP firmware doesn't implement ``VE`` (design §16.6.8), so
        :meth:`Session._verify_unit_id_via_ve` switches to the poll
        command for the verify probe on GP. The NCB write itself still
        carries ``$$`` because it's a write, not a read.
        """
        session, fake = await _make_session_pair(
            {
                b"A$$NCB 38400\r": b"A NCB 38400\r",
                b"A\r": b"A +14.54 +23.00 +0000.1 +0000.1 0100.0     N2 \r",
            },
            firmware="GP",
        )
        await session.change_baud_rate(38400, confirm=True)
        assert fake.reopen_count == 1


class TestChangeBaudRateBrokenPath:
    @pytest.mark.anyio
    async def test_reopen_error_transitions_to_broken(self) -> None:
        session, fake = await _make_session_pair(
            {b"ANCB 38400\r": b"A NCB 38400\r"},
        )
        fake.force_reopen_error(True)
        with pytest.raises(AlicatConnectionError) as ei:
            await session.change_baud_rate(38400, confirm=True)
        assert "BROKEN" in str(ei.value)
        assert session.state is SessionState.BROKEN

    @pytest.mark.anyio
    async def test_broken_session_refuses_further_dispatch(self) -> None:
        """Once BROKEN, execute() fails fast instead of hanging."""
        from alicatlib.commands import VE_QUERY, VeRequest

        session, fake = await _make_session_pair(
            {b"ANCB 38400\r": b"A NCB 38400\r"},
        )
        fake.force_reopen_error(True)
        with pytest.raises(AlicatConnectionError):
            await session.change_baud_rate(38400, confirm=True)
        with pytest.raises(AlicatConnectionError) as ei:
            await session.execute(VE_QUERY, VeRequest())
        assert "BROKEN" in str(ei.value)

    @pytest.mark.anyio
    async def test_change_unit_id_blocked_on_broken_session(self) -> None:
        session, fake = await _make_session_pair(
            {b"ANCB 38400\r": b"A NCB 38400\r"},
        )
        fake.force_reopen_error(True)
        with pytest.raises(AlicatConnectionError):
            await session.change_baud_rate(38400, confirm=True)
        with pytest.raises(AlicatConnectionError):
            await session.change_unit_id("B", confirm=True)

    @pytest.mark.anyio
    async def test_nack_on_ncb_transitions_to_broken(self) -> None:
        """Device replies ``?`` to NCB — guard_response raises inside the shield."""
        session, _fake = await _make_session_pair(
            {b"ANCB 38400\r": b"A ?\r"},
        )
        with pytest.raises(AlicatConnectionError):
            await session.change_baud_rate(38400, confirm=True)
        assert session.state is SessionState.BROKEN
