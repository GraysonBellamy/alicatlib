"""End-to-end: ``GAS_SELECT`` through ``AlicatProtocolClient`` + ``FakeTransport``.

No session, no device factory, no hardware. Just the byte path:

    encode → transport.write → transport.read_until → decode

If this test fails, one of the layer seams is broken; look at the narrower
tests first (`test_transport_fake`, `test_protocol_client`, `test_gas_select`).
"""

from __future__ import annotations

import pytest

from alicatlib.commands import GAS_SELECT, DecodeContext, GasSelectRequest
from alicatlib.errors import AlicatCommandRejectedError
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.registry import Gas
from alicatlib.transport import FakeTransport


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def ctx() -> DecodeContext:
    return DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))


class TestGasSelectRoundTrip:
    @pytest.mark.anyio
    async def test_query_then_decode(self, ctx: DecodeContext) -> None:
        """Query form round-trip: encode → write → read → decode."""
        fake = FakeTransport({b"AGS\r": b"A 8 N2 Nitrogen\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)

        cmd = GAS_SELECT.encode(ctx, GasSelectRequest())
        raw = await client.query_line(cmd)
        state = GAS_SELECT.decode(raw, ctx)

        assert fake.writes == (b"AGS\r",)
        assert state.gas is Gas.N2
        assert state.code == 8
        assert state.long_name == "Nitrogen"

    @pytest.mark.anyio
    async def test_set_persistent(self, ctx: DecodeContext) -> None:
        """Set form with ``save=True`` — device echoes the new active gas."""
        fake = FakeTransport({b"AGS 8 1\r": b"A 8 N2 Nitrogen\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)

        cmd = GAS_SELECT.encode(ctx, GasSelectRequest(gas="N2", save=True))
        raw = await client.query_line(cmd)
        state = GAS_SELECT.decode(raw, ctx)

        assert fake.writes == (b"AGS 8 1\r",)
        assert state.gas is Gas.N2

    @pytest.mark.anyio
    async def test_device_rejection_surfaces(self, ctx: DecodeContext) -> None:
        """A ``?`` rejection from the device stops decode from running."""
        fake = FakeTransport({b"AGS 999\r": b"A ?\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)

        cmd = b"AGS 999\r"  # handcrafted; a non-existent gas code
        with pytest.raises(AlicatCommandRejectedError):
            await client.query_line(cmd)

    @pytest.mark.anyio
    async def test_gp_prefix_reaches_transport(self) -> None:
        """End-to-end verification of the GP-firmware ``$$`` prefix path."""
        gp_ctx = DecodeContext(
            unit_id="A",
            firmware=FirmwareVersion.parse("GP"),
            command_prefix=b"$$",
        )
        fake = FakeTransport({b"A$$GS\r": b"A 8 N2 Nitrogen\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)

        cmd = GAS_SELECT.encode(gp_ctx, GasSelectRequest())
        raw = await client.query_line(cmd)
        state = GAS_SELECT.decode(raw, gp_ctx)

        assert fake.writes == (b"A$$GS\r",)
        assert state.gas is Gas.N2
