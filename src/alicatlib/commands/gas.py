"""Gas commands — primer §Active Gas (10v05+), §Set Gas, §Gas List.

This module carries three command specs:

- :data:`GAS_SELECT` (``GS``) — modern active-gas get/set, V10 ≥ 10v05.
- :data:`GAS_SELECT_LEGACY` (``G``) — legacy set-gas for every other
  supported firmware (all V1_V7, V8_V9, GP, and V10 < 10v05). The
  response is a post-op data frame rather than the modern 4-field reply;
  the decoder returns a :class:`~alicatlib.devices.data_frame.ParsedFrame`
  and the facade method (:meth:`FlowMeter.gas`) fabricates a
  :class:`GasState` by combining the request's gas code with the
  frame's unit id. Legacy ``G`` has no query form and no ``save`` flag.
- :data:`GAS_LIST` (``??G*``) — enumerate built-in + mixture gases.
  Multiline response keyed by primer gas code.

Firmware-gated dispatch between the modern and legacy pair is handled
at the facade via
:func:`alicatlib.commands._firmware_cutoffs.uses_modern_gas_select`.

Design reference: ``docs/design.md`` §5.4 (example + legacy-path pairs),
§5.5 (models), §5.11 (``parse_gas_list``), §9 Tier 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from alicatlib.commands._firmware_cutoffs import MIN_FIRMWARE_GAS_SELECT
from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.medium import Medium
from alicatlib.errors import AlicatParseError, ErrorContext
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_fields, parse_gas_list, parse_int
from alicatlib.registry import Gas, gas_registry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from alicatlib.devices.data_frame import ParsedFrame

__all__ = [
    "GAS_LIST",
    "GAS_SELECT",
    "GAS_SELECT_LEGACY",
    "GasList",
    "GasListRequest",
    "GasSelect",
    "GasSelectLegacy",
    "GasSelectLegacyRequest",
    "GasSelectRequest",
    "GasState",
]


# Re-exported for backwards compatibility with callers that imported the
# private constant before it moved to :mod:`commands._firmware_cutoffs`.
_MIN_FIRMWARE_GAS_SELECT = MIN_FIRMWARE_GAS_SELECT


# Upper bound for the legacy ``G`` command inside the V10 family — the
# last V10 release that still used the legacy wire form. Module-level
# constant (rather than a dataclass default calling ``FirmwareVersion(...)``)
# because ``RUF009`` rejects function calls as frozen-dataclass defaults.
_MAX_FIRMWARE_GAS_SELECT_LEGACY_V10: FirmwareVersion = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=4,
    raw="10v04",
)


# Safety cap on ``??G*`` field count. The primer gas registry tops out
# at code 255 (custom mixtures occupy 236–255), so 256 rows is the
# hard maximum. Protocol-level reads hit the count-header predicate
# first and terminate precisely; this cap only matters when a device
# omits the header.
_GAS_LIST_MAX_LINES = 256

# Minimum token count on a ``<uid> G<NN> <count>`` header line.
_GAS_LIST_HEADER_MIN_TOKENS = 3


@dataclass(frozen=True, slots=True)
class GasSelectRequest:
    """Arguments for :data:`GAS_SELECT`.

    Attributes:
        gas: Gas to select. Accepts a :class:`Gas` enum member, its primer
            short name (``"N2"``), its long name (``"Nitrogen"``), or any
            registered alias. ``None`` issues the *query* form (``GS`` with
            no argument) which reads back the active gas without changing it.
        save: If ``True``, persist the selection to EEPROM. ``False`` keeps
            it volatile (lost on power cycle). ``None`` — the default —
            omits the flag, which matches the device's own default
            behavior (volatile). See design §5.20 for the EEPROM-wear
            guard that fires on hot-looped ``save=True`` calls.
    """

    gas: Gas | str | None = None
    save: bool | None = None


@dataclass(frozen=True, slots=True)
class GasState:
    """Active-gas response.

    Populated from a four-field reply: ``<unit_id> <code> <short> <long>``.

    Attributes:
        unit_id: Echoed unit id (``"A"``..``"Z"``).
        code: Numeric gas code (primer Appendix C). Redundant with
            ``gas.code`` but preserved because some (pre-10v05) firmwares
            emit a code the registry hasn't seen — in which case
            :func:`gas_registry.by_code` raises and users can still read
            the raw code off the exception.
        gas: The typed :class:`Gas` enum member.
        label: Short primer label as echoed by the device (usually matches
            ``gas.value``; device may send a custom label for mixture
            slots 236–255).
        long_name: Long primer name as echoed by the device.
    """

    unit_id: str
    code: int
    gas: Gas
    label: str
    long_name: str


@dataclass(frozen=True, slots=True)
class GasSelect(Command[GasSelectRequest, GasState]):
    """Active-gas command (``GS``, 10v05+).

    Both get (``GS``) and set (``GS <code> [save]``) share a single command
    spec — the request's ``gas`` field picks the mode. The facade routes
    firmwares < 10v05 to :data:`GAS_SELECT_LEGACY`
    (see :func:`~alicatlib.commands._firmware_cutoffs.uses_modern_gas_select`).
    """

    name: str = "gas_select"
    token: str = "GS"  # noqa: S105  — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = frozenset(
        {DeviceKind.FLOW_METER, DeviceKind.FLOW_CONTROLLER},
    )
    media: Medium = Medium.GAS
    min_firmware: FirmwareVersion | None = MIN_FIRMWARE_GAS_SELECT
    firmware_families: frozenset[FirmwareFamily] = frozenset({FirmwareFamily.V10})

    def encode(self, ctx: DecodeContext, request: GasSelectRequest) -> bytes:
        """Emit the wire bytes for a GS query or set command."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.gas is None:
            # Query form: no gas argument, no save flag.
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        gas = gas_registry.coerce(request.gas)
        if request.save is None:
            body = f"{ctx.unit_id}{prefix}{self.token} {gas.code}"
        else:
            save_flag = "1" if request.save else "0"
            body = f"{ctx.unit_id}{prefix}{self.token} {gas.code} {save_flag}"
        return body.encode("ascii") + b"\r"

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> GasState:
        """Parse the four-field GS reply into a typed :class:`GasState`."""
        del ctx  # kept for signature uniformity across commands
        if isinstance(response, tuple):
            # A LINE command should never receive a multi-line response; the
            # session guarantees this, but guard against a mis-configured spec.
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=4)
        unit_id, code_s, label, long_name = fields
        code = parse_int(code_s, field="code")
        return GasState(
            unit_id=unit_id,
            code=code,
            gas=gas_registry.by_code(code),
            label=label,
            long_name=long_name,
        )


#: Module-level singleton — the canonical spec (see design §5.4).
GAS_SELECT: GasSelect = GasSelect()


# ---------------------------------------------------------------------------
# GAS_SELECT_LEGACY (``G <gas_code>``) — pre-10v05 + non-V10 families.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GasSelectLegacyRequest:
    """Arguments for :data:`GAS_SELECT_LEGACY`.

    Legacy ``G`` is *set only* — there is no query form and no ``save``
    flag. The facade (:meth:`FlowMeter.gas`) raises
    :class:`AlicatUnsupportedCommandError` if a caller asks for a query
    while the device is on firmware that only supports the legacy path;
    likewise it raises :class:`AlicatValidationError` if ``save=True``
    is passed to a legacy dispatch.

    Attributes:
        gas: Gas to select. Accepts a :class:`Gas` enum member, its primer
            short name, long name, or any registered alias. Required.
    """

    gas: Gas | str


@dataclass(frozen=True, slots=True)
class GasSelectLegacy(Command[GasSelectLegacyRequest, "ParsedFrame"]):
    r"""Legacy set-gas command (``G``) for firmware older than V10 ≥ 10v05.

    Per design §5.4, the device replies with a post-op data frame
    rather than the modern 4-field ``<uid> <code> <short> <long>``
    reply. This command's decoder returns the raw
    :class:`ParsedFrame` so the facade can stitch a :class:`GasState`
    from (a) the gas code the caller sent — known at the facade but
    not at the decoder — and (b) the frame's echoed unit id.

    Gating: ``firmware_families`` lists every family the legacy path
    applies to; ``max_firmware`` set to ``10v04`` inside
    :attr:`FirmwareFamily.V10` blocks V10 ≥ 10v05 specifically.
    Because the session's range check is family-scoped (design §5.10),
    the V10 upper bound does not leak into the gating decisions for
    GP / V1_V7 / V8_V9.
    """

    name: str = "gas_select_legacy"
    token: str = "G"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = frozenset(
        {DeviceKind.FLOW_METER, DeviceKind.FLOW_CONTROLLER},
    )
    media: Medium = Medium.GAS
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {
            FirmwareFamily.GP,
            FirmwareFamily.V1_V7,
            FirmwareFamily.V8_V9,
            FirmwareFamily.V10,
        },
    )
    # Upper bound is the last V10 release that still used the legacy
    # wire form. Cross-family comparisons are rejected by the session
    # (design §5.10), so this bound only fires for devices actually on
    # the V10 family; other families reach this command unconditionally.
    max_firmware: FirmwareVersion | None = _MAX_FIRMWARE_GAS_SELECT_LEGACY_V10

    def encode(self, ctx: DecodeContext, request: GasSelectLegacyRequest) -> bytes:
        r"""Emit ``<unit_id><prefix>G <gas_code>\r``."""
        gas = gas_registry.coerce(request.gas)
        prefix = ctx.command_prefix.decode("ascii")
        body = f"{ctx.unit_id}{prefix}{self.token} {gas.code}"
        return body.encode("ascii") + b"\r"

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-set data frame against ``ctx.data_frame_format``.

        The session caches the format at startup; callers hitting this
        decoder before the format has been probed get an
        :class:`AlicatParseError` pointing at the missing probe — same
        failure mode as :data:`~alicatlib.commands.polling.POLL_DATA`.
        """
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        if ctx.data_frame_format is None:
            raise AlicatParseError(
                f"{self.name} requires ctx.data_frame_format; session must probe ??D* first",
                field_name="data_frame_format",
                expected="DataFrameFormat",
                actual=None,
                context=ErrorContext(command_name=self.name, raw_response=response),
            )
        return ctx.data_frame_format.parse(response)


GAS_SELECT_LEGACY: GasSelectLegacy = GasSelectLegacy()


# ---------------------------------------------------------------------------
# GAS_LIST (``??G*``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GasListRequest:
    """Arguments for :data:`GAS_LIST` — no user-provided fields."""


def _gas_list_is_complete(lines: Sequence[bytes]) -> bool:
    """Terminate ``??G*`` when the leading count-header has been satisfied.

    Like ``??D*``, a typical ``??G*`` response begins with a header line
    declaring the row count that follows (``<uid> G01 <count>``, exactly
    three tokens with the third purely numeric). When present we
    terminate exactly; when absent — the first line is already an
    entry (``<uid> G<NN> <code> <label...>``, four or more tokens) — we
    fall through to the protocol client's idle-timeout fallback.
    Hardware-captured fixtures will determine which shape is canonical.
    """
    if not lines:
        return False
    tokens = lines[0].decode("ascii", errors="replace").split()
    # A header is *exactly* three tokens with a numeric count. Gas
    # entries always carry a fourth (the short-name label), so a line
    # like ``A G02 8 N2 Nitrogen`` never matches — avoiding a false
    # positive where the entry's code is read as a "count=8" header.
    if len(tokens) != _GAS_LIST_HEADER_MIN_TOKENS or not tokens[2].isdigit():
        return False
    declared = int(tokens[2])
    # Header + declared gas-entry lines.
    return len(lines) >= declared + 1


@dataclass(frozen=True, slots=True)
class GasList(Command[GasListRequest, dict[int, str]]):
    """``??G*`` — enumerate built-in and mixture gases on the device.

    Returns ``{gas_code: raw_label}``. Callers that want typed
    :class:`Gas` members should cross-reference the code against
    :func:`alicatlib.registry.gas_registry.by_code`; unknown codes
    (custom mixtures, legacy compat slots) are preserved as raw
    labels so diagnostics retain them.
    """

    name: str = "gas_list"
    token: str = "??G*"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINES
    device_kinds: frozenset[DeviceKind] = frozenset(
        {DeviceKind.FLOW_METER, DeviceKind.FLOW_CONTROLLER},
    )
    media: Medium = Medium.GAS
    expected_lines: int | None = _GAS_LIST_MAX_LINES
    # GP reads go prefix-less (design §16.6.8).
    prefix_less: bool = True

    def encode(self, ctx: DecodeContext, request: GasListRequest) -> bytes:
        r"""Emit ``<unit_id><prefix>??G*\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> dict[int, str]:
        """Parse the multi-line gas list into ``{gas_code: label}``."""
        del ctx
        if not isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected multi-line response, got single line",
            )
        return parse_gas_list(response)


GAS_LIST: GasList = GasList(is_complete=_gas_list_is_complete)
