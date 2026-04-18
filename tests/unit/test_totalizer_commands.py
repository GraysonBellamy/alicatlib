"""Tests for :mod:`alicatlib.commands.totalizer` — TC, T <n>, TP <n>, TCR.

Load-bearing coverage:

- **Token collisions**: ``T\\r`` remains :data:`TARE_FLOW`; ``T 1\\r``
  is :data:`TOTALIZER_RESET`. Same for ``TP\\r`` vs ``TP 1\\r``.
  Encoder must *always* emit the numeric arg on the reset paths so
  the wire form can never degrade into a bare tare command.
- **Destructive confirm**: both reset commands carry
  ``destructive=True`` on the spec so the session gate enforces
  ``confirm=True``.
- TC encode rules: query / disable / full-set / partial-set errors.
- TCR encode rules: query + set.
"""

from __future__ import annotations

import pytest

from alicatlib.commands import (
    TARE_FLOW,
    TARE_GAUGE_PRESSURE,
    TOTALIZER_CONFIG,
    TOTALIZER_RESET,
    TOTALIZER_RESET_PEAK,
    TOTALIZER_SAVE,
    DecodeContext,
    TareFlowRequest,
    TareGaugePressureRequest,
    TotalizerConfigRequest,
    TotalizerResetPeakRequest,
    TotalizerResetRequest,
    TotalizerSaveRequest,
)
from alicatlib.commands.totalizer import (
    TOTALIZER_TC_DECIMAL_MAX,
    TOTALIZER_TC_DIGITS_MAX,
    TOTALIZER_TC_DIGITS_MIN,
)
from alicatlib.devices.models import (
    TotalizerId,
    TotalizerLimitMode,
    TotalizerMode,
)
from alicatlib.errors import AlicatParseError, AlicatValidationError
from alicatlib.firmware import FirmwareVersion


@pytest.fixture
def ctx_v10() -> DecodeContext:
    return DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))


# ---------------------------------------------------------------------------
# Token-collision pinning — T / TP must stay distinct from their tare twins.
# ---------------------------------------------------------------------------


class TestTokenCollision:
    def test_bare_t_is_tare_flow(self, ctx_v10: DecodeContext) -> None:
        r"""``T\r`` with no arg is and must stay :data:`TARE_FLOW`."""
        assert TARE_FLOW.encode(ctx_v10, TareFlowRequest()) == b"AT\r"

    def test_t_with_arg_is_totalizer_reset(self, ctx_v10: DecodeContext) -> None:
        r"""``T 1\r`` with a numeric arg is :data:`TOTALIZER_RESET`."""
        bytes_ = TOTALIZER_RESET.encode(
            ctx_v10, TotalizerResetRequest(totalizer=TotalizerId.FIRST, confirm=True)
        )
        assert bytes_ == b"AT 1\r"
        # Must be distinguishable from tare-flow on the wire.
        assert bytes_ != TARE_FLOW.encode(ctx_v10, TareFlowRequest())

    def test_t_default_includes_numeric_arg(self, ctx_v10: DecodeContext) -> None:
        """Encoder must always emit the numeric arg; default is ``1``."""
        bytes_ = TOTALIZER_RESET.encode(ctx_v10, TotalizerResetRequest(confirm=True))
        # Critical: b"AT\r" (bare-T form) would collide with TARE_FLOW.
        assert bytes_ == b"AT 1\r"
        assert b" " in bytes_  # numeric arg present

    def test_t_totalizer_second(self, ctx_v10: DecodeContext) -> None:
        assert (
            TOTALIZER_RESET.encode(
                ctx_v10,
                TotalizerResetRequest(totalizer=TotalizerId.SECOND, confirm=True),
            )
            == b"AT 2\r"
        )

    def test_bare_tp_is_tare_gauge_pressure(self, ctx_v10: DecodeContext) -> None:
        r"""``TP\r`` with no arg is and must stay :data:`TARE_GAUGE_PRESSURE`."""
        assert TARE_GAUGE_PRESSURE.encode(ctx_v10, TareGaugePressureRequest()) == b"ATP\r"

    def test_tp_with_arg_is_totalizer_reset_peak(self, ctx_v10: DecodeContext) -> None:
        bytes_ = TOTALIZER_RESET_PEAK.encode(
            ctx_v10, TotalizerResetPeakRequest(totalizer=TotalizerId.FIRST, confirm=True)
        )
        assert bytes_ == b"ATP 1\r"
        assert bytes_ != TARE_GAUGE_PRESSURE.encode(ctx_v10, TareGaugePressureRequest())

    def test_tp_default_includes_numeric_arg(self, ctx_v10: DecodeContext) -> None:
        bytes_ = TOTALIZER_RESET_PEAK.encode(ctx_v10, TotalizerResetPeakRequest(confirm=True))
        assert bytes_ == b"ATP 1\r"


class TestResetDestructive:
    def test_totalizer_reset_is_destructive(self) -> None:
        assert TOTALIZER_RESET.destructive is True

    def test_totalizer_reset_peak_is_destructive(self) -> None:
        assert TOTALIZER_RESET_PEAK.destructive is True


# ---------------------------------------------------------------------------
# TC (configure totalizer)
# ---------------------------------------------------------------------------


class TestTotalizerConfigEncode:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert (
            TOTALIZER_CONFIG.encode(ctx_v10, TotalizerConfigRequest(totalizer=TotalizerId.FIRST))
            == b"ATC 1\r"
        )

    def test_query_second_totalizer(self, ctx_v10: DecodeContext) -> None:
        assert (
            TOTALIZER_CONFIG.encode(ctx_v10, TotalizerConfigRequest(totalizer=TotalizerId.SECOND))
            == b"ATC 2\r"
        )

    def test_disable(self, ctx_v10: DecodeContext) -> None:
        """``flow_statistic_code=1`` disables — primer: no further args."""
        assert (
            TOTALIZER_CONFIG.encode(
                ctx_v10,
                TotalizerConfigRequest(totalizer=TotalizerId.FIRST, flow_statistic_code=1),
            )
            == b"ATC 1 1\r"
        )

    def test_full_set(self, ctx_v10: DecodeContext) -> None:
        bytes_ = TOTALIZER_CONFIG.encode(
            ctx_v10,
            TotalizerConfigRequest(
                totalizer=TotalizerId.FIRST,
                flow_statistic_code=5,
                mode=TotalizerMode.BIDIRECTIONAL,
                limit_mode=TotalizerLimitMode.ROLLOVER,
                digits=8,
                decimal_place=2,
            ),
        )
        assert bytes_ == b"ATC 1 5 2 1 8 2\r"

    def test_keep_sentinel_roundtrips(self, ctx_v10: DecodeContext) -> None:
        """``-1`` KEEP sentinels encode as literal ``-1`` on the wire."""
        bytes_ = TOTALIZER_CONFIG.encode(
            ctx_v10,
            TotalizerConfigRequest(
                totalizer=TotalizerId.SECOND,
                flow_statistic_code=5,
                mode=TotalizerMode.KEEP,
                limit_mode=TotalizerLimitMode.KEEP,
                digits=7,
                decimal_place=0,
            ),
        )
        assert bytes_ == b"ATC 2 5 -1 -1 7 0\r"

    def test_partial_set_raises(self, ctx_v10: DecodeContext) -> None:
        """Enabling / reconfiguring requires all four config fields."""
        with pytest.raises(AlicatValidationError):
            TOTALIZER_CONFIG.encode(
                ctx_v10,
                TotalizerConfigRequest(
                    totalizer=TotalizerId.FIRST,
                    flow_statistic_code=5,
                    # mode / limit_mode / digits / decimal_place omitted
                ),
            )

    def test_digits_out_of_range_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            TOTALIZER_CONFIG.encode(
                ctx_v10,
                TotalizerConfigRequest(
                    totalizer=TotalizerId.FIRST,
                    flow_statistic_code=5,
                    mode=TotalizerMode.POSITIVE_ONLY,
                    limit_mode=TotalizerLimitMode.STOP_AT_MAX,
                    digits=TOTALIZER_TC_DIGITS_MAX + 1,
                    decimal_place=0,
                ),
            )
        with pytest.raises(AlicatValidationError):
            TOTALIZER_CONFIG.encode(
                ctx_v10,
                TotalizerConfigRequest(
                    totalizer=TotalizerId.FIRST,
                    flow_statistic_code=5,
                    mode=TotalizerMode.POSITIVE_ONLY,
                    limit_mode=TotalizerLimitMode.STOP_AT_MAX,
                    digits=TOTALIZER_TC_DIGITS_MIN - 1,
                    decimal_place=0,
                ),
            )

    def test_decimal_out_of_range_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            TOTALIZER_CONFIG.encode(
                ctx_v10,
                TotalizerConfigRequest(
                    totalizer=TotalizerId.FIRST,
                    flow_statistic_code=5,
                    mode=TotalizerMode.POSITIVE_ONLY,
                    limit_mode=TotalizerLimitMode.STOP_AT_MAX,
                    digits=7,
                    decimal_place=TOTALIZER_TC_DECIMAL_MAX + 1,
                ),
            )


class TestTotalizerConfigDecode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        """Primer's 6-field reply: ``<uid> <flow_stat> <mode> <limit> <digits> <decimal>``."""
        state = TOTALIZER_CONFIG.decode(b"A 5 2 1 8 2", ctx_v10)
        assert state.unit_id == "A"
        assert state.flow_statistic_code == 5
        assert state.mode is TotalizerMode.BIDIRECTIONAL
        assert state.limit_mode is TotalizerLimitMode.ROLLOVER
        assert state.digits == 8
        assert state.decimal_place == 2
        assert state.enabled is True

    def test_disabled_report(self, ctx_v10: DecodeContext) -> None:
        """``flow_statistic_code == 1`` → :attr:`TotalizerConfig.enabled` is False."""
        state = TOTALIZER_CONFIG.decode(b"A 1 0 0 7 0", ctx_v10)
        assert state.flow_statistic_code == 1
        assert state.enabled is False

    def test_real_hardware_seven_field_reply(self, ctx_v10: DecodeContext) -> None:
        """Real 10v20 firmware echoes the totalizer id as the second token.

        Observed on 2026-04-17 on MC-500SCCM-D @ 10v20.0-R24:
        ``ATC 1`` replies with ``A 1 1 0 0 7 0\\r`` — 7 fields. The
        primer documents 6 (totalizer id not echoed); real firmware
        echoes it. The decoder must accept both shapes and drop the
        echoed id when present.
        """
        state = TOTALIZER_CONFIG.decode(b"A 1 1 0 0 7 0", ctx_v10)
        assert state.unit_id == "A"
        # Second '1' is the totalizer_id echo — dropped; real
        # flow_statistic_code is the third token.
        assert state.flow_statistic_code == 1
        assert state.mode is TotalizerMode.POSITIVE_ONLY
        assert state.limit_mode is TotalizerLimitMode.STOP_AT_MAX
        assert state.digits == 7
        assert state.decimal_place == 0

    def test_bad_field_count_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            TOTALIZER_CONFIG.decode(b"A 5 2 1", ctx_v10)


# ---------------------------------------------------------------------------
# TCR (save totalizer)
# ---------------------------------------------------------------------------


class TestTotalizerSave:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert TOTALIZER_SAVE.encode(ctx_v10, TotalizerSaveRequest()) == b"ATCR\r"

    def test_enable(self, ctx_v10: DecodeContext) -> None:
        assert TOTALIZER_SAVE.encode(ctx_v10, TotalizerSaveRequest(enable=True)) == b"ATCR 1\r"

    def test_disable(self, ctx_v10: DecodeContext) -> None:
        assert TOTALIZER_SAVE.encode(ctx_v10, TotalizerSaveRequest(enable=False)) == b"ATCR 0\r"

    def test_decode_enabled(self, ctx_v10: DecodeContext) -> None:
        state = TOTALIZER_SAVE.decode(b"A 1", ctx_v10)
        assert state.enabled is True

    def test_decode_disabled(self, ctx_v10: DecodeContext) -> None:
        state = TOTALIZER_SAVE.decode(b"A 0", ctx_v10)
        assert state.enabled is False
