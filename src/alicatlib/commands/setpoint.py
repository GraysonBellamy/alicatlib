"""Setpoint commands — primer §Setpoint, §Setpoint Source.

Three command specs ship here:

- :data:`SETPOINT` (``LS``) — modern setpoint get/set for controllers
  on V10 (all) and V8_V9 ≥ 9v00. Response is a post-op data frame;
  the decoder returns a :class:`~alicatlib.devices.data_frame.ParsedFrame`
  and the facade (:meth:`FlowController.setpoint`) wraps into a
  :class:`SetpointState`.
- :data:`SETPOINT_LEGACY` (``S``) — paired legacy setpoint for
  firmware older than ``9v00`` within V8_V9, plus all V1_V7. Same
  data-frame response; the facade auto-dispatches via
  :func:`~alicatlib.commands._firmware_cutoffs.uses_modern_setpoint`.
- :data:`SETPOINT_SOURCE` (``LSS``) — get/set the setpoint-source mode
  (``"S"`` serial, ``"A"`` analog, ``"U"`` user-knob). The facade
  updates the session's :attr:`Session.setpoint_source` cache on
  every call so :meth:`FlowController.setpoint` can detect the
  "analog source silently ignores serial write" failure mode
  (design §5.20 risk table).

Design reference: ``docs/design.md`` §5.4 (legacy-path pairs),
§5.5 (:class:`SetpointState`), §5.20 (safety).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from alicatlib.commands._firmware_cutoffs import MIN_FIRMWARE_LSS, MIN_FIRMWARE_SETPOINT_LS
from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import SetpointState
from alicatlib.errors import AlicatParseError, AlicatValidationError, ErrorContext
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import (
    parse_ascii,
    parse_fields,
    parse_float,
    parse_int,
)
from alicatlib.registry.units import unit_registry

if TYPE_CHECKING:
    from alicatlib.devices.data_frame import ParsedFrame
    from alicatlib.registry import Unit

__all__ = [
    "SETPOINT",
    "SETPOINT_LEGACY",
    "SETPOINT_SOURCE",
    "SETPOINT_SOURCE_MODES",
    "Setpoint",
    "SetpointLegacy",
    "SetpointLegacyRequest",
    "SetpointRequest",
    "SetpointSource",
    "SetpointSourceRequest",
    "SetpointSourceResult",
]


# Upper bound for the legacy ``S`` command inside the V8_V9 family —
# the last release before ``LS`` took over. Module-level constant so
# the dataclass default doesn't violate RUF009 (no function-call
# defaults in frozen dataclasses).
_MAX_FIRMWARE_SETPOINT_LEGACY_V8V9: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V8_V9,
    major=8,
    minor=99,
    raw="8v99",
)


#: The three mode codes the device uses for ``LSS``. Values map 1:1 to
#: the primer's short names: S = Serial, A = Analog (knob / 4–20 mA
#: loop), U = User (front-panel encoder).
SETPOINT_SOURCE_MODES: Final[frozenset[str]] = frozenset({"S", "A", "U"})


_CONTROLLER_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(
    {DeviceKind.FLOW_CONTROLLER, DeviceKind.PRESSURE_CONTROLLER},
)


def _format_setpoint_value(value: float) -> str:
    """Format ``value`` as a decimal token for the wire.

    Uses Python's default float repr — ``50.0`` stays ``"50.0"``,
    ``3.14`` stays ``"3.14"``, exponential notation is only emitted
    for extreme magnitudes. Alicat's modern firmware accepts this
    shape; older V1_V7 firmware may want fixed-precision. Hardware
    capture refines this to ``f"{value:+.2f}"`` or similar if the
    plain repr fails round-trip, per design §16.4.
    """
    return repr(value)


def _decode_setpoint_reply(
    command_name: str,
    response: bytes | tuple[bytes, ...],
    ctx: DecodeContext,
) -> SetpointState:
    """Parse the modern ``LS`` reply: ``<uid> <current> <requested> <unit_code> <unit_label>``.

    Verified against a V10 ``MC-500SCCM-D`` capture on 2026-04-17 (design
    §16.6). Both ``current`` (measured value of the controlled variable)
    and ``requested`` (the setpoint target) are present on the wire; this
    decoder extracts both directly without requiring a follow-up data
    frame parse.

    The unit is resolved from the wire label (which is unambiguous);
    the numeric unit code is parsed and stored on context for diagnostics
    but the human label is the authoritative binding.
    """
    if isinstance(response, tuple):
        raise TypeError(
            f"{command_name}.decode expected single-line response, got {len(response)} lines",
        )
    text = parse_ascii(response)
    fields = parse_fields(text, command=command_name, expected_count=5)
    unit_id, current_s, requested_s, unit_code_s, unit_label = fields
    current = parse_float(current_s, field=f"{command_name}.current")
    requested = parse_float(requested_s, field=f"{command_name}.requested")
    # Validate the unit code is numeric (we don't bind by code — the label
    # is unambiguous — but a non-numeric token here means the reply shape
    # is wrong and we should surface it loudly).
    parse_int(unit_code_s, field=f"{command_name}.unit_code")
    unit: Unit | None
    try:
        unit = unit_registry.coerce(unit_label)
    except Exception:
        unit = None
    return SetpointState(
        unit_id=unit_id,
        current=current,
        requested=requested,
        unit=unit,
        unit_label=unit_label,
        frame=None,
    )


def _decode_setpoint_frame(
    command_name: str,
    response: bytes | tuple[bytes, ...],
    ctx: DecodeContext,
) -> ParsedFrame:
    """Parse the legacy ``S`` post-op data frame.

    The legacy ``S`` command (pre-9v00) is set-only and responds with a
    full data frame, unlike the modern ``LS`` 5-field reply. The facade
    on the legacy path wraps the parsed frame into a :class:`SetpointState`
    populated from the frame's Setpoint / Mass_Flow / Vol_Flow / etc.
    fields per the loop-control variable.
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
# SETPOINT (``LS``) — modern
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetpointRequest:
    """Arguments for :data:`SETPOINT`.

    Attributes:
        value: Setpoint target in the device's current engineering
            units. ``None`` issues the query form (``LS`` alone).
    """

    value: float | None = None


@dataclass(frozen=True, slots=True)
class Setpoint(Command[SetpointRequest, SetpointState]):
    r"""``LS`` — modern setpoint get/set (V10 + V8_V9 ≥ 9v00).

    Wire shape:

    - Query: ``<uid><prefix>LS\r``
    - Set:   ``<uid><prefix>LS <value>\r``

    Response is a post-op data frame containing the updated Setpoint
    field; the decoder returns the :class:`ParsedFrame` and the
    facade converts to :class:`SetpointState` with facade-level timing
    (same pattern as tare / legacy-gas).

    Gated to :attr:`DeviceKind.FLOW_CONTROLLER` and
    :attr:`DeviceKind.PRESSURE_CONTROLLER` — a plain meter has no
    setpoint. Firmware gating handles V8_V9 < 9v00 → redirect to
    :data:`SETPOINT_LEGACY` at the facade layer.
    """

    name: str = "setpoint"
    token: str = "LS"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = MIN_FIRMWARE_SETPOINT_LS
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(self, ctx: DecodeContext, request: SetpointRequest) -> bytes:
        r"""Emit the LS query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.value is None:
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        value_s = _format_setpoint_value(request.value)
        return f"{ctx.unit_id}{prefix}{self.token} {value_s}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> SetpointState:
        """Parse the modern ``LS`` 5-field reply into :class:`SetpointState`."""
        return _decode_setpoint_reply(self.name, response, ctx)


SETPOINT: Setpoint = Setpoint()


# ---------------------------------------------------------------------------
# SETPOINT_LEGACY (``S``) — pre-9v00 firmware
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetpointLegacyRequest:
    """Arguments for :data:`SETPOINT_LEGACY`.

    Legacy ``S`` is set-only — there is no query form on firmware
    that predates ``LS``. The facade rejects ``value is None`` pre-I/O
    with :class:`AlicatUnsupportedCommandError` and routes query
    intents to the modern :data:`SETPOINT` if the firmware supports
    it.
    """

    value: float


@dataclass(frozen=True, slots=True)
class SetpointLegacy(Command[SetpointLegacyRequest, "ParsedFrame"]):
    r"""``S`` — legacy setpoint set for pre-9v00 firmware.

    Applies to :attr:`FirmwareFamily.V1_V7` (all) and
    :attr:`FirmwareFamily.V8_V9` < 9v00. Session's family-scoped
    ``max_firmware`` gate blocks V8_V9 ≥ 9v00 (redirect to
    :data:`SETPOINT`); V1_V7 has no upper bound.

    Response: post-op data frame, same shape as
    :data:`SETPOINT`.
    """

    name: str = "setpoint_legacy"
    token: str = "S"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9},
    )
    # Cross-family comparison is rejected by the session (§5.10); this
    # bound only fires on V8_V9 devices, while V1_V7 remains unbounded.
    max_firmware: FirmwareVersion | None = _MAX_FIRMWARE_SETPOINT_LEGACY_V8V9

    def encode(self, ctx: DecodeContext, request: SetpointLegacyRequest) -> bytes:
        r"""Emit ``<unit_id><prefix>S <value>\r``."""
        prefix = ctx.command_prefix.decode("ascii")
        value_s = _format_setpoint_value(request.value)
        return f"{ctx.unit_id}{prefix}{self.token} {value_s}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-op data frame."""
        return _decode_setpoint_frame(self.name, response, ctx)


SETPOINT_LEGACY: SetpointLegacy = SetpointLegacy()


# ---------------------------------------------------------------------------
# SETPOINT_SOURCE (``LSS``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetpointSourceRequest:
    """Arguments for :data:`SETPOINT_SOURCE`.

    Attributes:
        mode: ``"S"`` (serial), ``"A"`` (analog), or ``"U"``
            (user-knob). ``None`` issues the query form.
        save: ``True`` persists to EEPROM (design §5.20.7 wear-rate
            guard applies); ``None`` / ``False`` keeps the change
            volatile.
    """

    mode: str | None = None
    save: bool | None = None


@dataclass(frozen=True, slots=True)
class SetpointSourceResult:
    """Typed response for :data:`SETPOINT_SOURCE`.

    Decodes the primer's ``<uid> <mode>`` two-field reply. Keeping the
    decoded ``mode`` as a ``str`` (rather than a dedicated enum) lets
    the facade treat unknown modes as best-effort diagnostics without
    fighting an enum-coerce failure — the ``mode`` is re-validated
    against :data:`SETPOINT_SOURCE_MODES` on the facade set path.
    """

    unit_id: str
    mode: str


@dataclass(frozen=True, slots=True)
class SetpointSource(Command[SetpointSourceRequest, SetpointSourceResult]):
    r"""``LSS`` — setpoint-source get/set.

    Wire shape:

    - Query: ``<uid><prefix>LSS\r``
    - Set:   ``<uid><prefix>LSS <mode>[ <save>]\r`` — mode ∈ {S, A, U}.

    Response: ``<uid> <mode>`` (2 fields). The facade caches the
    decoded mode on :attr:`Session.setpoint_source` so
    :meth:`FlowController.setpoint` can detect the
    ``LSS=A silently ignores serial setpoint`` failure mode.
    """

    name: str = "setpoint_source"
    token: str = "LSS"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = MIN_FIRMWARE_LSS
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V10},
    )

    def encode(
        self,
        ctx: DecodeContext,
        request: SetpointSourceRequest,
    ) -> bytes:
        """Emit the LSS query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.mode is None:
            # Query form — ``save`` is set-only semantics, silently
            # ignored in query shape.
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        mode = request.mode.strip().upper()
        if mode not in SETPOINT_SOURCE_MODES:
            raise AlicatValidationError(
                f"LSS mode {request.mode!r} not one of {sorted(SETPOINT_SOURCE_MODES)}",
                context=ErrorContext(
                    command_name=self.name,
                    extra={"mode": request.mode},
                ),
            )
        body = f"{ctx.unit_id}{prefix}{self.token} {mode}"
        if request.save is not None:
            body += f" {'1' if request.save else '0'}"
        return (body + "\r").encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> SetpointSourceResult:
        """Parse ``<uid> <mode>`` into :class:`SetpointSourceResult`."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=2)
        return SetpointSourceResult(unit_id=fields[0], mode=fields[1])


SETPOINT_SOURCE: SetpointSource = SetpointSource()
