"""Tare commands — primer §Tare (``T``, ``TP``, ``PC``, ``ZCA``, ``ZCP``).

Three zero-referencing commands plus two automated-tare commands:

- :data:`TARE_FLOW` (``T``) — set flow-reading zero. Precondition
  (caller's responsibility): no flow through the device. Applicable to
  flow meters / controllers.
- :data:`TARE_GAUGE_PRESSURE` (``TP``) — set gauge-pressure zero.
  Precondition: line depressurised to atmosphere. Applicable to flow
  and pressure devices.
- :data:`TARE_ABSOLUTE_PRESSURE` (``PC``) — calibrate the absolute
  pressure reading against the onboard barometer. Gated on
  :attr:`Capability.TAREABLE_ABSOLUTE_PRESSURE` (NOT ``BAROMETER``;
  see design §16.6.7 — flow-controller devices report a computed
  barometer reading but don't have a tareable process-port abs
  sensor). Devices without the capability raise
  :class:`AlicatMissingHardwareError` pre-I/O.
- :data:`AUTO_TARE` (``ZCA``, V10 10v05+, controllers) — query or
  set the auto-tare-on-zero-setpoint behaviour + its settling delay.
- :data:`POWER_UP_TARE` (``ZCP``, V10 10v05+, all devices) — query
  or toggle the one-shot 0.25 s tare at power-on.

Preconditions (no-flow, line-depressurised) cannot be verified by
the library; facade methods emit an INFO log naming the expected
precondition at call-time so the expectation is in the record (§5.18
pt 6).

All three respond with a post-op data frame — the encoder returns a
:class:`~alicatlib.devices.data_frame.ParsedFrame`; the facade wraps
into a :class:`DataFrame` with timing captured at the session layer
(mirrors the pattern used by :data:`GAS_SELECT_LEGACY`).

Design reference: ``docs/design.md`` §5.4 (command pattern), §5.20
(safety tiers), §9 Tier 1 (gas/units/tare coverage).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from alicatlib.commands.base import Capability, Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import AutoTareState, PowerUpTareState
from alicatlib.errors import (
    AlicatParseError,
    AlicatValidationError,
    ErrorContext,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_bool_code, parse_fields, parse_float

if TYPE_CHECKING:
    from alicatlib.devices.data_frame import ParsedFrame

__all__ = [
    "AUTO_TARE",
    "POWER_UP_TARE",
    "TARE_ABSOLUTE_PRESSURE",
    "TARE_FLOW",
    "TARE_GAUGE_PRESSURE",
    "ZCA_DELAY_MAX_S",
    "ZCA_DELAY_MIN_S",
    "AutoTare",
    "AutoTareRequest",
    "PowerUpTare",
    "PowerUpTareRequest",
    "TareAbsolutePressure",
    "TareAbsolutePressureRequest",
    "TareFlow",
    "TareFlowRequest",
    "TareGaugePressure",
    "TareGaugePressureRequest",
]


# V10 10v05+ is the firmware cutoff primer pins on both ZCA and ZCP.
_MIN_FIRMWARE_ZCA_ZCP: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=5,
    raw="10v05",
)

#: Minimum auto-tare settling delay in seconds per primer p. 18.
ZCA_DELAY_MIN_S: Final[float] = 0.1
#: Maximum auto-tare settling delay in seconds per primer p. 18.
ZCA_DELAY_MAX_S: Final[float] = 25.5


_FLOW_DEVICE_KINDS: frozenset[DeviceKind] = frozenset(
    {DeviceKind.FLOW_METER, DeviceKind.FLOW_CONTROLLER},
)
_PRESSURE_AWARE_DEVICE_KINDS: frozenset[DeviceKind] = frozenset(
    {
        DeviceKind.FLOW_METER,
        DeviceKind.FLOW_CONTROLLER,
        DeviceKind.PRESSURE_METER,
        DeviceKind.PRESSURE_CONTROLLER,
    },
)


def _encode_bare_tare(command_token: str, ctx: DecodeContext) -> bytes:
    r"""Emit ``<unit_id><prefix><token>\r`` — the only wire shape any tare uses.

    Every tare command is a bare-token imperative with no arguments;
    the only variable per-command is ``token``. Shared helper so the
    three command classes stay three-line ``encode`` methods.
    """
    prefix = ctx.command_prefix.decode("ascii")
    return f"{ctx.unit_id}{prefix}{command_token}\r".encode("ascii")


def _decode_tare_frame(
    command_name: str,
    response: bytes | tuple[bytes, ...],
    ctx: DecodeContext,
) -> ParsedFrame:
    """Parse the post-tare data frame against ``ctx.data_frame_format``.

    Mirrors :meth:`GasSelectLegacy.decode` — tare responses are data
    frames, so the session must have cached ``??D*`` before dispatch.
    The facade wraps the returned :class:`ParsedFrame` in a
    :class:`DataFrame` with read-site timing captured at the facade
    layer.
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
# TARE_FLOW (``T``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TareFlowRequest:
    """Arguments for :data:`TARE_FLOW` — no user-provided fields."""


@dataclass(frozen=True, slots=True)
class TareFlow(Command[TareFlowRequest, "ParsedFrame"]):
    r"""``T`` — tare the flow reading.

    Precondition (caller's responsibility): no gas is flowing through
    the device. The library cannot verify this — the facade emits an
    INFO log noting the precondition on every call.
    """

    name: str = "tare_flow"
    token: str = "T"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _FLOW_DEVICE_KINDS
    # Tare commands are silent on the GP07R100 capture (design §16.6.8).
    # Gate out of GP at spec level so the session raises a clean
    # AlicatFirmwareError pre-I/O rather than timing out.
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(self, ctx: DecodeContext, request: TareFlowRequest) -> bytes:
        r"""Emit ``<unit_id><prefix>T\r``."""
        del request
        return _encode_bare_tare(self.token, ctx)

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-tare data frame."""
        return _decode_tare_frame(self.name, response, ctx)


TARE_FLOW: TareFlow = TareFlow()


# ---------------------------------------------------------------------------
# TARE_GAUGE_PRESSURE (``TP``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TareGaugePressureRequest:
    """Arguments for :data:`TARE_GAUGE_PRESSURE` — no user-provided fields."""


@dataclass(frozen=True, slots=True)
class TareGaugePressure(Command[TareGaugePressureRequest, "ParsedFrame"]):
    r"""``TP`` — tare the gauge-pressure reading.

    Precondition (caller's responsibility): line depressurised to
    atmosphere. Applies to both flow devices (which carry a pressure
    transducer for compensation) and pressure devices.
    """

    name: str = "tare_gauge_pressure"
    token: str = "TP"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _PRESSURE_AWARE_DEVICE_KINDS
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(self, ctx: DecodeContext, request: TareGaugePressureRequest) -> bytes:
        r"""Emit ``<unit_id><prefix>TP\r``."""
        del request
        return _encode_bare_tare(self.token, ctx)

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-tare data frame."""
        return _decode_tare_frame(self.name, response, ctx)


TARE_GAUGE_PRESSURE: TareGaugePressure = TareGaugePressure()


# ---------------------------------------------------------------------------
# TARE_ABSOLUTE_PRESSURE (``PC``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TareAbsolutePressureRequest:
    """Arguments for :data:`TARE_ABSOLUTE_PRESSURE` — no user-provided fields."""


@dataclass(frozen=True, slots=True)
class TareAbsolutePressure(Command[TareAbsolutePressureRequest, "ParsedFrame"]):
    r"""``PC`` — calibrate absolute pressure against the onboard barometer.

    Gated on :attr:`Capability.TAREABLE_ABSOLUTE_PRESSURE` — NOT on
    :attr:`Capability.BAROMETER`. Hardware validation on 2026-04-17 established
    that flow-controller devices report a firmware-computed barometer
    reading (so ``BAROMETER`` probes positive) but do not have a
    process-port absolute-pressure sensor and therefore reject or
    silently ignore ``PC``. Four devices confirmed the pattern (8v17
    MCR-200, 8v30 MCR-500, 6v21 MCR-775, 7v09 MCP-50); see design
    §16.6.7 for the narrative and ``Capability.BAROMETER`` /
    :attr:`Capability.TAREABLE_ABSOLUTE_PRESSURE` for the semantic split.

    No safe probe for ``TAREABLE_ABSOLUTE_PRESSURE`` exists (probing
    would tare the device). Users with a pressure meter/controller that
    supports ``PC`` opt in via ``assume_capabilities`` on
    :func:`~alicatlib.devices.factory.open_device`. Devices without the
    capability raise :class:`AlicatMissingHardwareError` pre-I/O.

    Precondition: the gauge pressure should be at atmosphere — the
    device uses its barometer reading as the reference.
    """

    name: str = "tare_absolute_pressure"
    token: str = "PC"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _PRESSURE_AWARE_DEVICE_KINDS
    required_capabilities: Capability = Capability.TAREABLE_ABSOLUTE_PRESSURE
    # Primer lists PC at 6v00+. GP firmware is silent on this command
    # (design §16.6.8); keep it out of GP at the spec level.
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(
        self,
        ctx: DecodeContext,
        request: TareAbsolutePressureRequest,
    ) -> bytes:
        r"""Emit ``<unit_id><prefix>PC\r``."""
        del request
        return _encode_bare_tare(self.token, ctx)

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-tare data frame."""
        return _decode_tare_frame(self.name, response, ctx)


TARE_ABSOLUTE_PRESSURE: TareAbsolutePressure = TareAbsolutePressure()


# ---------------------------------------------------------------------------
# AUTO_TARE (``ZCA``) — controllers, 10v05+
# ---------------------------------------------------------------------------


_CONTROLLER_DEVICE_KINDS: frozenset[DeviceKind] = frozenset(
    {DeviceKind.FLOW_CONTROLLER, DeviceKind.PRESSURE_CONTROLLER},
)

_ALL_DEVICE_KINDS: frozenset[DeviceKind] = frozenset(DeviceKind)


@dataclass(frozen=True, slots=True)
class AutoTareRequest:
    """Arguments for :data:`AUTO_TARE`.

    Attributes:
        enable: ``True`` enables auto-tare on zero-setpoint;
            ``False`` disables. ``None`` issues the query form.
        delay_s: Settling delay in seconds before the device tares
            after seeing a zero setpoint. Primer constrains this to
            ``[0.1, 25.5]``. Required when enabling (``enable=True``);
            ignored and omitted from the wire when disabling
            (``enable=False``). See :class:`AutoTare` for the wire
            form the disable path emits.
    """

    enable: bool | None = None
    delay_s: float | None = None


@dataclass(frozen=True, slots=True)
class AutoTare(Command[AutoTareRequest, AutoTareState]):
    r"""``ZCA`` — auto-tare-on-zero-setpoint query/set (controllers, 10v05+).

    Wire shape (hardware-corrected; primer p. 18 is incomplete):

    - Query:   ``<uid><prefix>ZCA\r``
    - Enable:  ``<uid><prefix>ZCA 1 <delay>\r``
    - Disable: ``<uid><prefix>ZCA 0\r``  (no delay field — see §15.3)

    Primer documents the set form as always carrying both slots,
    with ``ZCA <uid> 0 0`` disabling auto-tare. Hardware validation
    (2026-04-17) confirmed on two 10v20 units that ``ZCA 0 0``
    **rejects with ``?``** — the device does not accept a zero delay
    in the disable form. The wire-form probe also confirmed that the
    shortest accepted disable form is ``ZCA 0`` with no delay field
    (``ZCA 0 0.1`` / ``ZCA 0 1`` / ``ZCA 0 1.0`` also work, but all
    land in the same ``enabled=0 delay=0.0`` state per the reply).
    The encoder emits the bare-``0`` form for ``enable=False``.

    Response: ``<uid> <enable> <delay>`` (3 fields) for both enable
    and disable paths.
    """

    name: str = "auto_tare"
    token: str = "ZCA"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_ZCA_ZCP
    firmware_families: frozenset[FirmwareFamily] = frozenset({FirmwareFamily.V10})

    def encode(self, ctx: DecodeContext, request: AutoTareRequest) -> bytes:
        """Emit ZCA query or set bytes.

        Disable uses the shortest accepted form (``ZCA 0``) rather
        than the primer's ``ZCA 0 0`` — the latter rejects on real
        10v20 (see §16.6.10 + class docstring).
        """
        prefix = ctx.command_prefix.decode("ascii")
        head = f"{ctx.unit_id}{prefix}{self.token}"
        if request.enable is None:
            return f"{head}\r".encode("ascii")
        # Disable form — no delay field on the wire.
        if not request.enable:
            return f"{head} 0\r".encode("ascii")
        # Enable form — delay required.
        delay = request.delay_s
        if delay is None:
            raise AlicatValidationError(
                f"{self.name}: delay_s is required when enabling auto-tare "
                f"(range {ZCA_DELAY_MIN_S}..{ZCA_DELAY_MAX_S} seconds)",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                ),
            )
        if delay < ZCA_DELAY_MIN_S or delay > ZCA_DELAY_MAX_S:
            raise AlicatValidationError(
                f"{self.name}: delay_s must be in "
                f"[{ZCA_DELAY_MIN_S}, {ZCA_DELAY_MAX_S}] seconds, got {delay}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"delay_s": delay},
                ),
            )
        return f"{head} 1 {delay}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> AutoTareState:
        """Parse ``<uid> <enable> <delay>`` into :class:`AutoTareState`."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=3)
        unit_id, enable_s, delay_s = fields
        return AutoTareState(
            unit_id=unit_id,
            enabled=parse_bool_code(enable_s, field=f"{self.name}.enabled"),
            delay_s=parse_float(delay_s, field=f"{self.name}.delay_s"),
        )


AUTO_TARE: AutoTare = AutoTare()


# ---------------------------------------------------------------------------
# POWER_UP_TARE (``ZCP``) — all devices, 10v05+
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PowerUpTareRequest:
    """Arguments for :data:`POWER_UP_TARE`.

    Attributes:
        enable: ``True`` / ``False`` sets power-up tare; ``None``
            issues the query form.
    """

    enable: bool | None = None


@dataclass(frozen=True, slots=True)
class PowerUpTare(Command[PowerUpTareRequest, PowerUpTareState]):
    r"""``ZCP`` — power-up tare query/set (all devices, V10 10v05+).

    Wire shape (primer p. 19):

    - Query: ``<uid><prefix>ZCP\r``
    - Set:   ``<uid><prefix>ZCP <enable>\r``

    Response: ``<uid> <enable>`` (2 fields).
    """

    name: str = "power_up_tare"
    token: str = "ZCP"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_ZCA_ZCP
    firmware_families: frozenset[FirmwareFamily] = frozenset({FirmwareFamily.V10})

    def encode(self, ctx: DecodeContext, request: PowerUpTareRequest) -> bytes:
        """Emit ZCP query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.enable is None:
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token} {int(request.enable)}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> PowerUpTareState:
        """Parse ``<uid> <enable>`` into :class:`PowerUpTareState`."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=2)
        unit_id, enable_s = fields
        return PowerUpTareState(
            unit_id=unit_id,
            enabled=parse_bool_code(enable_s, field=f"{self.name}.enabled"),
        )


POWER_UP_TARE: PowerUpTare = PowerUpTare()
