"""Tests for :mod:`alicatlib.commands.data_readings` — DCZ, DCA, DCFRP, DCFRT."""

from __future__ import annotations

import pytest

from alicatlib.commands import (
    AVERAGE_TIMING,
    STP_NTP_PRESSURE,
    STP_NTP_TEMPERATURE,
    ZERO_BAND,
    AverageTimingRequest,
    DecodeContext,
    StpNtpPressureRequest,
    StpNtpTemperatureRequest,
    ZeroBandRequest,
)
from alicatlib.commands.data_readings import DCZ_MAX_ZERO_BAND
from alicatlib.devices.models import StpNtpMode
from alicatlib.errors import AlicatParseError, AlicatValidationError
from alicatlib.firmware import FirmwareVersion


@pytest.fixture
def ctx_v10() -> DecodeContext:
    return DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))


# ---------------------------------------------------------------------------
# DCZ (zero band)
# ---------------------------------------------------------------------------


class TestZeroBandEncode:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert ZERO_BAND.encode(ctx_v10, ZeroBandRequest()) == b"ADCZ\r"

    def test_set(self, ctx_v10: DecodeContext) -> None:
        """Primer's literal ``0`` placeholder before zero_band."""
        assert ZERO_BAND.encode(ctx_v10, ZeroBandRequest(zero_band=0.5)) == b"ADCZ 0 0.5\r"

    def test_set_zero_disables(self, ctx_v10: DecodeContext) -> None:
        assert ZERO_BAND.encode(ctx_v10, ZeroBandRequest(zero_band=0.0)) == b"ADCZ 0 0.0\r"

    def test_negative_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            ZERO_BAND.encode(ctx_v10, ZeroBandRequest(zero_band=-0.1))

    def test_above_max_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            ZERO_BAND.encode(ctx_v10, ZeroBandRequest(zero_band=DCZ_MAX_ZERO_BAND + 0.1))


class TestZeroBandDecode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        state = ZERO_BAND.decode(b"A 0 0.5", ctx_v10)
        assert state.unit_id == "A"
        assert state.zero_band == 0.5

    def test_bad_field_count_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            ZERO_BAND.decode(b"A 0.5", ctx_v10)


# ---------------------------------------------------------------------------
# DCA (average timing)
# ---------------------------------------------------------------------------


class TestAverageTimingEncode:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert AVERAGE_TIMING.encode(ctx_v10, AverageTimingRequest(statistic_code=5)) == b"ADCA 5\r"

    def test_set(self, ctx_v10: DecodeContext) -> None:
        assert (
            AVERAGE_TIMING.encode(
                ctx_v10,
                AverageTimingRequest(statistic_code=5, averaging_ms=100),
            )
            == b"ADCA 5 100\r"
        )

    def test_rejects_invalid_statistic_code(self, ctx_v10: DecodeContext) -> None:
        """Primer allows only a specific set of statistic codes."""
        with pytest.raises(AlicatValidationError):
            AVERAGE_TIMING.encode(ctx_v10, AverageTimingRequest(statistic_code=999, averaging_ms=0))

    def test_rejects_negative_averaging(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            AVERAGE_TIMING.encode(
                ctx_v10,
                AverageTimingRequest(statistic_code=5, averaging_ms=-1),
            )

    def test_rejects_too_large_averaging(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            AVERAGE_TIMING.encode(
                ctx_v10,
                AverageTimingRequest(statistic_code=5, averaging_ms=10_000),
            )


class TestAverageTimingDecode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        state = AVERAGE_TIMING.decode(b"A 5 100", ctx_v10)
        assert state.statistic_code == 5
        assert state.averaging_ms == 100

    def test_real_hardware_short_reply(self, ctx_v10: DecodeContext) -> None:
        """Real 10v20 firmware drops the ``<stat>`` echo on DCA replies.

        Observed on 2026-04-17 on MC-500SCCM-D @ 10v20.0-R24:
        query ``ADCA 5`` returns ``A 1\\r`` — 2 tokens, no statistic
        echo. The decoder must accept the shorter shape; the facade
        re-populates ``statistic_code`` from the request (see
        :meth:`Device.average_timing`).
        """
        state = AVERAGE_TIMING.decode(b"A 1", ctx_v10)
        # statistic_code is the placeholder 0 — the facade re-fills it.
        assert state.statistic_code == 0
        assert state.averaging_ms == 1


# ---------------------------------------------------------------------------
# DCFRP / DCFRT (STP/NTP references)
# ---------------------------------------------------------------------------


class TestStpNtpPressureEncode:
    def test_query_stp(self, ctx_v10: DecodeContext) -> None:
        assert (
            STP_NTP_PRESSURE.encode(ctx_v10, StpNtpPressureRequest(mode=StpNtpMode.STP))
            == b"ADCFRP S\r"
        )

    def test_query_ntp(self, ctx_v10: DecodeContext) -> None:
        assert (
            STP_NTP_PRESSURE.encode(ctx_v10, StpNtpPressureRequest(mode=StpNtpMode.NTP))
            == b"ADCFRP N\r"
        )

    def test_set_keeps_units_when_code_is_none(self, ctx_v10: DecodeContext) -> None:
        """``unit_code=None`` emits the primer's "0 → keep current units" sentinel."""
        assert (
            STP_NTP_PRESSURE.encode(
                ctx_v10,
                StpNtpPressureRequest(mode=StpNtpMode.STP, pressure=14.696),
            )
            == b"ADCFRP S 0 14.696\r"
        )

    def test_set_with_explicit_unit_code(self, ctx_v10: DecodeContext) -> None:
        assert (
            STP_NTP_PRESSURE.encode(
                ctx_v10,
                StpNtpPressureRequest(mode=StpNtpMode.STP, pressure=1.0, unit_code=7),
            )
            == b"ADCFRP S 7 1.0\r"
        )


class TestStpNtpPressureDecode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        state = STP_NTP_PRESSURE.decode(b"A 14.696 2 PSIA", ctx_v10)
        assert state.pressure == 14.696
        assert state.unit_code == 2
        assert state.unit_label == "PSIA"


class TestStpNtpTemperatureEncode:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert (
            STP_NTP_TEMPERATURE.encode(ctx_v10, StpNtpTemperatureRequest(mode=StpNtpMode.NTP))
            == b"ADCFRT N\r"
        )

    def test_set(self, ctx_v10: DecodeContext) -> None:
        assert (
            STP_NTP_TEMPERATURE.encode(
                ctx_v10,
                StpNtpTemperatureRequest(mode=StpNtpMode.NTP, temperature=20.0, unit_code=3),
            )
            == b"ADCFRT N 3 20.0\r"
        )


class TestStpNtpTemperatureDecode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        state = STP_NTP_TEMPERATURE.decode(b"A 25.0 2 C", ctx_v10)
        assert state.temperature == 25.0
        assert state.unit_label == "C"
