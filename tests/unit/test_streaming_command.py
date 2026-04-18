"""Tests for :data:`alicatlib.commands.streaming.STREAMING_RATE` + helpers.

Covers:

- ``NCS`` encode (query, set, rate=0, GP prefix, firmware gating).
- ``NCS`` decode (2-field parse, parse error surfaces).
- Start-stream / stop-stream raw-byte helpers (primer p. 10).
- Pre-I/O validation: negative rate, non-int rate.

Runtime tests for :class:`StreamingSession` live in
:mod:`test_streaming_runtime`.
"""

from __future__ import annotations

import pytest

from alicatlib.commands import (
    STREAMING_RATE,
    DecodeContext,
    StreamingRateRequest,
)
from alicatlib.commands.streaming import (
    encode_start_stream,
    encode_stop_stream,
)
from alicatlib.errors import AlicatParseError, AlicatValidationError
from alicatlib.firmware import FirmwareVersion


@pytest.fixture
def ctx_v10() -> DecodeContext:
    return DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))


@pytest.fixture
def ctx_gp() -> DecodeContext:
    # STREAMING_RATE is firmware-family-gated to V10 (primer p. 22), but the
    # encoder itself still runs — this fixture exercises the GP prefix path
    # that the session gate would otherwise block pre-I/O.
    return DecodeContext(
        unit_id="A",
        firmware=FirmwareVersion.parse("10v05"),
        command_prefix=b"$$",
    )


class TestEncode:
    def test_query(self, ctx_v10: DecodeContext) -> None:
        assert STREAMING_RATE.encode(ctx_v10, StreamingRateRequest()) == b"ANCS\r"

    def test_set_with_rate(self, ctx_v10: DecodeContext) -> None:
        assert STREAMING_RATE.encode(ctx_v10, StreamingRateRequest(rate_ms=50)) == b"ANCS 50\r"

    def test_set_zero_is_distinct_from_query(self, ctx_v10: DecodeContext) -> None:
        """``rate_ms=0`` is the device's as-fast-as-possible setting.

        Must emit ``NCS 0``, not the bare ``NCS`` query form — ``0`` vs
        ``None`` is load-bearing per the design's encoder rule
        (§5.4: "``None`` means omitted/query form; ``0`` and ``False``
        are valid values where the command allows them").
        """
        assert STREAMING_RATE.encode(ctx_v10, StreamingRateRequest(rate_ms=0)) == b"ANCS 0\r"

    def test_gp_prefix(self, ctx_gp: DecodeContext) -> None:
        assert STREAMING_RATE.encode(ctx_gp, StreamingRateRequest(rate_ms=100)) == b"A$$NCS 100\r"

    def test_negative_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            STREAMING_RATE.encode(ctx_v10, StreamingRateRequest(rate_ms=-1))

    def test_non_int_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatValidationError):
            STREAMING_RATE.encode(
                ctx_v10,
                StreamingRateRequest(rate_ms=50.0),  # type: ignore[arg-type]
            )

    def test_bool_rejected_as_non_int(self, ctx_v10: DecodeContext) -> None:
        """``bool`` is an ``int`` subclass — reject explicitly.

        ``True`` / ``False`` would silently encode as ``1`` / ``0``,
        which is an accidental rate setting. Match the encoder rule
        that keeps the four sentinels (``None``, ``0``, ``False``, ``""``)
        distinct.
        """
        with pytest.raises(AlicatValidationError):
            STREAMING_RATE.encode(
                ctx_v10,
                StreamingRateRequest(rate_ms=True),
            )


class TestDecode:
    def test_basic(self, ctx_v10: DecodeContext) -> None:
        result = STREAMING_RATE.decode(b"A 50", ctx_v10)
        assert result.unit_id == "A"
        assert result.rate_ms == 50

    def test_zero_rate(self, ctx_v10: DecodeContext) -> None:
        """Device reports ``0`` when streaming as-fast-as-possible."""
        result = STREAMING_RATE.decode(b"A 0", ctx_v10)
        assert result.rate_ms == 0

    def test_multiline_input_rejected(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(TypeError):
            STREAMING_RATE.decode((b"A 50", b"A 100"), ctx_v10)

    def test_missing_field_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            STREAMING_RATE.decode(b"A", ctx_v10)

    def test_extra_field_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            STREAMING_RATE.decode(b"A 50 extra", ctx_v10)

    def test_non_numeric_rate_raises(self, ctx_v10: DecodeContext) -> None:
        with pytest.raises(AlicatParseError):
            STREAMING_RATE.decode(b"A fast", ctx_v10)


class TestStartStopHelpers:
    def test_start_stream_bytes(self) -> None:
        r"""Primer p. 10: start-stream is ``{unit_id}@ @\r``."""
        assert encode_start_stream("A") == b"A@ @\r"
        assert encode_start_stream("Z") == b"Z@ @\r"

    def test_stop_stream_bytes(self) -> None:
        r"""Primer p. 10: stop-stream is ``@@ {new_unit_id}\r``.

        Same wire form the factory's ``_recover_from_stream`` already
        writes — keeping both paths in sync.
        """
        assert encode_stop_stream("A") == b"@@ A\r"
        assert encode_stop_stream("B") == b"@@ B\r"
