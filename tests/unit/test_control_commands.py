"""Tests for :mod:`alicatlib.commands.control` — ``SR``, ``LCDB``."""

from __future__ import annotations

import pytest

from alicatlib.commands import (
    DEADBAND_LIMIT,
    RAMP_RATE,
    DeadbandLimitRequest,
    DecodeContext,
    RampRateRequest,
)
from alicatlib.devices.models import TimeUnit
from alicatlib.errors import AlicatParseError, AlicatValidationError
from alicatlib.firmware import FirmwareVersion


@pytest.fixture
def ctx_v10() -> DecodeContext:
    return DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))


@pytest.fixture
def ctx_gp() -> DecodeContext:
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("10v05"),
        command_prefix=b"$$",
    )


# ---------------------------------------------------------------------------
# RAMP_RATE (``SR``)
# ---------------------------------------------------------------------------


class TestRampRateEncode:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert RAMP_RATE.encode(ctx_v10, RampRateRequest()) == b"ASR\r"

    def test_set_basic(self, ctx_v10: DecodeContext) -> None:
        assert (
            RAMP_RATE.encode(
                ctx_v10,
                RampRateRequest(max_ramp=25.0, time_unit=TimeUnit.SECOND),
            )
            == b"ASR 25.0 4\r"
        )

    def test_set_disable_keeps_time_unit(self, ctx_v10: DecodeContext) -> None:
        """Primer p. 15: time_unit is required even when disabling ramping."""
        assert (
            RAMP_RATE.encode(
                ctx_v10,
                RampRateRequest(max_ramp=0.0, time_unit=TimeUnit.SECOND),
            )
            == b"ASR 0.0 4\r"
        )

    def test_set_without_time_unit_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            RAMP_RATE.encode(
                ctx_v10,
                RampRateRequest(max_ramp=25.0),
            )

    def test_negative_max_ramp_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            RAMP_RATE.encode(
                ctx_v10,
                RampRateRequest(max_ramp=-1.0, time_unit=TimeUnit.SECOND),
            )

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        assert (
            RAMP_RATE.encode(
                ctx_gp,
                RampRateRequest(max_ramp=1.0, time_unit=TimeUnit.MILLISECOND),
            )
            == b"A$$SR 1.0 3\r"
        )


class TestRampRateDecode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        """Five-field primer reply: ``<uid> <ramp> <unit_code> <time_code> <label>``."""
        state = RAMP_RATE.decode(b"A 25.0 12 4 SCCM/s", ctx_v10)
        assert state.unit_id == "A"
        assert state.max_ramp == 25.0
        assert state.setpoint_unit_code == 12
        assert state.time_unit is TimeUnit.SECOND
        assert state.rate_unit_label == "SCCM/s"

    def test_disabled_ramp(self, ctx_v10: DecodeContext) -> None:
        state = RAMP_RATE.decode(b"A 0.0 12 4 SCCM/s", ctx_v10)
        assert state.max_ramp == 0.0

    def test_unknown_time_code_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            RAMP_RATE.decode(b"A 25.0 12 99 SCCM/foo", ctx_v10)

    def test_bad_field_count_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            RAMP_RATE.decode(b"A 25.0 12 4", ctx_v10)


# ---------------------------------------------------------------------------
# DEADBAND_LIMIT (``LCDB``)
# ---------------------------------------------------------------------------


class TestDeadbandEncode:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert DEADBAND_LIMIT.encode(ctx_v10, DeadbandLimitRequest()) == b"ALCDB\r"

    def test_set_volatile(self, ctx_v10: DecodeContext) -> None:
        """save=None or save=False emits ``0`` in the save slot."""
        assert (
            DEADBAND_LIMIT.encode(ctx_v10, DeadbandLimitRequest(deadband=0.5)) == b"ALCDB 0 0.5\r"
        )
        assert (
            DEADBAND_LIMIT.encode(ctx_v10, DeadbandLimitRequest(deadband=0.5, save=False))
            == b"ALCDB 0 0.5\r"
        )

    def test_set_persisted(self, ctx_v10: DecodeContext) -> None:
        """save=True emits ``1`` in the save slot — feeds through the EEPROM guard."""
        assert (
            DEADBAND_LIMIT.encode(ctx_v10, DeadbandLimitRequest(deadband=0.5, save=True))
            == b"ALCDB 1 0.5\r"
        )

    def test_set_disable(self, ctx_v10: DecodeContext) -> None:
        """deadband=0 is a valid "disable" setting — distinct from None query form."""
        assert (
            DEADBAND_LIMIT.encode(ctx_v10, DeadbandLimitRequest(deadband=0.0)) == b"ALCDB 0 0.0\r"
        )

    def test_negative_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            DEADBAND_LIMIT.encode(ctx_v10, DeadbandLimitRequest(deadband=-0.1))

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        assert (
            DEADBAND_LIMIT.encode(ctx_gp, DeadbandLimitRequest(deadband=0.5)) == b"A$$LCDB 0 0.5\r"
        )


class TestDeadbandDecode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        state = DEADBAND_LIMIT.decode(b"A 0.5 2 PSIA", ctx_v10)
        assert state.unit_id == "A"
        assert state.deadband == 0.5
        assert state.unit_code == 2
        assert state.unit_label == "PSIA"

    def test_disabled(self, ctx_v10: DecodeContext) -> None:
        state = DEADBAND_LIMIT.decode(b"A 0.0 2 PSIA", ctx_v10)
        assert state.deadband == 0.0

    def test_bad_field_count_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            DEADBAND_LIMIT.decode(b"A 0.5", ctx_v10)
