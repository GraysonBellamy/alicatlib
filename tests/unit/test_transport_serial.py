"""Tests for :class:`alicatlib.transport.serial.SerialTransport`.

Backed by ``anyserial.testing.serial_port_pair()`` — no hardware needed. Each
test opens one side as our :class:`SerialTransport` and uses the other as the
peer simulating device-side bytes / faults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from anyserial.testing import serial_port_pair

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from alicatlib.errors import (
    AlicatConnectionError,
    AlicatTimeoutError,
    AlicatTransportError,
)
from alicatlib.transport import SerialSettings, SerialTransport, Transport

if TYPE_CHECKING:
    from anyserial import SerialPort


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
async def paired(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[SerialTransport, SerialPort]]:
    """Yield (transport, peer). The transport's ``open_serial_port`` is
    monkeypatched to return one side of a mocked pair; the caller drives the
    other side to simulate the device.
    """
    port_a, port_b = serial_port_pair()

    async def fake_open(path: str, config: object) -> SerialPort:
        return port_a

    monkeypatch.setattr(
        "alicatlib.transport.serial.open_serial_port",
        fake_open,
    )

    transport = SerialTransport(SerialSettings(port="/dev/mockA"))
    await transport.open()
    try:
        yield transport, port_b
    finally:
        await transport.close()
        if port_b.is_open:
            await port_b.aclose()


# ---------------------------------------------------------------------------
# Static Protocol conformance.
# ---------------------------------------------------------------------------


def test_serial_transport_is_a_transport() -> None:
    _: Transport = SerialTransport(SerialSettings(port="/dev/null"))


# ---------------------------------------------------------------------------
# Basic round-trip.
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.anyio
    async def test_write_reaches_peer(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, peer = paired
        await transport.write(b"A??M*\r", timeout=1.0)
        got = await peer.receive(32)
        assert got == b"A??M*\r"

    @pytest.mark.anyio
    async def test_read_until_consumes_one_line(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, peer = paired
        await peer.send(b"A +0.000 +25.0\rA next\r")
        line = await transport.read_until(b"\r", timeout=1.0)
        assert line == b"A +0.000 +25.0\r"

    @pytest.mark.anyio
    async def test_read_until_preserves_pushback_across_calls(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        """Bytes past the separator must stay buffered for the next read."""
        transport, peer = paired
        await peer.send(b"line1\rline2\rline3\r")
        assert await transport.read_until(b"\r", timeout=1.0) == b"line1\r"
        assert await transport.read_until(b"\r", timeout=1.0) == b"line2\r"
        assert await transport.read_until(b"\r", timeout=1.0) == b"line3\r"


# ---------------------------------------------------------------------------
# Timeouts — read phase vs write phase must be distinguishable.
# ---------------------------------------------------------------------------


class TestTimeouts:
    @pytest.mark.anyio
    async def test_read_until_raises_on_missing_separator(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, peer = paired
        await peer.send(b"no terminator yet")
        with pytest.raises(AlicatTimeoutError) as ei:
            await transport.read_until(b"\r", timeout=0.05)
        assert ei.value.context.extra.get("phase") == "read"

    @pytest.mark.anyio
    async def test_read_until_timeout_preserves_partial_for_next_call(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        """After a read timeout, the rest of the line may arrive — the
        partial bytes must roll into the next read_until instead of being
        discarded.
        """
        transport, peer = paired
        await peer.send(b"partial")
        with pytest.raises(AlicatTimeoutError):
            await transport.read_until(b"\r", timeout=0.05)
        await peer.send(b" rest\r")
        line = await transport.read_until(b"\r", timeout=1.0)
        assert line == b"partial rest\r"

    @pytest.mark.anyio
    async def test_write_timeout_sets_write_phase(
        self,
        monkeypatch: pytest.MonkeyPatch,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        """A slow ``send()`` should surface as AlicatTimeoutError with
        ``phase='write'`` — distinct from read timeouts so observability can
        tell a jammed bus from a non-responsive device.
        """
        import anyio

        transport, _peer = paired

        async def slow_send(data: bytes) -> None:
            await anyio.sleep(10)

        # Reach into SerialTransport's port and monkeypatch its send.
        monkeypatch.setattr(transport._port, "send", slow_send)  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(AlicatTimeoutError) as ei:
            await transport.write(b"A\r", timeout=0.05)
        assert ei.value.context.extra.get("phase") == "write"


# ---------------------------------------------------------------------------
# Lifecycle.
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.anyio
    async def test_double_open_raises(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, _peer = paired
        with pytest.raises(AlicatConnectionError):
            await transport.open()

    @pytest.mark.anyio
    async def test_write_after_close_raises_connection_error(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, _peer = paired
        await transport.close()
        with pytest.raises(AlicatConnectionError):
            await transport.write(b"A\r", timeout=0.1)

    @pytest.mark.anyio
    async def test_close_is_idempotent(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, _peer = paired
        await transport.close()
        await transport.close()  # should not raise
        assert not transport.is_open


# ---------------------------------------------------------------------------
# read_available — idle-timeout semantics.
# ---------------------------------------------------------------------------


class TestReadAvailable:
    @pytest.mark.anyio
    async def test_returns_empty_when_nothing_arrives(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, _peer = paired
        got = await transport.read_available(idle_timeout=0.05)
        assert got == b""

    @pytest.mark.anyio
    async def test_returns_buffered_bytes(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, peer = paired
        await peer.send(b"leftover")
        got = await transport.read_available(idle_timeout=0.1)
        assert got == b"leftover"

    @pytest.mark.anyio
    async def test_honours_max_bytes_and_preserves_remainder(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, peer = paired
        await peer.send(b"1234567890")
        first = await transport.read_available(idle_timeout=0.1, max_bytes=4)
        assert first == b"1234"
        # The remainder sits in SerialTransport's pushback.
        rest = await transport.read_available(idle_timeout=0.1)
        assert rest == b"567890"


# ---------------------------------------------------------------------------
# drain_input — both anyserial's input buffer and our pushback.
# ---------------------------------------------------------------------------


class TestDrainInput:
    @pytest.mark.anyio
    async def test_drain_clears_pushback(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, peer = paired
        await peer.send(b"partial")
        with pytest.raises(AlicatTimeoutError):
            await transport.read_until(b"\r", timeout=0.05)
        # ``partial`` is now in the pushback; drain must wipe it.
        await transport.drain_input()
        got = await transport.read_available(idle_timeout=0.05)
        assert got == b""


# ---------------------------------------------------------------------------
# Error normalisation — backend SerialError surfaces as AlicatTransportError.
# ---------------------------------------------------------------------------


class TestErrorMapping:
    @pytest.mark.anyio
    async def test_disconnected_fault_raises_alicat_transport_error(
        self,
        paired: tuple[SerialTransport, SerialPort],
    ) -> None:
        transport, _peer = paired
        # Reach into the mock backend's fault plan; this is the documented
        # testing primitive for anyserial (see docs/design.md §5.1).
        transport._port._backend.faults.disconnected = True  # type: ignore[union-attr]
        with pytest.raises(AlicatTransportError) as ei:
            await transport.write(b"A\r", timeout=0.5)
        # Context includes the port label and the write-phase marker.
        assert ei.value.context.port == "/dev/mockA"
        assert ei.value.context.extra.get("phase") == "write"


# ---------------------------------------------------------------------------
# Label — should match the configured port path.
# ---------------------------------------------------------------------------


class TestLabel:
    def test_label_is_port_path(self) -> None:
        t = SerialTransport(SerialSettings(port="/dev/ttyUSB0"))
        assert t.label == "/dev/ttyUSB0"
