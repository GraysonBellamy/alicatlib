r"""Display commands — primer §Device setup (Blink / Lock / Unlock).

Three commands ship here; all are gated to
:attr:`Capability.DISPLAY` (discovered at :func:`open_device`):

- :data:`BLINK_DISPLAY` (``FFP``, 8v28+) — flash the backlight for a
  duration (seconds). ``0`` stops an active flash; ``-1`` flashes
  indefinitely.
- :data:`LOCK_DISPLAY` (``L``) — disable front-panel buttons.
  Response is a post-op data frame carrying the :attr:`StatusCode.LCK`
  bit. No firmware cutoff documented — assume all firmware supports
  it when :attr:`Capability.DISPLAY` is present.
- :data:`UNLOCK_DISPLAY` (``U``) — re-enable front-panel buttons;
  post-op data frame clears the ``LCK`` bit.

Design reference: ``docs/design.md`` §9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from alicatlib.commands.base import Capability, Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import BlinkDisplayState
from alicatlib.errors import (
    AlicatParseError,
    AlicatValidationError,
    ErrorContext,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_bool_code, parse_fields

if TYPE_CHECKING:
    from alicatlib.devices.data_frame import ParsedFrame

__all__ = [
    "BLINK_DISPLAY",
    "LOCK_DISPLAY",
    "UNLOCK_DISPLAY",
    "BlinkDisplay",
    "BlinkDisplayRequest",
    "LockDisplay",
    "LockDisplayRequest",
    "UnlockDisplay",
    "UnlockDisplayRequest",
]


_ALL_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(DeviceKind)


# Primer p. 21: FFP is 8v28+; V8_V9 and V10 support it once the device
# reports a display. The ``min_firmware`` gate blocks pre-8v28 V8_V9
# devices; V10 is unconditional. V1_V7 / GP rarely have displays and
# the capability gate catches them.
_MIN_FIRMWARE_FFP: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V8_V9,
    major=8,
    minor=28,
    raw="8v28",
)


#: Primer documents ``-1`` as the "flash indefinitely" sentinel.
_FFP_FLASH_INDEFINITELY: Final[int] = -1


# ---------------------------------------------------------------------------
# BLINK_DISPLAY (``FFP``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlinkDisplayRequest:
    """Arguments for :data:`BLINK_DISPLAY`.

    Attributes:
        duration_s: Flash duration in seconds. ``None`` issues the
            query form; ``0`` stops an active flash; ``-1`` flashes
            indefinitely. Other negatives are rejected pre-I/O
            because they are not documented sentinels.
    """

    duration_s: int | None = None


@dataclass(frozen=True, slots=True)
class BlinkDisplay(Command[BlinkDisplayRequest, BlinkDisplayState]):
    r"""``FFP`` — blink display query/set (8v28+, DISPLAY capability).

    Wire:

    - Query: ``<uid><prefix>FFP\r``
    - Set:   ``<uid><prefix>FFP <duration>\r``

    Response: ``<uid> <0|1>`` — two fields. ``1`` = flashing,
    ``0`` = idle.
    """

    name: str = "blink_display"
    token: str = "FFP"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    required_capabilities: Capability = Capability.DISPLAY
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_FFP
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(self, ctx: DecodeContext, request: BlinkDisplayRequest) -> bytes:
        """Emit FFP query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.duration_s is None:
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        if request.duration_s < 0 and request.duration_s != _FFP_FLASH_INDEFINITELY:
            raise AlicatValidationError(
                f"{self.name}: duration_s must be >= 0 or exactly "
                f"{_FFP_FLASH_INDEFINITELY} (flash indefinitely), "
                f"got {request.duration_s}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"duration_s": request.duration_s},
                ),
            )
        return f"{ctx.unit_id}{prefix}{self.token} {request.duration_s}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> BlinkDisplayState:
        """Parse ``<uid> <0|1>`` into :class:`BlinkDisplayState`."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=2)
        unit_id, flag_s = fields
        return BlinkDisplayState(
            unit_id=unit_id,
            flashing=parse_bool_code(flag_s, field=f"{self.name}.flashing"),
        )


BLINK_DISPLAY: BlinkDisplay = BlinkDisplay()


# ---------------------------------------------------------------------------
# LOCK_DISPLAY / UNLOCK_DISPLAY — shared post-op data-frame decoders
# ---------------------------------------------------------------------------


def _decode_display_frame(
    command_name: str,
    response: bytes | tuple[bytes, ...],
    ctx: DecodeContext,
) -> ParsedFrame:
    """Shared decode: both L / U reply with a post-op data frame.

    The facade wraps this into a :class:`DisplayLockResult` at
    facade-level timing (same pattern as HP / HC / C and tare
    commands).
    """
    if isinstance(response, tuple):
        raise TypeError(
            f"{command_name}.decode expected single-line response, got {len(response)} lines",
        )
    if ctx.data_frame_format is None:
        raise AlicatParseError(
            f"{command_name} requires ctx.data_frame_format; session must probe ??D* first",
            field_name="data_frame_format",
            expected="DataFrameFormat",
            actual=None,
            context=ErrorContext(command_name=command_name, raw_response=response),
        )
    return ctx.data_frame_format.parse(response)


# ---------------------------------------------------------------------------
# LOCK_DISPLAY (``L``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LockDisplayRequest:
    """Arguments for :data:`LOCK_DISPLAY` (empty — ``L`` takes no arguments)."""


@dataclass(frozen=True, slots=True)
class LockDisplay(Command[LockDisplayRequest, "ParsedFrame"]):
    r"""``L`` — lock front-panel display (DISPLAY capability).

    Wire: ``<uid><prefix>L\r``. Response is a post-op data frame with
    :attr:`StatusCode.LCK` active. No primer firmware cutoff —
    capability gate suffices.
    """

    name: str = "lock_display"
    token: str = "L"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    required_capabilities: Capability = Capability.DISPLAY

    def encode(self, ctx: DecodeContext, request: LockDisplayRequest) -> bytes:
        r"""Emit ``<uid><prefix>L\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-op data frame — see :func:`_decode_display_frame`."""
        return _decode_display_frame(self.name, response, ctx)


LOCK_DISPLAY: LockDisplay = LockDisplay()


# ---------------------------------------------------------------------------
# UNLOCK_DISPLAY (``U``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnlockDisplayRequest:
    """Arguments for :data:`UNLOCK_DISPLAY` (empty)."""


@dataclass(frozen=True, slots=True)
class UnlockDisplay(Command[UnlockDisplayRequest, "ParsedFrame"]):
    r"""``U`` — unlock front-panel display. Safety escape hatch.

    Wire: ``<uid><prefix>U\r``. Response is a post-op data frame
    *without* the :attr:`StatusCode.LCK` bit.

    Intentionally NOT gated on :attr:`Capability.DISPLAY` (unlike
    :data:`LOCK_DISPLAY`): the point of this command is to recover a
    device that got into a locked state. Hardware validation (2026-04-17)
    found that on V1_V7 firmware, any command starting with ``AL<X>``
    (including ``ALS`` / ``ALSS`` / ``ALV`` that the library itself
    firmware-gates pre-I/O) is parsed by the device as
    "lock display with argument X" and sets the ``LCK`` status bit.
    The library's firmware gates protect against this under normal
    use, but third-party code or direct catalog-command execution can
    still trip it — ``dev.unlock_display()`` must always be callable
    as the escape. ``AU`` is confirmed safe on V1_V7 (7v09) / V8_V9 /
    V10; on a device without a physical display it's a harmless
    no-op (the primer makes no firmware-cutoff claim).
    """

    name: str = "unlock_display"
    token: str = "U"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    # No required_capabilities — this is a safety escape.

    def encode(self, ctx: DecodeContext, request: UnlockDisplayRequest) -> bytes:
        r"""Emit ``<uid><prefix>U\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-op data frame — see :func:`_decode_display_frame`."""
        return _decode_display_frame(self.name, response, ctx)


UNLOCK_DISPLAY: UnlockDisplay = UnlockDisplay()
