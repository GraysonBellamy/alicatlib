"""Tests for ``alicatlib.errors``."""

from __future__ import annotations

import pytest

from alicatlib.errors import (
    AlicatError,
    AlicatFirmwareError,
    AlicatParseError,
    AlicatTimeoutError,
    AlicatTransportError,
    ErrorContext,
    UnknownGasError,
)
from alicatlib.firmware import FirmwareVersion


class TestErrorContext:
    def test_defaults_are_empty(self) -> None:
        ctx = ErrorContext()
        assert ctx.command_name is None
        assert ctx.unit_id is None
        assert ctx.extra == {}

    def test_merged_overlays_known_fields(self) -> None:
        ctx = ErrorContext(command_name="gas", unit_id="A").merged(unit_id="B", port="/dev/ttyUSB0")
        assert ctx.command_name == "gas"
        assert ctx.unit_id == "B"
        assert ctx.port == "/dev/ttyUSB0"

    def test_merged_puts_unknown_keys_in_extra(self) -> None:
        ctx = ErrorContext().merged(note="retry_1", attempt=2)
        assert ctx.extra == {"note": "retry_1", "attempt": 2}

    def test_merged_is_copy_on_write(self) -> None:
        original = ErrorContext(command_name="gas")
        new = original.merged(unit_id="A")
        assert original.unit_id is None
        assert new.unit_id == "A"


class TestHierarchy:
    def test_timeout_is_transport(self) -> None:
        err = AlicatTimeoutError("timed out")
        assert isinstance(err, AlicatTransportError)
        assert isinstance(err, AlicatError)

    def test_parse_is_alicat(self) -> None:
        err = AlicatParseError("bad field", field_name="gas_code", expected="int", actual="abc")
        assert isinstance(err, AlicatError)
        assert err.field_name == "gas_code"
        assert err.expected == "int"
        assert err.actual == "abc"


class TestUnknownGasError:
    def test_formats_suggestions(self) -> None:
        err = UnknownGasError("N22", suggestions=("N2", "N2O"))
        assert "N22" in str(err)
        assert "N2" in str(err)
        assert err.value == "N22"
        assert err.suggestions == ("N2", "N2O")

    def test_without_suggestions(self) -> None:
        err = UnknownGasError("XYZ")
        assert "XYZ" in str(err)
        assert "did you mean" not in str(err)


class TestAlicatFirmwareError:
    def test_includes_required_range(self) -> None:
        err = AlicatFirmwareError(
            command="auto_tare",
            reason="firmware_too_old",
            actual=FirmwareVersion(9, 0),
            required_min=FirmwareVersion(10, 5),
        )
        msg = str(err)
        assert "auto_tare" in msg
        assert "10v05" in msg
        assert "9v00" in msg


class TestWithContext:
    def test_with_context_returns_enriched_copy(self) -> None:
        original = AlicatTimeoutError("no reply", context=ErrorContext(command_name="poll"))
        enriched = original.with_context(port="/dev/ttyUSB0", elapsed_s=0.25)
        assert original.context.port is None
        assert enriched.context.port == "/dev/ttyUSB0"
        assert enriched.context.elapsed_s == pytest.approx(0.25)
        assert enriched.context.command_name == "poll"
