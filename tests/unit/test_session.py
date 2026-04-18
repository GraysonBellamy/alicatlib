"""Tests for :class:`alicatlib.devices.session.Session`.

The session is the one dispatch path for commands, so these tests pin:

- Pre-I/O gating (family, firmware range, device kind, capability,
  destructive-confirm) fails loudly and *before* the transport sees
  anything.
- Dispatch maps :class:`ResponseMode` correctly onto the
  :class:`AlicatProtocolClient` methods.
- Any :class:`AlicatError` raised from the I/O path is re-raised with
  the session's context (command, unit_id, port, firmware, elapsed_s).
- :meth:`refresh_firmware` / :meth:`refresh_data_frame_format` actually
  update the cached state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import (
    GAS_SELECT,
    MANUFACTURING_INFO,
    VE_QUERY,
    Capability,
    Command,
    DecodeContext,
    GasSelectRequest,
    GasState,
    ManufacturingInfoRequest,
    ResponseMode,
    VeRequest,
)
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.models import DeviceInfo
from alicatlib.devices.session import Session, validate_unit_id
from alicatlib.errors import (
    AlicatFirmwareError,
    AlicatMissingHardwareError,
    AlicatTimeoutError,
    AlicatUnsupportedCommandError,
    AlicatValidationError,
    InvalidUnitIdError,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.registry import Gas
from alicatlib.transport import FakeTransport
from tests._typing import approx

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.devices.data_frame import DataFrameFormat
    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _info(
    *,
    firmware: str = "10v05",
    kind: DeviceKind = DeviceKind.FLOW_CONTROLLER,
    media: Medium = Medium.GAS,
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
        software=firmware,
        firmware=FirmwareVersion.parse(firmware),
        firmware_date=date(2021, 5, 19),
        kind=kind,
        media=media,
        capabilities=capabilities,
    )


async def _make_session(
    script: Mapping[bytes, ScriptedReply] | None = None,
    *,
    firmware: str = "10v05",
    kind: DeviceKind = DeviceKind.FLOW_CONTROLLER,
    media: Medium = Medium.GAS,
    capabilities: Capability = Capability.NONE,
    unit_id: str = "A",
    data_frame_format: DataFrameFormat | None = None,
    port_label: str | None = "fake://test",
) -> tuple[Session, FakeTransport, AlicatProtocolClient]:
    fake = FakeTransport(script, label=port_label or "fake://test")
    await fake.open()
    # Short multiline idle — keeps LINES tests snappy when the script
    # emits fewer than expected_lines rows.
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    session = Session(
        client,
        unit_id=unit_id,
        info=_info(firmware=firmware, kind=kind, media=media, capabilities=capabilities),
        data_frame_format=data_frame_format,
        port_label=port_label,
    )
    return session, fake, client


# ---------------------------------------------------------------------------
# validate_unit_id
# ---------------------------------------------------------------------------


class TestValidateUnitId:
    def test_accepts_a_through_z(self) -> None:
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            assert validate_unit_id(ch) == ch

    def test_rejects_streaming_by_default(self) -> None:
        """`@` is the streaming unit id; polling sessions must not accept it."""
        with pytest.raises(InvalidUnitIdError):
            validate_unit_id("@")

    def test_accepts_streaming_when_allowed(self) -> None:
        assert validate_unit_id("@", allow_streaming=True) == "@"

    def test_rejects_lowercase(self) -> None:
        with pytest.raises(InvalidUnitIdError):
            validate_unit_id("a")

    def test_rejects_multi_character(self) -> None:
        with pytest.raises(InvalidUnitIdError):
            validate_unit_id("AB")

    def test_rejects_empty(self) -> None:
        with pytest.raises(InvalidUnitIdError):
            validate_unit_id("")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestSessionConstruction:
    @pytest.mark.anyio
    async def test_rejects_bad_unit_id_eagerly(self) -> None:
        fake = FakeTransport()
        await fake.open()
        client = AlicatProtocolClient(fake)
        with pytest.raises(InvalidUnitIdError):
            Session(client, unit_id="1", info=_info())

    @pytest.mark.anyio
    async def test_exposes_properties(self) -> None:
        session, _fake, _client = await _make_session()
        assert session.unit_id == "A"
        assert session.info.model == "MC-100SCCM-D"
        assert session.firmware == FirmwareVersion.parse("10v05")
        assert session.port_label == "fake://test"
        assert session.data_frame_format is None
        assert not session.closed


# ---------------------------------------------------------------------------
# Gating — firmware family
# ---------------------------------------------------------------------------


class TestFirmwareFamilyGating:
    @pytest.mark.anyio
    async def test_gp_rejected_for_v10_only_command(self) -> None:
        """GAS_SELECT is V10-only; a GP device must fail pre-I/O."""
        session, fake, _ = await _make_session(firmware="GP")
        with pytest.raises(AlicatFirmwareError) as ei:
            await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        assert ei.value.reason == "family_not_supported"
        # Critical: no I/O happened.
        assert fake.writes == ()

    @pytest.mark.anyio
    async def test_v10_allowed_for_v10_command(self) -> None:
        """Happy path — V10 device runs V10 command."""
        session, _, _ = await _make_session(
            script={b"AGS 8\r": b"A 8 N2 Nitrogen\r"},
            firmware="10v05",
        )
        result = await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        assert isinstance(result, GasState)
        assert result.gas is Gas.N2


# ---------------------------------------------------------------------------
# Gating — firmware range (min)
# ---------------------------------------------------------------------------


class TestFirmwareRangeGating:
    @pytest.mark.anyio
    async def test_firmware_too_old(self) -> None:
        """GAS_SELECT needs 10v05+; 10v04 must fail with firmware_too_old."""
        # Build a V10-family firmware just below the minimum.
        session, fake, _ = await _make_session(firmware="10v04")
        with pytest.raises(AlicatFirmwareError) as ei:
            await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        assert ei.value.reason == "firmware_too_old"
        assert fake.writes == ()

    @pytest.mark.anyio
    async def test_equal_to_min_allowed(self) -> None:
        """Exactly at min_firmware passes — range is inclusive."""
        session, _, _ = await _make_session(
            script={b"AGS 8\r": b"A 8 N2 Nitrogen\r"},
            firmware="10v05",
        )
        await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))


# ---------------------------------------------------------------------------
# Gating — device kind
# ---------------------------------------------------------------------------


class TestDeviceKindGating:
    @pytest.mark.anyio
    async def test_pressure_meter_rejected_from_gas_select(self) -> None:
        """GAS_SELECT is flow-only; a pressure meter must raise."""
        session, fake, _ = await _make_session(
            kind=DeviceKind.PRESSURE_METER,
        )
        with pytest.raises(AlicatUnsupportedCommandError):
            await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        assert fake.writes == ()


# ---------------------------------------------------------------------------
# Gating — medium (design §5.4, §5.9a)
# ---------------------------------------------------------------------------


class TestMediumGating:
    @pytest.mark.anyio
    async def test_gas_command_rejected_on_liquid_device(self) -> None:
        """GAS_SELECT is gas-only; a liquid-configured device must raise."""
        from alicatlib.errors import AlicatMediumMismatchError

        session, fake, _ = await _make_session(media=Medium.LIQUID)
        with pytest.raises(AlicatMediumMismatchError) as excinfo:
            await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        assert fake.writes == ()
        err = excinfo.value
        assert err.command == "gas_select"
        assert err.command_media is Medium.GAS
        assert err.device_media is Medium.LIQUID
        # Hint should point at the fluid() API.
        assert "device.fluid" in str(err)

    @pytest.mark.anyio
    async def test_gas_command_passes_on_gas_device(self) -> None:
        session, _fake, _ = await _make_session(
            script={b"AGS 8\r": b"A 8 N2 Nitrogen\r"},
            media=Medium.GAS,
        )
        result = await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        assert result.gas is Gas.N2

    @pytest.mark.anyio
    async def test_gas_command_passes_on_dual_medium_device(self) -> None:
        """CODA-style dual-medium device — GAS commands pass because the
        bitwise intersection of Medium.GAS with (Medium.GAS | Medium.LIQUID)
        is non-empty."""
        session, _fake, _ = await _make_session(
            script={b"AGS 8\r": b"A 8 N2 Nitrogen\r"},
            media=Medium.GAS | Medium.LIQUID,
        )
        result = await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        assert result.gas is Gas.N2

    @pytest.mark.anyio
    async def test_medium_none_rejects_gas_command(self) -> None:
        """Medium.NONE (unresolved) must refuse every medium-specific command."""
        from alicatlib.errors import AlicatMediumMismatchError

        session, fake, _ = await _make_session(media=Medium.NONE)
        with pytest.raises(AlicatMediumMismatchError):
            await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        assert fake.writes == ()


# ---------------------------------------------------------------------------
# Gating — capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BarometerRequest:
    pass


@dataclass(frozen=True, slots=True)
class _CapabilityProbeCommand(Command[_BarometerRequest, bytes]):
    name: str = "probe_barometer"
    token: str = "PB"
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = field(default_factory=lambda: frozenset(DeviceKind))
    required_capabilities: Capability = Capability.BAROMETER

    def encode(self, ctx: DecodeContext, request: _BarometerRequest) -> bytes:
        """Test encode — never reached when capability gate fires."""
        del request
        return f"{ctx.unit_id}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> bytes:
        """Test decode — passthrough."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError("expected single line")
        return response


class TestCapabilityGating:
    @pytest.mark.anyio
    async def test_missing_capability_rejected(self) -> None:
        session, fake, _ = await _make_session(capabilities=Capability.NONE)
        cmd = _CapabilityProbeCommand()
        with pytest.raises(AlicatMissingHardwareError):
            await session.execute(cmd, _BarometerRequest())
        assert fake.writes == ()

    @pytest.mark.anyio
    async def test_capability_present_allows_dispatch(self) -> None:
        session, fake, _ = await _make_session(
            script={b"APB\r": b"A ok\r"},
            capabilities=Capability.BAROMETER,
        )
        cmd = _CapabilityProbeCommand()
        result = await session.execute(cmd, _BarometerRequest())
        assert result == b"A ok"
        assert fake.writes == (b"APB\r",)


# ---------------------------------------------------------------------------
# Gating — destructive
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DestructiveRequest:
    confirm: bool = False


@dataclass(frozen=True, slots=True)
class _DestructiveCommand(Command[_DestructiveRequest, bytes]):
    name: str = "factory_reset"
    token: str = "FR"
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = field(default_factory=lambda: frozenset(DeviceKind))
    destructive: bool = True

    def encode(self, ctx: DecodeContext, request: _DestructiveRequest) -> bytes:
        """Test encode — never reached without confirm."""
        del request
        return f"{ctx.unit_id}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> bytes:
        """Test decode — passthrough."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError("expected single line")
        return response


class TestDestructiveGating:
    @pytest.mark.anyio
    async def test_destructive_without_confirm_rejected(self) -> None:
        session, fake, _ = await _make_session()
        cmd = _DestructiveCommand()
        with pytest.raises(AlicatValidationError):
            await session.execute(cmd, _DestructiveRequest(confirm=False))
        assert fake.writes == ()

    @pytest.mark.anyio
    async def test_destructive_with_confirm_allowed(self) -> None:
        session, fake, _ = await _make_session(
            script={b"AFR\r": b"A ok\r"},
        )
        cmd = _DestructiveCommand()
        await session.execute(cmd, _DestructiveRequest(confirm=True))
        assert fake.writes == (b"AFR\r",)

    @pytest.mark.anyio
    async def test_non_destructive_confirm_not_required(self) -> None:
        """Non-destructive commands never check confirm."""
        session, _, _ = await _make_session(
            script={b"AGS 8\r": b"A 8 N2 Nitrogen\r"},
        )
        await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))


# ---------------------------------------------------------------------------
# Dispatch — response_mode paths
# ---------------------------------------------------------------------------


class TestDispatch:
    @pytest.mark.anyio
    async def test_line_command(self) -> None:
        session, _, _ = await _make_session(
            script={b"AGS 8\r": b"A 8 N2 Nitrogen\r"},
        )
        result = await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        assert result.gas is Gas.N2

    @pytest.mark.anyio
    async def test_lines_command(self) -> None:
        """MANUFACTURING_INFO reads expected_lines=10 lines from the transport."""
        lines = b"".join(f"A M{i:02d} payload-{i}\r".encode("ascii") for i in range(1, 11))
        session, _, _ = await _make_session(
            script={b"A??M*\r": lines},
            # firmware family V8_V9+ required for ??M*
            firmware="10v05",
        )
        info = await session.execute(MANUFACTURING_INFO, ManufacturingInfoRequest())
        assert info.unit_id == "A"
        assert info.by_code[1] == "payload-1"
        assert info.by_code[10] == "payload-10"

    @pytest.mark.anyio
    async def test_none_mode_returns_none(self) -> None:
        """NONE-mode commands return None (convention — no decode call)."""

        @dataclass(frozen=True, slots=True)
        class _NoneRequest:
            pass

        @dataclass(frozen=True, slots=True)
        class _NoneCommand(Command[_NoneRequest, None]):
            name: str = "no_reply"
            token: str = "NR"
            response_mode: ResponseMode = ResponseMode.NONE
            device_kinds: frozenset[DeviceKind] = field(
                default_factory=lambda: frozenset(DeviceKind),
            )

            def encode(self, ctx: DecodeContext, request: _NoneRequest) -> bytes:
                """Test encode for NONE-mode command."""
                del request
                return f"{ctx.unit_id}{self.token}\r".encode("ascii")

            def decode(
                self,
                response: bytes | tuple[bytes, ...],
                ctx: DecodeContext,
            ) -> None:
                """Test decode — never called by session for NONE mode."""
                del response, ctx

        session, fake, _ = await _make_session()
        # execute() returns None for NONE-mode commands (typed as Resp=None).
        await session.execute(_NoneCommand(), _NoneRequest())
        assert fake.writes == (b"ANR\r",)

    @pytest.mark.anyio
    async def test_stream_mode_spec_rejected(self) -> None:
        """A command declaring ``ResponseMode.STREAM`` is a spec bug.

        Streaming is a port-level state transition owned by
        :class:`~alicatlib.devices.streaming.StreamingSession`, not a
        request/response command. If a spec slips through with
        ``ResponseMode.STREAM``, dispatch refuses loudly rather than
        silently returning None or hanging.
        """

        @dataclass(frozen=True, slots=True)
        class _StreamRequest:
            pass

        @dataclass(frozen=True, slots=True)
        class _StreamCommand(Command[_StreamRequest, None]):
            name: str = "stream_start"
            token: str = "SS"
            response_mode: ResponseMode = ResponseMode.STREAM
            device_kinds: frozenset[DeviceKind] = field(
                default_factory=lambda: frozenset(DeviceKind),
            )

            def encode(self, ctx: DecodeContext, request: _StreamRequest) -> bytes:
                """Test encode for STREAM-mode command."""
                del request
                return f"{ctx.unit_id}{self.token}\r".encode("ascii")

            def decode(
                self,
                response: bytes | tuple[bytes, ...],
                ctx: DecodeContext,
            ) -> None:
                """Test decode — unreachable in this test."""
                del response, ctx

        session, _, _ = await _make_session()
        with pytest.raises(RuntimeError, match=r"ResponseMode\.STREAM"):
            await session.execute(_StreamCommand(), _StreamRequest())


# ---------------------------------------------------------------------------
# DecodeContext construction
# ---------------------------------------------------------------------------


class TestDecodeContext:
    @pytest.mark.anyio
    async def test_gp_firmware_gets_double_dollar_prefix(self) -> None:
        """GP devices need `$$` between unit_id and token — session injects it."""
        session, fake, _ = await _make_session(
            script={b"A$$VE\r": b"A GP\r"},
            firmware="GP",
        )
        await session.execute(VE_QUERY, VeRequest())
        # Critical check: wire bytes include `$$`.
        assert fake.writes == (b"A$$VE\r",)

    @pytest.mark.anyio
    async def test_numeric_firmware_has_empty_prefix(self) -> None:
        session, fake, _ = await _make_session(
            script={b"AVE\r": b"A 10v05\r"},
            firmware="10v05",
        )
        await session.execute(VE_QUERY, VeRequest())
        assert fake.writes == (b"AVE\r",)


# ---------------------------------------------------------------------------
# Error enrichment
# ---------------------------------------------------------------------------


class TestErrorEnrichment:
    @pytest.mark.anyio
    async def test_timeout_enriched_with_session_context(self) -> None:
        """An I/O timeout surfaces with the session's command / unit_id / port / firmware."""
        session, _, _ = await _make_session(script={})  # no scripted reply → timeout
        with pytest.raises(AlicatTimeoutError) as ei:
            await session.execute(GAS_SELECT, GasSelectRequest(gas=Gas.N2))
        ctx = ei.value.context
        assert ctx.command_name == "gas_select"
        assert ctx.unit_id == "A"
        assert ctx.port == "fake://test"
        assert ctx.firmware == FirmwareVersion.parse("10v05")
        assert ctx.elapsed_s is not None
        assert ctx.elapsed_s >= 0.0


# ---------------------------------------------------------------------------
# Refresh methods
# ---------------------------------------------------------------------------


class TestRefresh:
    @pytest.mark.anyio
    async def test_refresh_firmware_updates_info(self) -> None:
        """After refresh, session.info.firmware reflects the VE response."""
        session, _, _ = await _make_session(
            script={b"AVE\r": b"A 10v06 2022-06-01\r"},
            firmware="10v05",
        )
        new_fw = await session.refresh_firmware()
        assert new_fw == FirmwareVersion(FirmwareFamily.V10, 10, 6, "10v06")
        assert session.info.firmware == new_fw
        assert session.info.firmware_date == date(2022, 6, 1)

    @pytest.mark.anyio
    async def test_refresh_data_frame_format_updates_cache(self) -> None:
        header = (
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________\r"
        )
        lines = b"".join(
            [
                header,
                b"A D01 700 Unit ID                    string          1\r",
                b"A D02 005 Mass Flow                  s decimal     7/2 012 02 SCCM\r",
            ]
        )
        session, _, _ = await _make_session(
            script={b"A??D*\r": lines},
        )
        # Session is constructed with data_frame_format=None (default).
        fmt = await session.refresh_data_frame_format()
        assert fmt.names() == ("Unit_ID", "Mass_Flow")
        # Refresh populates the cache with the returned format.
        assert session.data_frame_format is fmt

    @pytest.mark.anyio
    async def test_refresh_capabilities_raises_until_factory_wires_it(self) -> None:
        """refresh_capabilities is reserved API; factory implements it."""
        session, _, _ = await _make_session()
        with pytest.raises(NotImplementedError):
            await session.refresh_capabilities()


# ---------------------------------------------------------------------------
# Execute → refresh_data_frame_format lazy path
# ---------------------------------------------------------------------------


class TestPollConvenience:
    @pytest.mark.anyio
    async def test_poll_reuses_cached_format(self) -> None:
        """If format is already cached, poll issues only A\\r — no ??D*."""
        fmt = await _sample_format()
        session, fake, _ = await _make_session(
            script={b"A\r": b"A 14.7 25.5 50.0\r"},
            data_frame_format=fmt,
        )
        frame = await session.poll()
        assert frame.unit_id == "A"
        assert frame.values["Mass_Flow"] == approx(25.5)
        assert fake.writes == (b"A\r",)

    @pytest.mark.anyio
    async def test_poll_probes_format_if_missing(self) -> None:
        """First poll() on a fresh session probes ??D* before the poll itself."""
        df_response = (
            b"A D00 ID_ NAME______________________ TYPE_______ "
            b"WIDTH NOTES___________________\r"
            b"A D01 700 Unit ID                    string          1\r"
            b"A D02 005 Mass Flow                  s decimal     7/2 012 02 SCCM\r"
        )
        session, fake, _ = await _make_session(
            script={
                b"A??D*\r": df_response,
                b"A\r": b"A 25.5\r",
            },
        )
        frame = await session.poll()
        # Both the format probe and the poll itself went to the wire — in order.
        assert fake.writes[0] == b"A??D*\r"
        assert fake.writes[1] == b"A\r"
        # The poll line `A 25.5` has only 2 tokens; first token is unit_id,
        # second is Mass_Flow (the only required-field after Unit_ID).
        assert frame.values["Mass_Flow"] == approx(25.5)


async def _sample_format() -> DataFrameFormat:
    """Parse a small ??D* fixture into a DataFrameFormat for poll tests."""
    from alicatlib.protocol.parser import parse_data_frame_table

    return parse_data_frame_table(
        [
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________",
            b"A D01 700 Unit ID                    string          1",
            b"A D02 002 Abs Press                  s decimal     7/2 010 02 PSIA",
            b"A D03 005 Mass Flow                  s decimal     7/2 012 02 SCCM",
            b"A D04 037 Mass Flow Setpt            s decimal     7/2 012 02 SCCM",
        ]
    )


# ---------------------------------------------------------------------------
# Close / lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.anyio
    async def test_close_marks_closed(self) -> None:
        session, _, _ = await _make_session()
        assert not session.closed
        await session.close()
        assert session.closed

    @pytest.mark.anyio
    async def test_close_does_not_touch_transport(self) -> None:
        """Session doesn't own the transport; close() is a state flag only."""
        session, fake, _ = await _make_session()
        await session.close()
        assert fake.is_open  # transport lifecycle is the factory's concern


# ---------------------------------------------------------------------------
# Unused-port guard — make sure _make_session returns the right wiring
# ---------------------------------------------------------------------------


class TestHelperFixture:
    """Belt-and-braces: the test helper itself wires the session correctly."""

    @pytest.mark.anyio
    async def test_session_is_wired_to_client_is_wired_to_fake(self) -> None:
        session, fake, client = await _make_session()
        # The public session surface should reflect our inputs.
        assert session.unit_id == "A"
        assert fake.is_open
        assert client.idle_timeout_exits == 0
