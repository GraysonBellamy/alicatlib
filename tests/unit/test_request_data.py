"""Tests for :data:`alicatlib.commands.polling.REQUEST_DATA` (``DV``).

The wire shape is pinned by
``tests/fixtures/responses/request_data_dv.txt`` — captured 2026-04-17
against MC-5SLPM-D / 10v20.0-R24. Notable properties exercised here:

- reply has **no unit-id prefix** (unique in the catalog);
- ``--`` sentinel maps to ``None`` per-slot;
- averaging-ms and statistics-count are validated pre-I/O;
- :meth:`Device.request` zips wire values with the requested
  statistics into a :class:`MeasurementSet`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import (
    REQUEST_DATA,
    DecodeContext,
    RequestDataRequest,
    ResponseMode,
)
from alicatlib.commands.polling import RequestData
from alicatlib.devices import DeviceKind
from alicatlib.devices.base import Device
from alicatlib.devices.models import DeviceInfo, MeasurementSet
from alicatlib.devices.session import Session
from alicatlib.errors import AlicatParseError, AlicatValidationError
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.registry import Statistic
from alicatlib.testing import parse_fixture
from alicatlib.transport import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "responses"
_DV_FIXTURE = _FIXTURES_DIR / "request_data_dv.txt"


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def ctx_v10() -> DecodeContext:
    return DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v20"))


@pytest.fixture
def ctx_gp() -> DecodeContext:
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("GP"),
        command_prefix=b"$$",
    )


# ---------------------------------------------------------------------------
# Command-spec metadata
# ---------------------------------------------------------------------------


class TestRequestDataSpec:
    def test_token_and_name(self) -> None:
        assert REQUEST_DATA.token == "DV"
        assert REQUEST_DATA.name == "request_data"
        assert REQUEST_DATA.response_mode is ResponseMode.LINE

    def test_applies_to_every_device_kind(self) -> None:
        assert REQUEST_DATA.device_kinds == frozenset(DeviceKind)

    def test_firmware_families_numeric_only(self) -> None:
        """GP is intentionally excluded (no capture yet)."""
        assert REQUEST_DATA.firmware_families == frozenset(
            {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10},
        )

    def test_singleton_is_instance(self) -> None:
        assert isinstance(REQUEST_DATA, RequestData)


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------


class TestRequestDataEncode:
    def test_single_statistic_by_enum(self, ctx_v10: DecodeContext) -> None:
        out = REQUEST_DATA.encode(
            ctx_v10,
            RequestDataRequest(statistics=[Statistic.ABS_PRESS], averaging_ms=10),
        )
        assert out == b"ADV 10 2\r"

    def test_single_statistic_by_string(self, ctx_v10: DecodeContext) -> None:
        out = REQUEST_DATA.encode(
            ctx_v10,
            RequestDataRequest(statistics=["abs_press"], averaging_ms=10),
        )
        assert out == b"ADV 10 2\r"

    def test_multiple_statistics_preserves_order(self, ctx_v10: DecodeContext) -> None:
        """Five-stat fixture row: abs_press / temp / vol_flow / mass_flow / mass_flow_setpt."""
        out = REQUEST_DATA.encode(
            ctx_v10,
            RequestDataRequest(
                statistics=[
                    Statistic.ABS_PRESS,
                    Statistic.TEMP_STREAM,
                    Statistic.VOL_FLOW,
                    Statistic.MASS_FLOW,
                    Statistic.MASS_FLOW_SETPT,
                ],
                averaging_ms=2,
            ),
        )
        assert out == b"ADV 2 2 3 4 5 37\r"

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        out = REQUEST_DATA.encode(
            ctx_gp,
            RequestDataRequest(statistics=[Statistic.ABS_PRESS], averaging_ms=50),
        )
        assert out == b"A$$DV 50 2\r"

    def test_encode_rejects_zero_averaging(self, ctx_v10: DecodeContext) -> None:
        """Device rejects ``0`` on the wire — surface a typed error pre-I/O."""
        with pytest.raises(AlicatValidationError, match="averaging_ms"):
            REQUEST_DATA.encode(
                ctx_v10,
                RequestDataRequest(statistics=[Statistic.ABS_PRESS], averaging_ms=0),
            )

    def test_encode_rejects_negative_averaging(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError, match="averaging_ms"):
            REQUEST_DATA.encode(
                ctx_v10,
                RequestDataRequest(statistics=[Statistic.ABS_PRESS], averaging_ms=-1),
            )

    def test_encode_rejects_averaging_above_9999(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError, match="averaging_ms"):
            REQUEST_DATA.encode(
                ctx_v10,
                RequestDataRequest(statistics=[Statistic.ABS_PRESS], averaging_ms=10_000),
            )

    def test_encode_rejects_empty_statistics(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError, match="statistics count"):
            REQUEST_DATA.encode(
                ctx_v10,
                RequestDataRequest(statistics=[], averaging_ms=10),
            )

    def test_encode_rejects_more_than_13_statistics(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError, match="statistics count"):
            REQUEST_DATA.encode(
                ctx_v10,
                RequestDataRequest(
                    statistics=[Statistic.ABS_PRESS] * 14,
                    averaging_ms=10,
                ),
            )


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------


class TestRequestDataDecode:
    def test_single_value(self, ctx_v10: DecodeContext) -> None:
        values = REQUEST_DATA.decode(b"+014.63", ctx_v10)
        assert values == (14.63,)

    def test_multi_value(self, ctx_v10: DecodeContext) -> None:
        values = REQUEST_DATA.decode(b"+014.63 +000.06", ctx_v10)
        assert values == (14.63, 0.06)

    def test_five_values(self, ctx_v10: DecodeContext) -> None:
        values = REQUEST_DATA.decode(b"+014.63 +023.61 +000.02 +000.02 +078.94", ctx_v10)
        assert values == (14.63, 23.61, 0.02, 0.02, 78.94)

    def test_absent_token_maps_to_none(self, ctx_v10: DecodeContext) -> None:
        """Per-slot ``--`` sentinel surfaces as ``None``."""
        values = REQUEST_DATA.decode(b"--", ctx_v10)
        assert values == (None,)

    def test_mixed_absent_and_value(self, ctx_v10: DecodeContext) -> None:
        values = REQUEST_DATA.decode(b"+014.63 -- +000.02", ctx_v10)
        assert values == (14.63, None, 0.02)

    def test_wide_dash_sentinel(self, ctx_v10: DecodeContext) -> None:
        """Real hardware pads the absent sentinel to the column width.

        Observed on 2026-04-17 on MW-10SLPM-D @ 10v04.0-R24:
        ``DV`` with ``MASS_FLOW_SETPT`` (controller-only statistic) on
        a meter returns ``-------`` (seven dashes, matching the
        setpoint column's 7/2 decimal width). Primer only documented
        ``--``. The parser treats any pure-dash run as the sentinel.
        """
        values = REQUEST_DATA.decode(b"+014.63 ------- +000.02", ctx_v10)
        assert values == (14.63, None, 0.02)

    def test_mixed_dash_widths(self, ctx_v10: DecodeContext) -> None:
        """Different absent statistics can use different sentinel widths."""
        values = REQUEST_DATA.decode(b"-- +001.00 ------- ---", ctx_v10)
        assert values == (None, 1.0, None, None)

    def test_rejects_multiline(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(TypeError):
            REQUEST_DATA.decode((b"+014.63",), ctx_v10)

    def test_empty_reply_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError, match="empty reply"):
            REQUEST_DATA.decode(b"", ctx_v10)

    def test_non_numeric_non_sentinel_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            REQUEST_DATA.decode(b"oops", ctx_v10)


# ---------------------------------------------------------------------------
# Fixture round-trip
# ---------------------------------------------------------------------------


class TestRequestDataFixture:
    """Replay every documented DV exchange from the captured fixture."""

    def test_single_statistic_row(self, ctx_v10: DecodeContext) -> None:
        script = parse_fixture(_DV_FIXTURE)
        reply = script[b"ADV 10 2\r"].rstrip(b"\r")
        assert REQUEST_DATA.decode(reply, ctx_v10) == (14.63,)

    def test_two_statistic_row(self, ctx_v10: DecodeContext) -> None:
        script = parse_fixture(_DV_FIXTURE)
        reply = script[b"ADV 500 2 5\r"].rstrip(b"\r")
        assert REQUEST_DATA.decode(reply, ctx_v10) == (14.63, 0.06)

    def test_five_statistic_row(self, ctx_v10: DecodeContext) -> None:
        script = parse_fixture(_DV_FIXTURE)
        reply = script[b"ADV 2 2 3 4 5 37\r"].rstrip(b"\r")
        assert REQUEST_DATA.decode(reply, ctx_v10) == (14.63, 23.61, 0.02, 0.02, 78.94)

    def test_invalid_statistic_slot_is_none(self, ctx_v10: DecodeContext) -> None:
        script = parse_fixture(_DV_FIXTURE)
        reply = script[b"ADV 2 99\r"].rstrip(b"\r")
        assert REQUEST_DATA.decode(reply, ctx_v10) == (None,)


# ---------------------------------------------------------------------------
# Device.request() facade
# ---------------------------------------------------------------------------


def _info() -> DeviceInfo:
    from datetime import date

    from alicatlib.commands import Capability
    from alicatlib.devices import Medium

    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model="MC-5SLPM-D",
        serial="123456",
        manufactured="2024-01-01",
        calibrated="2024-02-01",
        calibrated_by="ACS",
        software="10v20",
        firmware=FirmwareVersion.parse("10v20"),
        firmware_date=date(2024, 1, 1),
        kind=DeviceKind.FLOW_CONTROLLER,
        media=Medium.GAS,
        capabilities=Capability.NONE,
    )


async def _make_session(script: Mapping[bytes, ScriptedReply]) -> Session:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    return Session(client, unit_id="A", info=_info())


class TestDeviceRequestFacade:
    @pytest.mark.anyio
    async def test_request_single_statistic(self) -> None:
        session = await _make_session({b"ADV 10 2\r": b"+014.63\r"})
        dev = Device(session)
        before = datetime.now(UTC)
        result = await dev.request([Statistic.ABS_PRESS], averaging_ms=10)
        after = datetime.now(UTC)

        assert isinstance(result, MeasurementSet)
        assert result.unit_id == "A"
        assert result.averaging_ms == 10
        assert result.values == {Statistic.ABS_PRESS: 14.63}
        assert before <= result.received_at <= after

    @pytest.mark.anyio
    async def test_request_multi_statistic_zip(self) -> None:
        session = await _make_session({b"ADV 500 2 5\r": b"+014.63 +000.06\r"})
        dev = Device(session)
        result = await dev.request(
            [Statistic.ABS_PRESS, Statistic.MASS_FLOW],
            averaging_ms=500,
        )
        assert result.values == {Statistic.ABS_PRESS: 14.63, Statistic.MASS_FLOW: 0.06}

    @pytest.mark.anyio
    async def test_request_device_rejected_slot_is_none(self) -> None:
        """A ``--`` slot (device doesn't support this statistic) → ``None``.

        The fixture captures the wire behavior: per-slot ``--`` — *not*
        a command-level ``?`` rejection. A statistic the wire understands
        but this device can't produce (peak values on a unit without
        the peak-tracking feature, for example) comes back as ``--``.
        """
        # ABS_PRESS_PEAK is code 98; script the device to reject it per-slot.
        session = await _make_session({b"ADV 2 98\r": b"--\r"})
        dev = Device(session)
        result = await dev.request([Statistic.ABS_PRESS_PEAK], averaging_ms=2)
        assert result.values == {Statistic.ABS_PRESS_PEAK: None}

    @pytest.mark.anyio
    async def test_request_accepts_string_aliases(self) -> None:
        session = await _make_session({b"ADV 10 2 5\r": b"+014.63 +000.06\r"})
        dev = Device(session)
        result = await dev.request(["abs_press", "mass_flow"], averaging_ms=10)
        assert Statistic.ABS_PRESS in result.values
        assert Statistic.MASS_FLOW in result.values

    @pytest.mark.anyio
    async def test_request_validation_fails_pre_io(self) -> None:
        """No transport reads should happen when validation fails."""
        fake = FakeTransport({}, label="fake://test")
        await fake.open()
        client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
        session = Session(client, unit_id="A", info=_info())
        dev = Device(session)

        with pytest.raises(AlicatValidationError):
            await dev.request([Statistic.ABS_PRESS], averaging_ms=0)

        assert fake.writes == ()
