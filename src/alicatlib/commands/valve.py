r"""Valve-control commands — primer §Valve control Controllers.

Four controller-only commands ship here:

- :data:`HOLD_VALVES` (``HP``, 5v07+) — hold valve(s) at current
  position; the device replies with a data frame and the :attr:`HLD`
  status bit set.
- :data:`HOLD_VALVES_CLOSED` (``HC``, 5v07+) — force valve(s) closed.
  Interrupts closed-loop control; marked ``destructive=True`` so
  callers must pass ``confirm=True`` explicitly.
- :data:`CANCEL_VALVE_HOLD` (``C``) — release any hold and resume
  closed-loop control. The device replies with a data frame *without*
  the ``HLD`` status bit. No firmware cutoff — the primer documents
  this as universally available.
- :data:`VALVE_DRIVE` (``VD``, 8v18+) — query valve drive state;
  replies with one to three percentages (single / dual / triple-valve
  controllers respectively). Do not infer valve count from the column
  count — use :attr:`Capability.MULTI_VALVE` / :attr:`THIRD_VALVE`
  instead (design §9).

All four are gated to
``{DeviceKind.FLOW_CONTROLLER, DeviceKind.PRESSURE_CONTROLLER}`` —
meters have no valves to drive.

Design reference: ``docs/design.md`` §9 (Tier-2 controller scope).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import ValveDriveState
from alicatlib.errors import AlicatParseError, ErrorContext
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_fields, parse_float

if TYPE_CHECKING:
    from alicatlib.devices.data_frame import ParsedFrame

__all__ = [
    "CANCEL_VALVE_HOLD",
    "HOLD_VALVES",
    "HOLD_VALVES_CLOSED",
    "VALVE_DRIVE",
    "CancelValveHold",
    "CancelValveHoldRequest",
    "HoldValves",
    "HoldValvesClosed",
    "HoldValvesClosedRequest",
    "HoldValvesRequest",
    "ValveDrive",
    "ValveDriveRequest",
]


_CONTROLLER_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(
    {DeviceKind.FLOW_CONTROLLER, DeviceKind.PRESSURE_CONTROLLER},
)


# Primer p. 25: ``HP`` / ``HC`` are 5v07+ within V1_V7; V8_V9 and V10
# support them unconditionally. ``min_firmware`` only compares within
# the declared family (design §5.10), so setting it to 5v07 gates only
# pre-5v07 V1_V7 devices — exactly what we want.
_MIN_FIRMWARE_HOLD: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V1_V7,
    major=5,
    minor=7,
    raw="5v07",
)


# Primer p. 25: ``VD`` is 8v18+ within V8_V9; V10 supports it
# unconditionally; V1_V7 and GP do not. We gate firmware families to
# {V8_V9, V10} and set ``min_firmware=8v18`` so pre-8v18 V8_V9 devices
# fail pre-I/O.
_MIN_FIRMWARE_VD: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V8_V9,
    major=8,
    minor=18,
    raw="8v18",
)


_ALL_CONTROLLER_FIRMWARE_FAMILIES: Final[frozenset[FirmwareFamily]] = frozenset(
    {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10},
)


# ``VD`` reply is ``<uid>`` followed by 1..4 valve percentages.
# Design §16.6 flagged that the 10v20 firmware returns a fixed-width
# four-column reply even on meter / single-valve hardware (the extra
# columns are zeros), and hardware validation (2026-04-17) confirmed this
# on a real single-valve flow controller — the ``A 100.00 000.00
# 000.00 000.00`` shape is load-bearing. Keep the min at ``uid + 1``
# for the older families that really do omit unused columns.
_VD_MIN_FIELDS: Final[int] = 2  # uid + 1 percentage
_VD_MAX_FIELDS: Final[int] = 5  # uid + 4 percentages


def _decode_hold_frame(
    command_name: str,
    response: bytes | tuple[bytes, ...],
    ctx: DecodeContext,
) -> ParsedFrame:
    """Parse a post-op data-frame reply shared by ``HP`` / ``HC`` / ``C``.

    All three commands respond with a full data frame — the caller's
    facade (:class:`~alicatlib.devices._controller._ControllerMixin`)
    wraps the parsed frame into a :class:`ValveHoldResult` at
    facade-level timing, mirroring the tare / legacy-setpoint pattern.
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
# HOLD_VALVES (``HP``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HoldValvesRequest:
    """Arguments for :data:`HOLD_VALVES` (empty — ``HP`` takes no arguments)."""


@dataclass(frozen=True, slots=True)
class HoldValves(Command[HoldValvesRequest, "ParsedFrame"]):
    r"""``HP`` — hold valve(s) at current position (5v07+).

    Wire: ``<uid><prefix>HP\r``. Response is a post-op data frame
    with :attr:`StatusCode.HLD` active. Closed-loop control pauses
    until :data:`CANCEL_VALVE_HOLD` is sent.
    """

    name: str = "hold_valves"
    token: str = "HP"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_HOLD
    firmware_families: frozenset[FirmwareFamily] = _ALL_CONTROLLER_FIRMWARE_FAMILIES

    def encode(self, ctx: DecodeContext, request: HoldValvesRequest) -> bytes:
        r"""Emit ``<uid><prefix>HP\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-op data frame."""
        return _decode_hold_frame(self.name, response, ctx)


HOLD_VALVES: HoldValves = HoldValves()


# ---------------------------------------------------------------------------
# HOLD_VALVES_CLOSED (``HC``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HoldValvesClosedRequest:
    """Arguments for :data:`HOLD_VALVES_CLOSED`.

    Attributes:
        confirm: Must be ``True`` — ``HC`` forces valves closed and
            interrupts any in-flight closed-loop control, which on a
            live process can be surprising. The session's destructive-
            confirm gate raises :class:`AlicatValidationError` when
            this is ``False`` (design §5.4 gating step 5).
    """

    confirm: bool = False


@dataclass(frozen=True, slots=True)
class HoldValvesClosed(Command[HoldValvesClosedRequest, "ParsedFrame"]):
    r"""``HC`` — hold valves closed (5v07+); destructive.

    Wire: ``<uid><prefix>HC\r``. Response is a post-op data frame with
    :attr:`StatusCode.HLD` active; flow and closed-loop control both
    stop. ``destructive=True`` forces explicit ``confirm=True`` on the
    request.
    """

    name: str = "hold_valves_closed"
    token: str = "HC"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_HOLD
    firmware_families: frozenset[FirmwareFamily] = _ALL_CONTROLLER_FIRMWARE_FAMILIES
    destructive: bool = True

    def encode(self, ctx: DecodeContext, request: HoldValvesClosedRequest) -> bytes:
        r"""Emit ``<uid><prefix>HC\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-op data frame."""
        return _decode_hold_frame(self.name, response, ctx)


HOLD_VALVES_CLOSED: HoldValvesClosed = HoldValvesClosed()


# ---------------------------------------------------------------------------
# CANCEL_VALVE_HOLD (``C``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CancelValveHoldRequest:
    """Arguments for :data:`CANCEL_VALVE_HOLD` (empty — ``C`` takes no arguments)."""


@dataclass(frozen=True, slots=True)
class CancelValveHold(Command[CancelValveHoldRequest, "ParsedFrame"]):
    r"""``C`` — cancel valve hold, resume closed-loop control.

    Wire: ``<uid><prefix>C\r``. Response is a post-op data frame
    *without* the :attr:`StatusCode.HLD` bit. Safe to issue even when
    no hold is active (the primer documents a successful data-frame
    reply in that case).

    No primer firmware cutoff — the command is documented as
    universally available across V1_V7, V8_V9, V10. GP behaviour is
    not documented; the firmware-family gate excludes GP to stay
    conservative until a capture confirms.
    """

    name: str = "cancel_valve_hold"
    token: str = "C"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    firmware_families: frozenset[FirmwareFamily] = _ALL_CONTROLLER_FIRMWARE_FAMILIES

    def encode(self, ctx: DecodeContext, request: CancelValveHoldRequest) -> bytes:
        r"""Emit ``<uid><prefix>C\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-op data frame."""
        return _decode_hold_frame(self.name, response, ctx)


CANCEL_VALVE_HOLD: CancelValveHold = CancelValveHold()


# ---------------------------------------------------------------------------
# VALVE_DRIVE (``VD``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValveDriveRequest:
    """Arguments for :data:`VALVE_DRIVE` (empty — ``VD`` takes no arguments)."""


@dataclass(frozen=True, slots=True)
class ValveDrive(Command[ValveDriveRequest, ValveDriveState]):
    r"""``VD`` — query valve drive state (8v18+).

    Wire: ``<uid><prefix>VD\r``. Response is 2–4 whitespace-separated
    tokens: ``<uid> <pct1> [<pct2>] [<pct3>]``. The decoder returns a
    :class:`ValveDriveState` whose ``valves`` tuple carries all
    reported percentages.

    Column count reflects the *physical* valve count of the
    controller, but users should gate by :attr:`Capability.MULTI_VALVE`
    / :attr:`THIRD_VALVE` rather than infer from the reply (design §9):
    the capability flags are probed once at ``open_device`` and
    survive firmware quirks that VD's column count does not.
    """

    name: str = "valve_drive"
    token: str = "VD"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_VD
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(self, ctx: DecodeContext, request: ValveDriveRequest) -> bytes:
        r"""Emit ``<uid><prefix>VD\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ValveDriveState:
        """Parse ``<uid> <pct1> [<pct2>] [<pct3>]`` into :class:`ValveDriveState`."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name)
        if len(fields) < _VD_MIN_FIELDS or len(fields) > _VD_MAX_FIELDS:
            raise AlicatParseError(
                f"{self.name}: expected {_VD_MIN_FIELDS}..{_VD_MAX_FIELDS} fields "
                f"(uid + 1..4 percentages), got {len(fields)} — {text!r}",
                field_name="valve_drive",
                expected=f"{_VD_MIN_FIELDS}..{_VD_MAX_FIELDS} fields",
                actual=len(fields),
                context=ErrorContext(command_name=self.name, raw_response=response),
            )
        unit_id = fields[0]
        valves = tuple(
            parse_float(f, field=f"{self.name}.valve[{i}]") for i, f in enumerate(fields[1:])
        )
        return ValveDriveState(unit_id=unit_id, valves=valves)


VALVE_DRIVE: ValveDrive = ValveDrive()
