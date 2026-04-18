"""Tests for :mod:`alicatlib.commands.base` — the declarative command spec."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from alicatlib.commands import (
    GAS_SELECT,
    Capability,
    Command,
    DecodeContext,
    ResponseMode,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion


class TestResponseMode:
    def test_values_are_stable(self) -> None:
        """Values appear in telemetry; pin them to guard against accidental rename."""
        assert ResponseMode.NONE.value == "none"
        assert ResponseMode.LINE.value == "line"
        assert ResponseMode.LINES.value == "lines"
        assert ResponseMode.STREAM.value == "stream"


class TestCapability:
    def test_none_is_zero(self) -> None:
        assert Capability.NONE.value == 0

    def test_flags_compose(self) -> None:
        combined = Capability.BAROMETER | Capability.TOTALIZER
        assert Capability.BAROMETER in combined
        assert Capability.TOTALIZER in combined
        assert Capability.MULTI_VALVE not in combined


class TestDecodeContext:
    def test_default_prefix_and_capabilities(self) -> None:
        ctx = DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))
        assert ctx.command_prefix == b""
        assert ctx.capabilities == Capability.NONE

    def test_is_frozen(self) -> None:
        ctx = DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))
        with pytest.raises(FrozenInstanceError):
            ctx.unit_id = "B"  # type: ignore[misc]


class TestCommandBase:
    def test_is_frozen(self) -> None:
        """GAS_SELECT is a module singleton and must not be mutable at runtime."""
        with pytest.raises(FrozenInstanceError):
            GAS_SELECT.token = "X"  # type: ignore[misc]

    def test_base_encode_raises_not_implemented(self) -> None:
        """Abstract-ish base: a raw Command can't encode."""
        cmd: Command[object, object] = Command(
            name="raw",
            token="X",
            response_mode=ResponseMode.LINE,
            device_kinds=frozenset(),
        )
        ctx = DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))
        with pytest.raises(NotImplementedError):
            cmd.encode(ctx, object())
        with pytest.raises(NotImplementedError):
            cmd.decode(b"A\r", ctx)


class TestMultilineTerminationInvariant:
    """Design §5.4: every LINES command must declare ``expected_lines`` or
    ``is_complete``. A LINES command that declares neither always falls
    through to the idle-timeout fallback, adding ~100 ms latency per call.
    Pin the invariant on the catalog so new LINES commands can't regress it.
    """

    def test_every_lines_command_declares_termination(self) -> None:
        # Guard loop is cheap to run and covers multiline tables (??M*, ??D*, GL).
        from alicatlib.commands import Commands

        lines_commands = [
            cast("Command[object, object]", cmd)
            for cmd in vars(Commands).values()
            if isinstance(cmd, Command) and cmd.response_mode is ResponseMode.LINES
        ]
        missing = [
            cmd.name
            for cmd in lines_commands
            if cmd.expected_lines is None and cmd.is_complete is None
        ]
        assert missing == [], (
            f"LINES commands missing termination contract: {missing} (design §5.4)"
        )


class TestGasSelectMetadata:
    """GAS_SELECT's metadata drives firmware gating in the session."""

    def test_min_firmware_is_10v05(self) -> None:
        assert GAS_SELECT.min_firmware == FirmwareVersion(
            family=FirmwareFamily.V10,
            major=10,
            minor=5,
            raw="10v05",
        )

    def test_firmware_families_v10_only(self) -> None:
        """GS is the modern (10v05+) form; legacy ``G`` covers older families."""
        assert GAS_SELECT.firmware_families == frozenset({FirmwareFamily.V10})

    def test_not_destructive_not_experimental(self) -> None:
        assert not GAS_SELECT.destructive
        assert not GAS_SELECT.experimental

    def test_applies_to_flow_devices(self) -> None:
        from alicatlib.devices import DeviceKind

        assert DeviceKind.FLOW_METER in GAS_SELECT.device_kinds
        assert DeviceKind.FLOW_CONTROLLER in GAS_SELECT.device_kinds
