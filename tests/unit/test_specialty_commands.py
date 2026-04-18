"""Tests for specialty command modules: output, display, user_data, ZCA/ZCP."""

from __future__ import annotations

import pytest

from alicatlib.commands import (
    ANALOG_OUTPUT_SOURCE,
    AUTO_TARE,
    BLINK_DISPLAY,
    LOCK_DISPLAY,
    POWER_UP_TARE,
    UNLOCK_DISPLAY,
    USER_DATA,
    AnalogOutputSourceRequest,
    AutoTareRequest,
    BlinkDisplayRequest,
    DecodeContext,
    LockDisplayRequest,
    PowerUpTareRequest,
    UnlockDisplayRequest,
    UserDataRequest,
)
from alicatlib.commands.tare import ZCA_DELAY_MAX_S, ZCA_DELAY_MIN_S
from alicatlib.commands.user_data import UD_MAX_VALUE_LEN
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.devices.models import AnalogOutputChannel
from alicatlib.errors import AlicatParseError, AlicatValidationError
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import Statistic


def _mini_format() -> DataFrameFormat:
    """Frame format with just ``Unit_ID`` + one numeric — enough to parse L / U replies."""

    def _text(v: str) -> float | str | None:
        return v

    def _decimal(v: str) -> float | str | None:
        return parse_optional_float(v, field="decimal")

    return DataFrameFormat(
        fields=(
            DataFrameField(
                name="Unit_ID",
                raw_name="Unit_ID",
                type_name="text",
                statistic=Statistic.NONE,
                unit=None,
                conditional=False,
                parser=_text,
            ),
            DataFrameField(
                name="Mass_Flow",
                raw_name="Mass_Flow",
                type_name="decimal",
                statistic=Statistic.MASS_FLOW,
                unit=None,
                conditional=False,
                parser=_decimal,
            ),
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


@pytest.fixture
def ctx_v10() -> DecodeContext:
    return DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))


@pytest.fixture
def ctx_v10_with_format() -> DecodeContext:
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("10v05"),
        data_frame_format=_mini_format(),
    )


# ---------------------------------------------------------------------------
# ASOCV
# ---------------------------------------------------------------------------


class TestAnalogOutputSource:
    def test_encode_query_primary(self, ctx_v10: DecodeContext) -> None:
        assert (
            ANALOG_OUTPUT_SOURCE.encode(
                ctx_v10,
                AnalogOutputSourceRequest(channel=AnalogOutputChannel.PRIMARY),
            )
            == b"AASOCV 0\r"
        )

    def test_encode_query_secondary(self, ctx_v10: DecodeContext) -> None:
        assert (
            ANALOG_OUTPUT_SOURCE.encode(
                ctx_v10,
                AnalogOutputSourceRequest(channel=AnalogOutputChannel.SECONDARY),
            )
            == b"AASOCV 1\r"
        )

    def test_encode_set_statistic(self, ctx_v10: DecodeContext) -> None:
        assert (
            ANALOG_OUTPUT_SOURCE.encode(
                ctx_v10,
                AnalogOutputSourceRequest(
                    channel=AnalogOutputChannel.PRIMARY, value=5, unit_code=7
                ),
            )
            == b"AASOCV 0 5 7\r"
        )

    def test_encode_set_min_sentinel(self, ctx_v10: DecodeContext) -> None:
        """``value=0`` pins the output at its minimum — distinct from query."""
        assert (
            ANALOG_OUTPUT_SOURCE.encode(
                ctx_v10,
                AnalogOutputSourceRequest(channel=AnalogOutputChannel.PRIMARY, value=0),
            )
            == b"AASOCV 0 0\r"
        )

    def test_rejects_negative_value(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            ANALOG_OUTPUT_SOURCE.encode(
                ctx_v10,
                AnalogOutputSourceRequest(channel=AnalogOutputChannel.PRIMARY, value=-1),
            )

    def test_decode(self, ctx_v10: DecodeContext) -> None:
        state = ANALOG_OUTPUT_SOURCE.decode(b"A 5 12 SCCM", ctx_v10)
        assert state.value == 5
        assert state.unit_code == 12
        assert state.unit_label == "SCCM"


# ---------------------------------------------------------------------------
# FFP / L / U
# ---------------------------------------------------------------------------


class TestBlinkDisplay:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert BLINK_DISPLAY.encode(ctx_v10, BlinkDisplayRequest()) == b"AFFP\r"

    def test_set_duration(self, ctx_v10: DecodeContext) -> None:
        assert BLINK_DISPLAY.encode(ctx_v10, BlinkDisplayRequest(duration_s=5)) == b"AFFP 5\r"

    def test_stop(self, ctx_v10: DecodeContext) -> None:
        assert BLINK_DISPLAY.encode(ctx_v10, BlinkDisplayRequest(duration_s=0)) == b"AFFP 0\r"

    def test_flash_indefinitely(self, ctx_v10: DecodeContext) -> None:
        """``-1`` is the documented "flash forever" sentinel."""
        assert BLINK_DISPLAY.encode(ctx_v10, BlinkDisplayRequest(duration_s=-1)) == b"AFFP -1\r"

    def test_other_negative_rejected(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            BLINK_DISPLAY.encode(ctx_v10, BlinkDisplayRequest(duration_s=-2))

    def test_decode_flashing(self, ctx_v10: DecodeContext) -> None:
        state = BLINK_DISPLAY.decode(b"A 1", ctx_v10)
        assert state.flashing is True

    def test_decode_idle(self, ctx_v10: DecodeContext) -> None:
        state = BLINK_DISPLAY.decode(b"A 0", ctx_v10)
        assert state.flashing is False


class TestLockUnlockDisplay:
    def test_lock_encode(self, ctx_v10: DecodeContext) -> None:
        assert LOCK_DISPLAY.encode(ctx_v10, LockDisplayRequest()) == b"AL\r"

    def test_unlock_encode(self, ctx_v10: DecodeContext) -> None:
        assert UNLOCK_DISPLAY.encode(ctx_v10, UnlockDisplayRequest()) == b"AU\r"

    def test_lock_requires_frame_format(self, ctx_v10: DecodeContext) -> None:
        """Post-op decode needs ``??D*`` cached."""
        with pytest.raises(AlicatParseError):
            LOCK_DISPLAY.decode(b"A 50.0 LCK", ctx_v10)

    def test_lock_parses_frame(self, ctx_v10_with_format: DecodeContext) -> None:
        parsed = LOCK_DISPLAY.decode(b"A 50.0 LCK", ctx_v10_with_format)
        # LCK is a StatusCode the data-frame parser lifts into the status
        # set rather than leaving in the values map; just confirm the unit
        # id round-trips (the frame wrapper at facade layer surfaces
        # ``DisplayLockResult.locked``).
        assert parsed.unit_id == "A"

    def test_unlock_is_not_capability_gated(self) -> None:
        """``UNLOCK_DISPLAY`` is the safety escape — always callable.

        :data:`LOCK_DISPLAY` is gated on :attr:`Capability.DISPLAY` (no
        point locking a display you haven't verified exists) but
        :data:`UNLOCK_DISPLAY` must NOT be gated: if anything ever
        locks the device (a V1_V7 ``AL<X>`` side-effect, third-party
        code, direct ``session.execute``) we need a callable escape
        regardless of what the factory's capability probe saw.

        Finding from 2026-04-17: on V1_V7 firmware (7v09)
        — which has ``Capability.NONE`` because ``DISPLAY`` has no
        safe probe — the ``AU`` wire bytes demonstrably clear the
        ``LCK`` status bit. Gating ``UNLOCK_DISPLAY`` on
        ``Capability.DISPLAY`` would lock users out of the recovery.
        """
        from alicatlib.commands import Capability

        assert LOCK_DISPLAY.required_capabilities is Capability.DISPLAY
        assert UNLOCK_DISPLAY.required_capabilities is Capability.NONE


# ---------------------------------------------------------------------------
# UD (user data)
# ---------------------------------------------------------------------------


class TestUserData:
    def test_read(self, ctx_v10: DecodeContext) -> None:
        assert USER_DATA.encode(ctx_v10, UserDataRequest(slot=0)) == b"AUD 0\r"

    def test_write(self, ctx_v10: DecodeContext) -> None:
        assert USER_DATA.encode(ctx_v10, UserDataRequest(slot=1, value="hello")) == b"AUD 1 hello\r"

    def test_write_with_spaces(self, ctx_v10: DecodeContext) -> None:
        """Multi-word values pass through verbatim — the decoder re-joins them."""
        assert (
            USER_DATA.encode(ctx_v10, UserDataRequest(slot=2, value="hi there"))
            == b"AUD 2 hi there\r"
        )

    def test_slot_out_of_range_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            USER_DATA.encode(ctx_v10, UserDataRequest(slot=4))
        with pytest.raises(AlicatValidationError):
            USER_DATA.encode(ctx_v10, UserDataRequest(slot=-1))

    def test_value_too_long_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            USER_DATA.encode(ctx_v10, UserDataRequest(slot=0, value="x" * (UD_MAX_VALUE_LEN + 1)))

    def test_value_with_cr_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            USER_DATA.encode(ctx_v10, UserDataRequest(slot=0, value="bad\rvalue"))

    def test_value_non_ascii_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            USER_DATA.encode(ctx_v10, UserDataRequest(slot=0, value="emoji 🙂"))

    def test_decode_with_value(self, ctx_v10: DecodeContext) -> None:
        state = USER_DATA.decode(b"A 1 hello world", ctx_v10)
        assert state.slot == 1
        assert state.value == "hello world"

    def test_decode_empty_slot(self, ctx_v10: DecodeContext) -> None:
        """Some firmware returns ``<uid> <slot>`` when the slot has never been written."""
        state = USER_DATA.decode(b"A 3", ctx_v10)
        assert state.slot == 3
        assert state.value == ""

    def test_decode_empty_slot_real_hardware(self, ctx_v10: DecodeContext) -> None:
        """Real 10v20 firmware returns just ``<uid>`` when a slot is empty.

        Observed on 2026-04-17 on MC-500SCCM-D @ 10v20.0-R24:
        query ``AUD 0`` (and 1, 2, 3) all return ``A \\r`` — 1 token
        after whitespace split. The decoder returns ``slot=-1`` as a
        sentinel; the facade re-populates it from the request (see
        :meth:`Device.user_data`).
        """
        state = USER_DATA.decode(b"A ", ctx_v10)
        assert state.slot == -1
        assert state.value == ""


# ---------------------------------------------------------------------------
# ZCA (auto-tare)
# ---------------------------------------------------------------------------


class TestAutoTare:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert AUTO_TARE.encode(ctx_v10, AutoTareRequest()) == b"AZCA\r"

    def test_enable_with_delay(self, ctx_v10: DecodeContext) -> None:
        assert (
            AUTO_TARE.encode(ctx_v10, AutoTareRequest(enable=True, delay_s=1.5)) == b"AZCA 1 1.5\r"
        )

    def test_enable_without_delay_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            AUTO_TARE.encode(ctx_v10, AutoTareRequest(enable=True))

    def test_disable_no_delay(self, ctx_v10: DecodeContext) -> None:
        """Disable emits ``AZCA 0`` with NO delay field.

        Primer documents ``ZCA <uid> 0 0`` as the disable form, but
        Captures on 2026-04-17 (§16.6.10) confirmed on two 10v20
        units that the primer form rejects with ``?``. The wire-form
        probe found ``ZCA 0`` (no delay field) is the shortest form
        the device accepts — encoder emits this shape.
        """
        assert AUTO_TARE.encode(ctx_v10, AutoTareRequest(enable=False)) == b"AZCA 0\r"

    def test_disable_with_delay_ignored(self, ctx_v10: DecodeContext) -> None:
        """``enable=False`` always uses the bare-``0`` form; ``delay_s`` is ignored."""
        assert (
            AUTO_TARE.encode(ctx_v10, AutoTareRequest(enable=False, delay_s=1.5))
            == b"AZCA 0\r"
        )

    def test_delay_below_min_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            AUTO_TARE.encode(
                ctx_v10,
                AutoTareRequest(enable=True, delay_s=ZCA_DELAY_MIN_S - 0.01),
            )

    def test_delay_above_max_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            AUTO_TARE.encode(
                ctx_v10,
                AutoTareRequest(enable=True, delay_s=ZCA_DELAY_MAX_S + 0.01),
            )

    def test_decode(self, ctx_v10: DecodeContext) -> None:
        state = AUTO_TARE.decode(b"A 1 1.5", ctx_v10)
        assert state.enabled is True
        assert state.delay_s == 1.5


# ---------------------------------------------------------------------------
# ZCP (power-up tare)
# ---------------------------------------------------------------------------


class TestPowerUpTare:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert POWER_UP_TARE.encode(ctx_v10, PowerUpTareRequest()) == b"AZCP\r"

    def test_enable(self, ctx_v10: DecodeContext) -> None:
        assert POWER_UP_TARE.encode(ctx_v10, PowerUpTareRequest(enable=True)) == b"AZCP 1\r"

    def test_disable(self, ctx_v10: DecodeContext) -> None:
        assert POWER_UP_TARE.encode(ctx_v10, PowerUpTareRequest(enable=False)) == b"AZCP 0\r"

    def test_decode_enabled(self, ctx_v10: DecodeContext) -> None:
        state = POWER_UP_TARE.decode(b"A 1", ctx_v10)
        assert state.enabled is True
