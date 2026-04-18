r"""Totalizer commands — primer §Totalizers Flow devices.

Four commands ship here; all gate to flow devices (meter or
controller) — pressure-only devices have no totalizer:

- :data:`TOTALIZER_CONFIG` (``TC``, 10v00+) — query or set totalizer
  configuration (which flow statistic, accumulation mode, overflow
  limit behaviour, digit counts). ``flow_statistic_code=1`` is the
  primer's "disable" sentinel. :data:`TotalizerMode.KEEP` /
  :data:`TotalizerLimitMode.KEEP` (value ``-1``) preserve the current
  setting when the caller only wants to change a subset.
- :data:`TOTALIZER_RESET` (``T <n>``, 8v00+) — **token-collision with
  flow tare**. Primer p. 24: ``<uid>T\r`` is flow tare (zero the
  flow reading); ``<uid>T 1\r`` / ``<uid>T 2\r`` resets totalizer 1
  / 2. The encoder **always** emits the totalizer argument so this
  spec can never produce the tare-flow wire form by accident.
  ``destructive=True`` — caller must pass ``confirm=True``.
- :data:`TOTALIZER_RESET_PEAK` (``TP <n>``, 8v00+) — **token-collision
  with gauge-pressure tare**. Same pattern: ``<uid>TP\r`` tares
  gauge pressure; ``<uid>TP 1\r`` / ``<uid>TP 2\r`` resets the peak
  on that totalizer.
- :data:`TOTALIZER_SAVE` (``TCR``, 10v05+) — query/set whether the
  device persists totalizer values across power cycles.

Design reference: ``docs/design.md`` §9 (Tier-2 all-device scope).
Note the token-collision pinning requirement above for the T/TP reset
commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import (
    TotalizerConfig,
    TotalizerId,
    TotalizerLimitMode,
    TotalizerMode,
    TotalizerSaveState,
)
from alicatlib.errors import (
    AlicatParseError,
    AlicatValidationError,
    ErrorContext,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_bool_code, parse_fields, parse_int

if TYPE_CHECKING:
    from alicatlib.devices.data_frame import ParsedFrame

__all__ = [
    "TOTALIZER_CONFIG",
    "TOTALIZER_RESET",
    "TOTALIZER_RESET_PEAK",
    "TOTALIZER_SAVE",
    "TOTALIZER_TC_DECIMAL_MAX",
    "TOTALIZER_TC_DECIMAL_MIN",
    "TOTALIZER_TC_DIGITS_MAX",
    "TOTALIZER_TC_DIGITS_MIN",
    "TotalizerConfigCommand",
    "TotalizerConfigRequest",
    "TotalizerReset",
    "TotalizerResetPeak",
    "TotalizerResetPeakRequest",
    "TotalizerResetRequest",
    "TotalizerSave",
    "TotalizerSaveRequest",
]


_FLOW_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(
    {DeviceKind.FLOW_METER, DeviceKind.FLOW_CONTROLLER},
)


# TC lands at 10v00; primer lists it as V10 only. V8_V9 and earlier
# have different totalizer plumbing; keep to V10 to avoid surprises.
_MIN_FIRMWARE_TC: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=0,
    raw="10v00",
)
_V10_ONLY: Final[frozenset[FirmwareFamily]] = frozenset({FirmwareFamily.V10})


# T / TP reset (with totalizer arg) land at 8v00 per primer p. 24.
# ``8v00`` is the floor of V8_V9, so the family gate alone gates
# pre-V8_V9 devices; adding the explicit min_firmware is still cheap
# insurance and documents the primer's cutoff.
_MIN_FIRMWARE_RESET: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V8_V9,
    major=8,
    minor=0,
    raw="8v00",
)
_V8_V9_V10: Final[frozenset[FirmwareFamily]] = frozenset(
    {FirmwareFamily.V8_V9, FirmwareFamily.V10},
)


# TCR is 10v05+ per primer p. 24.
_MIN_FIRMWARE_TCR: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=5,
    raw="10v05",
)


#: Primer p. 23: ``number_of_digits`` must be between 7 and 10 (default 7).
TOTALIZER_TC_DIGITS_MIN: Final[int] = 7
TOTALIZER_TC_DIGITS_MAX: Final[int] = 10

#: Primer p. 23: ``decimal_place`` must be between 0 and 9.
TOTALIZER_TC_DECIMAL_MIN: Final[int] = 0
TOTALIZER_TC_DECIMAL_MAX: Final[int] = 9


# TC reply: ``<uid> <flow_stat> <mode> <limit_mode> <digits> <decimal>``.
# Primer names the configuration fields explicitly (5), so the uid +
# 5-field decode is canonical. A device that echoes the totalizer id
# would produce 7 fields; hardware-correctable per design §15.3.
_TC_FIELD_COUNT: Final[int] = 6


def _decode_reset_frame(
    command_name: str,
    response: bytes | tuple[bytes, ...],
    ctx: DecodeContext,
) -> ParsedFrame:
    """Shared decode — both ``T <n>`` and ``TP <n>`` reply with a post-op data frame."""
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
# TOTALIZER_CONFIG (``TC``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TotalizerConfigRequest:
    """Arguments for :data:`TOTALIZER_CONFIG`.

    Attributes:
        totalizer: Which totalizer (``1`` or ``2``) to query or
            configure. Required for both query and set — ``TC`` is
            per-totalizer on the wire.
        flow_statistic_code: ``None`` issues the query form. ``1``
            disables the totalizer (subsequent fields are omitted).
            ``-1`` keeps the current statistic. Any other positive
            integer selects the flow statistic to accumulate (primer
            Appendix A).
        mode / limit_mode / digits / decimal_place: Required whenever
            ``flow_statistic_code`` is set to a value other than ``1``
            (the disable sentinel). Ranges:
            ``digits ∈ [7, 10]`` (default 7), ``decimal_place ∈ [0, 9]``.
            Use :attr:`TotalizerMode.KEEP` / :attr:`TotalizerLimitMode.KEEP`
            to retain the current setting for just one field.
    """

    totalizer: TotalizerId
    flow_statistic_code: int | None = None
    mode: TotalizerMode | None = None
    limit_mode: TotalizerLimitMode | None = None
    digits: int | None = None
    decimal_place: int | None = None


@dataclass(frozen=True, slots=True)
class TotalizerConfigCommand(Command[TotalizerConfigRequest, TotalizerConfig]):
    r"""``TC`` — totalizer configuration query/set (V10 10v00+, flow devices).

    Wire shape:

    - Query:   ``<uid><prefix>TC <totalizer>\r``
    - Disable: ``<uid><prefix>TC <totalizer> 1\r``
    - Set:     ``<uid><prefix>TC <totalizer> <flow_stat> <mode> <limit_mode> <digits> <decimal>\r``

    Response: ``<uid> <flow_stat> <mode> <limit_mode> <digits> <decimal>``
    (6 fields — primer's explicit field list). The totalizer id is the
    caller's responsibility to track; the facade re-populates
    :attr:`TotalizerConfig.totalizer` from the request.
    """

    name: str = "totalizer_config"
    token: str = "TC"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _FLOW_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_TC
    firmware_families: frozenset[FirmwareFamily] = _V10_ONLY

    def encode(self, ctx: DecodeContext, request: TotalizerConfigRequest) -> bytes:
        """Emit the TC query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        head = f"{ctx.unit_id}{prefix}{self.token} {int(request.totalizer)}"
        if request.flow_statistic_code is None:
            return (head + "\r").encode("ascii")
        # Disable form per primer: ``TC <tot> 1`` with no further args.
        if request.flow_statistic_code == 1:
            return f"{head} 1\r".encode("ascii")
        # Full set — require all five config fields. Partial sets use
        # the primer's ``-1`` "keep current" sentinel per field, which
        # is encoded the same way as any other integer here.
        if (
            request.mode is None
            or request.limit_mode is None
            or request.digits is None
            or request.decimal_place is None
        ):
            raise AlicatValidationError(
                f"{self.name}: enabling / reconfiguring requires mode, limit_mode, "
                f"digits, and decimal_place (use TotalizerMode.KEEP / "
                f"TotalizerLimitMode.KEEP to preserve the current value of a "
                f"specific field); to disable instead, pass flow_statistic_code=1 "
                f"with the rest left ``None``.",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={
                        "flow_statistic_code": request.flow_statistic_code,
                        "mode": request.mode,
                        "limit_mode": request.limit_mode,
                        "digits": request.digits,
                        "decimal_place": request.decimal_place,
                    },
                ),
            )
        if not (TOTALIZER_TC_DIGITS_MIN <= request.digits <= TOTALIZER_TC_DIGITS_MAX):
            raise AlicatValidationError(
                f"{self.name}: digits must be in "
                f"[{TOTALIZER_TC_DIGITS_MIN}, {TOTALIZER_TC_DIGITS_MAX}], "
                f"got {request.digits}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"digits": request.digits},
                ),
            )
        if not (TOTALIZER_TC_DECIMAL_MIN <= request.decimal_place <= TOTALIZER_TC_DECIMAL_MAX):
            raise AlicatValidationError(
                f"{self.name}: decimal_place must be in "
                f"[{TOTALIZER_TC_DECIMAL_MIN}, {TOTALIZER_TC_DECIMAL_MAX}], "
                f"got {request.decimal_place}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"decimal_place": request.decimal_place},
                ),
            )
        return (
            f"{head} {request.flow_statistic_code} {int(request.mode)} "
            f"{int(request.limit_mode)} {request.digits} {request.decimal_place}\r"
        ).encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> TotalizerConfig:
        """Parse the ``TC`` reply into :class:`TotalizerConfig`.

        Primer-derived shape: ``<uid> <stat> <mode> <limit_mode>
        <digits> <decimal>`` — 6 fields; totalizer id not echoed.
        Hardware validation (2026-04-17) on 10v20 firmware shows the
        totalizer id IS echoed as the second token, producing 7 fields
        (``<uid> <totalizer_id> <stat> <mode> <limit_mode> <digits>
        <decimal>``). Accept both shapes; drop the echoed id when
        present. The facade re-populates :attr:`totalizer` from the
        request either way.
        """
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name)
        if len(fields) == _TC_FIELD_COUNT + 1:
            # Real 10v20: skip the totalizer_id echo at index 1.
            unit_id = fields[0]
            stat_s, mode_s, limit_s, digits_s, decimal_s = fields[2:]
        elif len(fields) == _TC_FIELD_COUNT:
            unit_id, stat_s, mode_s, limit_s, digits_s, decimal_s = fields
        else:
            raise AlicatParseError(
                f"{self.name}: expected {_TC_FIELD_COUNT} or "
                f"{_TC_FIELD_COUNT + 1} fields, got {len(fields)} — {text!r}",
                field_name="totalizer_config",
                expected=f"{_TC_FIELD_COUNT} or {_TC_FIELD_COUNT + 1}",
                actual=len(fields),
                context=ErrorContext(command_name=self.name, raw_response=response),
            )
        return TotalizerConfig(
            unit_id=unit_id,
            totalizer=TotalizerId.FIRST,  # facade replaces from request
            flow_statistic_code=parse_int(stat_s, field=f"{self.name}.flow_statistic_code"),
            mode=TotalizerMode(parse_int(mode_s, field=f"{self.name}.mode")),
            limit_mode=TotalizerLimitMode(
                parse_int(limit_s, field=f"{self.name}.limit_mode"),
            ),
            digits=parse_int(digits_s, field=f"{self.name}.digits"),
            decimal_place=parse_int(decimal_s, field=f"{self.name}.decimal_place"),
        )


TOTALIZER_CONFIG: TotalizerConfigCommand = TotalizerConfigCommand()


# ---------------------------------------------------------------------------
# TOTALIZER_RESET (``T <n>``) — token-collision with TARE_FLOW
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TotalizerResetRequest:
    """Arguments for :data:`TOTALIZER_RESET`.

    Attributes:
        totalizer: Which totalizer to reset. Defaults to
            :attr:`TotalizerId.FIRST` — primer says a bare ``T <n>``
            command without a number defaults to totalizer 1, but the
            encoder always emits an explicit number so the wire shape
            can never collide with ``TARE_FLOW`` (bare ``T``).
        confirm: Required ``True`` — the session's destructive-confirm
            gate raises :class:`AlicatValidationError` when ``False``.
    """

    totalizer: TotalizerId = TotalizerId.FIRST
    confirm: bool = False


@dataclass(frozen=True, slots=True)
class TotalizerReset(Command[TotalizerResetRequest, "ParsedFrame"]):
    r"""``T <n>`` — reset totalizer count (8v00+, flow devices). Destructive.

    Wire: ``<uid><prefix>T <totalizer>\r`` — **always** with the
    numeric argument. Primer's bare ``<uid>T\r`` is the flow-tare
    command (see :data:`TARE_FLOW`); the two share a token and
    collide at the wire level if the numeric arg is omitted.

    Response: post-op data frame with the totalizer reset to zero.
    """

    name: str = "totalizer_reset"
    token: str = "T"  # noqa: S105 — protocol token, shared with TARE_FLOW
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _FLOW_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_RESET
    firmware_families: frozenset[FirmwareFamily] = _V8_V9_V10
    destructive: bool = True

    def encode(self, ctx: DecodeContext, request: TotalizerResetRequest) -> bytes:
        r"""Emit ``<uid><prefix>T <totalizer>\r`` — always with the numeric arg."""
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token} {int(request.totalizer)}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-op data frame."""
        return _decode_reset_frame(self.name, response, ctx)


TOTALIZER_RESET: TotalizerReset = TotalizerReset()


# ---------------------------------------------------------------------------
# TOTALIZER_RESET_PEAK (``TP <n>``) — token-collision with TARE_GAUGE_PRESSURE
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TotalizerResetPeakRequest:
    """Arguments for :data:`TOTALIZER_RESET_PEAK`.

    Same shape as :class:`TotalizerResetRequest`; destructive.
    """

    totalizer: TotalizerId = TotalizerId.FIRST
    confirm: bool = False


@dataclass(frozen=True, slots=True)
class TotalizerResetPeak(Command[TotalizerResetPeakRequest, "ParsedFrame"]):
    r"""``TP <n>`` — reset totalizer peak reading (8v00+). Destructive.

    Wire: ``<uid><prefix>TP <totalizer>\r``. **Always** emits the
    numeric argument so the spec can never accidentally produce the
    :data:`TARE_GAUGE_PRESSURE` wire form (bare ``<uid>TP\r``).
    """

    name: str = "totalizer_reset_peak"

    token: str = "TP"  # noqa: S105
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _FLOW_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_RESET
    firmware_families: frozenset[FirmwareFamily] = _V8_V9_V10
    destructive: bool = True

    def encode(self, ctx: DecodeContext, request: TotalizerResetPeakRequest) -> bytes:
        r"""Emit ``<uid><prefix>TP <totalizer>\r`` — always with the numeric arg."""
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token} {int(request.totalizer)}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the post-op data frame."""
        return _decode_reset_frame(self.name, response, ctx)


TOTALIZER_RESET_PEAK: TotalizerResetPeak = TotalizerResetPeak()


# ---------------------------------------------------------------------------
# TOTALIZER_SAVE (``TCR``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TotalizerSaveRequest:
    """Arguments for :data:`TOTALIZER_SAVE`.

    Attributes:
        enable: ``True`` / ``False`` toggles whether the device
            persists totalizer values across power cycles. ``None``
            issues the query form.
        save: ``True`` persists to EEPROM. The underlying ``TCR``
            command writes a config flag; we surface the standard
            ``save`` attribute so the session's EEPROM-wear monitor
            (design §5.20.7) can track its rate alongside other
            EEPROM-backed sets.
    """

    enable: bool | None = None
    save: bool | None = None


@dataclass(frozen=True, slots=True)
class TotalizerSave(Command[TotalizerSaveRequest, TotalizerSaveState]):
    r"""``TCR`` — save-totalizer query/set (V10 10v05+, flow devices).

    Wire shape:

    - Query: ``<uid><prefix>TCR\r``
    - Set:   ``<uid><prefix>TCR <enable>\r``

    Response: ``<uid> <enable>`` (2 fields).
    """

    name: str = "totalizer_save"
    token: str = "TCR"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _FLOW_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_TCR
    firmware_families: frozenset[FirmwareFamily] = _V10_ONLY

    def encode(self, ctx: DecodeContext, request: TotalizerSaveRequest) -> bytes:
        """Emit TCR query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.enable is None:
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token} {int(request.enable)}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> TotalizerSaveState:
        """Parse ``<uid> <enable>`` into :class:`TotalizerSaveState`."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=2)
        unit_id, enable_s = fields
        return TotalizerSaveState(
            unit_id=unit_id,
            enabled=parse_bool_code(enable_s, field=f"{self.name}.enabled"),
        )


TOTALIZER_SAVE: TotalizerSave = TotalizerSave()
