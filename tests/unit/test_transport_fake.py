"""Tests for :class:`alicatlib.transport.fake.FakeTransport`."""

from __future__ import annotations

import pytest

from alicatlib.errors import AlicatConnectionError, AlicatTimeoutError
from alicatlib.transport import FakeTransport, Transport


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Static Protocol conformance.
# ---------------------------------------------------------------------------


def test_fake_transport_is_a_transport() -> None:
    """Structural check — if this fails, the Protocol contract shifted."""
    _: Transport = FakeTransport()


# ---------------------------------------------------------------------------
# Lifecycle.
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.anyio
    async def test_starts_closed(self) -> None:
        t = FakeTransport()
        assert not t.is_open

    @pytest.mark.anyio
    async def test_open_then_close(self) -> None:
        t = FakeTransport()
        await t.open()
        assert t.is_open
        await t.close()
        assert not t.is_open

    @pytest.mark.anyio
    async def test_double_open_raises(self) -> None:
        t = FakeTransport()
        await t.open()
        with pytest.raises(AlicatConnectionError):
            await t.open()

    @pytest.mark.anyio
    async def test_close_twice_is_safe(self) -> None:
        t = FakeTransport()
        await t.open()
        await t.close()
        await t.close()  # idempotent

    @pytest.mark.anyio
    async def test_write_before_open_raises(self) -> None:
        t = FakeTransport()
        with pytest.raises(AlicatConnectionError):
            await t.write(b"A\r", timeout=0.1)

    @pytest.mark.anyio
    async def test_read_before_open_raises(self) -> None:
        t = FakeTransport()
        with pytest.raises(AlicatConnectionError):
            await t.read_until(b"\r", timeout=0.1)


# ---------------------------------------------------------------------------
# Scripted replies.
# ---------------------------------------------------------------------------


class TestScriptedReplies:
    @pytest.mark.anyio
    async def test_bytes_reply(self) -> None:
        t = FakeTransport({b"A\r": b"A +0.000 +25.0 N2\r"})
        await t.open()
        await t.write(b"A\r", timeout=0.1)
        reply = await t.read_until(b"\r", timeout=0.1)
        assert reply == b"A +0.000 +25.0 N2\r"

    @pytest.mark.anyio
    async def test_multiline_reply_via_list(self) -> None:
        """A list of bytes is concatenated; read_until returns one line at a time."""
        t = FakeTransport(
            {
                b"A??M*\r": [
                    b"A M01 Alicat\r",
                    b"A M02 Model\r",
                    b"A M03 Serial\r",
                ],
            },
        )
        await t.open()
        await t.write(b"A??M*\r", timeout=0.1)
        assert await t.read_until(b"\r", timeout=0.1) == b"A M01 Alicat\r"
        assert await t.read_until(b"\r", timeout=0.1) == b"A M02 Model\r"
        assert await t.read_until(b"\r", timeout=0.1) == b"A M03 Serial\r"

    @pytest.mark.anyio
    async def test_callable_reply(self) -> None:
        def echo(cmd: bytes) -> bytes:
            return cmd[:-1] + b" echoed\r"

        t = FakeTransport({b"AGS 5\r": echo})
        await t.open()
        await t.write(b"AGS 5\r", timeout=0.1)
        assert await t.read_until(b"\r", timeout=0.1) == b"AGS 5 echoed\r"

    @pytest.mark.anyio
    async def test_unscripted_write_has_no_reply(self) -> None:
        """Unknown writes record but produce no bytes; reads time out."""
        t = FakeTransport()
        await t.open()
        await t.write(b"UNKNOWN\r", timeout=0.1)
        assert t.writes == (b"UNKNOWN\r",)
        with pytest.raises(AlicatTimeoutError):
            await t.read_until(b"\r", timeout=0.05)

    @pytest.mark.anyio
    async def test_add_script_after_open(self) -> None:
        t = FakeTransport()
        await t.open()
        t.add_script(b"A\r", b"A ok\r")
        await t.write(b"A\r", timeout=0.1)
        assert await t.read_until(b"\r", timeout=0.1) == b"A ok\r"


# ---------------------------------------------------------------------------
# Read behavior — byte exactness, pushback, available-read.
# ---------------------------------------------------------------------------


class TestReadBehavior:
    @pytest.mark.anyio
    async def test_read_until_consumes_through_separator(self) -> None:
        """Byte ordering: the separator is included; leftover bytes stay buffered."""
        t = FakeTransport({b"A\r": b"A line1\rline2\r"})
        await t.open()
        await t.write(b"A\r", timeout=0.1)
        first = await t.read_until(b"\r", timeout=0.1)
        second = await t.read_until(b"\r", timeout=0.1)
        assert first == b"A line1\r"
        assert second == b"line2\r"

    @pytest.mark.anyio
    async def test_read_until_timeout_on_missing_separator(self) -> None:
        t = FakeTransport()
        await t.open()
        t.feed(b"partial no terminator")
        with pytest.raises(AlicatTimeoutError) as ei:
            await t.read_until(b"\r", timeout=0.05)
        assert ei.value.context.extra.get("phase") == "read"

    @pytest.mark.anyio
    async def test_read_available_returns_all_buffered(self) -> None:
        t = FakeTransport()
        await t.open()
        t.feed(b"some bytes")
        got = await t.read_available(idle_timeout=0.05)
        assert got == b"some bytes"

    @pytest.mark.anyio
    async def test_read_available_honours_max_bytes(self) -> None:
        t = FakeTransport()
        await t.open()
        t.feed(b"1234567890")
        got = await t.read_available(idle_timeout=0.05, max_bytes=4)
        assert got == b"1234"
        # Remaining bytes still buffered.
        remainder = await t.read_available(idle_timeout=0.05)
        assert remainder == b"567890"

    @pytest.mark.anyio
    async def test_drain_input_clears_buffer(self) -> None:
        t = FakeTransport()
        await t.open()
        t.feed(b"garbage")
        await t.drain_input()
        got = await t.read_available(idle_timeout=0.05)
        assert got == b""


# ---------------------------------------------------------------------------
# Write recording.
# ---------------------------------------------------------------------------


class TestWriteRecording:
    @pytest.mark.anyio
    async def test_records_every_write_in_order(self) -> None:
        t = FakeTransport()
        await t.open()
        await t.write(b"A\r", timeout=0.1)
        await t.write(b"B\r", timeout=0.1)
        await t.write(b"AGS 5\r", timeout=0.1)
        assert t.writes == (b"A\r", b"B\r", b"AGS 5\r")


# ---------------------------------------------------------------------------
# Forced-timeout knobs — used to exercise error paths without real hardware.
# ---------------------------------------------------------------------------


class TestForcedTimeouts:
    @pytest.mark.anyio
    async def test_force_write_timeout_sets_write_phase(self) -> None:
        t = FakeTransport()
        await t.open()
        t.force_write_timeout()
        with pytest.raises(AlicatTimeoutError) as ei:
            await t.write(b"A\r", timeout=0.1)
        assert ei.value.context.extra.get("phase") == "write"

    @pytest.mark.anyio
    async def test_force_read_timeout_sets_read_phase(self) -> None:
        t = FakeTransport({b"A\r": b"A ok\r"})
        await t.open()
        t.force_read_timeout()
        await t.write(b"A\r", timeout=0.1)
        with pytest.raises(AlicatTimeoutError) as ei:
            await t.read_until(b"\r", timeout=0.1)
        assert ei.value.context.extra.get("phase") == "read"


# ---------------------------------------------------------------------------
# Label — used in error messages.
# ---------------------------------------------------------------------------


class TestLabel:
    def test_default_label(self) -> None:
        assert FakeTransport().label == "fake://test"

    def test_custom_label(self) -> None:
        assert FakeTransport(label="fake://sensor-A").label == "fake://sensor-A"
