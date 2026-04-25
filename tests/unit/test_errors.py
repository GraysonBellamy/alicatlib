"""Tests for ``alicatlib.errors``."""

from __future__ import annotations

import pytest

from alicatlib.devices.medium import Medium
from alicatlib.errors import (
    AlicatError,
    AlicatFirmwareError,
    AlicatMediumMismatchError,
    AlicatParseError,
    AlicatTimeoutError,
    AlicatTransportError,
    ErrorContext,
    UnknownGasError,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from tests._typing import approx


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
            actual=FirmwareVersion(family=FirmwareFamily.V8_V9, major=9, minor=0, raw="9v00"),
            required_min=FirmwareVersion(family=FirmwareFamily.V10, major=10, minor=5, raw="10v05"),
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
        assert enriched.context.elapsed_s == approx(0.25)
        assert enriched.context.command_name == "poll"

    def test_with_context_preserves_parse_error_fields(self) -> None:
        original = AlicatParseError(
            "bad field", field_name="gas_code", expected="int", actual="abc"
        )
        enriched = original.with_context(unit_id="A", port="/dev/ttyUSB0")
        assert enriched.field_name == "gas_code"
        assert enriched.expected == "int"
        assert enriched.actual == "abc"
        assert enriched.context.unit_id == "A"
        assert enriched.context.port == "/dev/ttyUSB0"

    def test_with_context_preserves_unknown_gas_fields(self) -> None:
        original = UnknownGasError("xenon", suggestions=("xe",))
        enriched = original.with_context(unit_id="A")
        assert enriched.value == "xenon"
        assert enriched.suggestions == ("xe",)
        assert enriched.context.unit_id == "A"
        # Message must not be re-derived from str(self) (the historical bug
        # nested the rendered string into a fresh "Unknown gas: ..." prefix).
        assert "xenon" in str(enriched)
        assert "Unknown gas: 'Unknown gas:" not in str(enriched)

    def test_with_context_preserves_medium_mismatch_fields(self) -> None:
        original = AlicatMediumMismatchError(
            command="gas", device_media=Medium.LIQUID, command_media=Medium.GAS
        )
        enriched = original.with_context(unit_id="A")
        assert enriched.command == "gas"
        assert enriched.device_media is Medium.LIQUID
        assert enriched.command_media is Medium.GAS
        assert enriched.context.unit_id == "A"

    def test_with_context_preserves_firmware_error_fields(self) -> None:
        actual = FirmwareVersion(family=FirmwareFamily.V8_V9, major=9, minor=0, raw="9v00")
        required = FirmwareVersion(family=FirmwareFamily.V10, major=10, minor=5, raw="10v05")
        original = AlicatFirmwareError(
            command="auto_tare", reason="firmware_too_old", actual=actual, required_min=required
        )
        enriched = original.with_context(unit_id="A")
        assert enriched.command == "auto_tare"
        assert enriched.reason == "firmware_too_old"
        assert enriched.actual == actual
        assert enriched.required_min == required
        assert enriched.context.unit_id == "A"


class TestEmptyContextIsImmutable:
    def test_default_extra_cannot_be_mutated(self) -> None:
        # Two errors share the default empty extra mapping; mutating one
        # must not bleed into another. Use try/except — MappingProxyType
        # raises TypeError, but subclassing ``dict`` would raise nothing.
        a = AlicatError("a")
        b = AlicatError("b")
        # Sanity: both share the same sentinel.
        assert a.context.extra is b.context.extra
        # And it must reject mutation.
        with pytest.raises(TypeError):
            a.context.extra["x"] = 1  # type: ignore[index]


class TestStrRendersAllContextFields:
    def test_renders_command_bytes_and_raw_response(self) -> None:
        ctx = ErrorContext(
            command_name="gas",
            command_bytes=b"A G 1\r\n",
            raw_response=b"A ? \r\n",
            unit_id="A",
        )
        rendered = str(AlicatError("rejected", context=ctx))
        assert "command=gas" in rendered
        assert "command_bytes=" in rendered
        assert "raw_response=" in rendered
        assert "unit_id=A" in rendered
