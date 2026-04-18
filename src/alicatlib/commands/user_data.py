r"""User-data command — primer §Save and Read User Data (``UD``, 8v24+).

Four slots (``0..3``), 32 ASCII characters each. Reading a slot
returns whatever was last written; writing overwrites that slot
atomically (the device's response echoes the newly-written string).
Binary data must be encoded into ASCII (hex / base64) by the caller —
the library does not interpret the stored value.

Wire shape:

- Read:  ``<uid><prefix>UD <slot>\r``
- Write: ``<uid><prefix>UD <slot> <value>\r``

Response: ``<uid> <slot> <value>`` — value may contain spaces so the
decoder joins tokens after the slot with a single space.

Design reference: ``docs/design.md`` §9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import UserDataSetting
from alicatlib.errors import (
    AlicatParseError,
    AlicatValidationError,
    ErrorContext,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_fields, parse_int

__all__ = [
    "UD_MAX_SLOT",
    "UD_MAX_VALUE_LEN",
    "USER_DATA",
    "UserData",
    "UserDataRequest",
]


#: Primer p. 22 pins ``UD`` at ``8v24+``; V8_V9 and V10 support it once
#: past that cutoff. Family-scoped ``min_firmware`` only gates pre-8v24
#: V8_V9; V10 is unconditional. V1_V7 / GP have no ``UD`` command.
_MIN_FIRMWARE_UD: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V8_V9,
    major=8,
    minor=24,
    raw="8v24",
)


#: Highest permitted slot index — primer documents 4 slots, ``0..3``.
UD_MAX_SLOT: Final[int] = 3


#: Maximum ASCII value length per primer — 32 characters.
UD_MAX_VALUE_LEN: Final[int] = 32


#: Minimum field count the decoder accepts — ``<uid>`` on its own when
#: the slot is empty. Hardware validation (2026-04-17) on 10v20 firmware
#: showed empty slots reply with just ``A \r`` (1 token after split);
#: the facade re-populates ``slot`` from the request in that case.
_UD_MIN_FIELDS: Final[int] = 1

#: Reply shape cutoff: fewer than this many fields means the ``value`` slot
#: is absent from the reply. The facade re-populates ``slot`` from the request.
_UD_VALUE_START_INDEX: Final[int] = 2


_ALL_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(DeviceKind)


@dataclass(frozen=True, slots=True)
class UserDataRequest:
    r"""Arguments for :data:`USER_DATA`.

    Attributes:
        slot: Which 32-char slot to read / write — ``0..3`` inclusive.
        value: ``None`` issues the read form; a string writes the new
            value. Validated pre-I/O: must be pure ASCII and ≤ 32
            characters, must not contain ``\r`` (the wire terminator)
            because that would truncate the write.
    """

    slot: int
    value: str | None = None


@dataclass(frozen=True, slots=True)
class UserData(Command[UserDataRequest, UserDataSetting]):
    r"""``UD`` — read or write one of the four 32-char user-data slots.

    Wire shape:

    - Read:  ``<uid><prefix>UD <slot>\r``
    - Write: ``<uid><prefix>UD <slot> <value>\r``

    Response: ``<uid> <slot> <value>`` where ``value`` may contain
    spaces. The decoder joins tokens after the slot into a single
    string, preserving whatever the device echoed verbatim.
    """

    name: str = "user_data"
    token: str = "UD"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_UD
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(self, ctx: DecodeContext, request: UserDataRequest) -> bytes:
        """Emit UD read or write bytes."""
        if not (0 <= request.slot <= UD_MAX_SLOT):
            raise AlicatValidationError(
                f"{self.name}: slot must be in [0, {UD_MAX_SLOT}], got {request.slot}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"slot": request.slot},
                ),
            )
        prefix = ctx.command_prefix.decode("ascii")
        head = f"{ctx.unit_id}{prefix}{self.token} {request.slot}"
        if request.value is None:
            return (head + "\r").encode("ascii")
        value = request.value
        if len(value) > UD_MAX_VALUE_LEN:
            raise AlicatValidationError(
                f"{self.name}: value must be <= {UD_MAX_VALUE_LEN} chars, got {len(value)}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"value_len": len(value)},
                ),
            )
        if "\r" in value or "\n" in value:
            raise AlicatValidationError(
                f"{self.name}: value must not contain \\r or \\n "
                "(wire-level terminators would truncate the write)",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"value_len": len(value)},
                ),
            )
        try:
            value.encode("ascii")
        except UnicodeEncodeError as err:
            raise AlicatValidationError(
                f"{self.name}: value must be pure ASCII; got non-ASCII at position {err.start}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"value": value},
                ),
            ) from err
        return f"{head} {value}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> UserDataSetting:
        """Parse ``<uid> <slot> <value...>`` into :class:`UserDataSetting`.

        ``value`` may contain spaces; the decoder re-joins everything
        after the slot field with single spaces so round-trip writes
        of multi-word strings behave predictably.
        """
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name)
        # Minimum: uid + slot (2 tokens) — some firmware returns exactly
        # that when the slot has never been written. A 3+ field reply
        # adds the value, which may contain spaces (re-joined below).
        if len(fields) < _UD_MIN_FIELDS:
            raise AlicatParseError(
                f"{self.name}: expected >={_UD_MIN_FIELDS} fields "
                f"(uid [+ slot + value]), got {len(fields)} — {text!r}",
                field_name="user_data",
                expected=f">= {_UD_MIN_FIELDS} fields",
                actual=len(fields),
                context=ErrorContext(command_name=self.name, raw_response=response),
            )
        unit_id = fields[0]
        # Empty-slot reply: only the unit id comes back. Leave slot as a
        # placeholder; the facade re-populates it from the request.
        if len(fields) == 1:
            return UserDataSetting(unit_id=unit_id, slot=-1, value="")
        slot = parse_int(fields[1], field=f"{self.name}.slot")
        value = " ".join(fields[2:]) if len(fields) > _UD_VALUE_START_INDEX else ""
        return UserDataSetting(unit_id=unit_id, slot=slot, value=value)


USER_DATA: UserData = UserData()
