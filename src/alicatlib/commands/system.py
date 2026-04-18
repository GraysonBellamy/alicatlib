"""System / identification commands — primer §VE, §??M*, §??D*.

These three commands feed the identification pipeline (design §5.9):

- :data:`VE_QUERY` (``VE``) — firmware version; works on *every* firmware
  family and is the anchor of identification.
- :data:`MANUFACTURING_INFO` (``??M*``) — 10-line manufacturing-info
  table; only available on numeric-family firmware ≥ 8v28. GP / pre-8v28
  devices fall back to caller-supplied ``model_hint``.
- :data:`DATA_FRAME_FORMAT_QUERY` (``??D*``) — per-device data-frame
  layout; the session caches this at startup and exposes it on
  :attr:`alicatlib.commands.base.DecodeContext.data_frame_format`.

Design reference: ``docs/design.md`` §5.4, §5.9, §5.11.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import ManufacturingInfo
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import (
    parse_data_frame_table,
    parse_manufacturing_info,
    parse_ve_response,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from alicatlib.devices.data_frame import DataFrameFormat

__all__ = [
    "DATA_FRAME_FORMAT_QUERY",
    "MANUFACTURING_INFO",
    "MIN_FIRMWARE_MANUFACTURING_INFO",
    "VE_QUERY",
    "DataFrameFormatQuery",
    "DataFrameFormatRequest",
    "ManufacturingInfoCommand",
    "ManufacturingInfoRequest",
    "VeCommand",
    "VeRequest",
    "VeResult",
]

# Minimum token count on a "<uid> D<NN> <count>" header line. Leading unit
# id + code + numeric count = 3 tokens; anything shorter is not a header.
_DF_HEADER_MIN_TOKENS = 3


# The Alicat Serial Primer's Quick Command Reference lists ``??M*`` as a
# 8v28+ command, but hardware validation on 2026-04-17 caught a real 8v17.0-R23
# device responding to it (and a V10 10v20.0-R24 with the same dialect —
# see design §16.6). Either the primer's annotation is too conservative or
# the 8v17.0-R23 has the command back-ported via the R23 revision.
#
# The session-level firmware gate is therefore *removed* (set to ``None``
# below) and the reachability decision lives in
# :func:`alicatlib.devices.factory._manufacturing_info_reachable`, which
# uses family-by-family dispatch and falls back to ``model_hint``
# gracefully on any device that responds with ``?`` (rejection), empty
# bytes, or a parse error.
#
# The constant is preserved for backwards-compatibility with code that
# imports it as a documentation reference.
MIN_FIRMWARE_MANUFACTURING_INFO: FirmwareVersion = FirmwareVersion(
    family=FirmwareFamily.V8_V9,
    major=8,
    minor=28,
    raw="8v28",
)


# Commands that either apply to every device kind or have no device-kind
# concept (discovery / identification commands fall in the latter bucket).
_ALL_DEVICE_KINDS: frozenset[DeviceKind] = frozenset(DeviceKind)


# ---------------------------------------------------------------------------
# VE
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VeRequest:
    """Arguments for :data:`VE_QUERY` — no user-provided fields."""


@dataclass(frozen=True, slots=True)
class VeResult:
    """Typed response for :data:`VE_QUERY`.

    :func:`alicatlib.protocol.parser.parse_ve_response` is intentionally
    tolerant (the ``VE`` response shape varies across firmware families),
    so we surface both the parsed :class:`FirmwareVersion` and the
    optional firmware date to let callers use whichever they need without
    having to re-parse the raw bytes.
    """

    unit_id: str
    firmware: FirmwareVersion
    firmware_date: date | None


@dataclass(frozen=True, slots=True)
class VeCommand(Command[VeRequest, VeResult]):
    """Firmware-version query. Works on every family; anchor of identification."""

    name: str = "ve_query"
    token: str = "VE"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS

    def encode(self, ctx: DecodeContext, request: VeRequest) -> bytes:
        r"""Emit ``<unit_id><prefix>VE\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> VeResult:
        """Parse the firmware version (and optional date) out of a ``VE`` reply."""
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        # Unit ID is the first whitespace-delimited token; parse_ve_response
        # scans the whole line for firmware + optional date.
        first_token = response.split(None, 1)[0].decode("ascii", errors="replace")
        firmware, firmware_date = parse_ve_response(response)
        return VeResult(
            unit_id=first_token,
            firmware=firmware,
            firmware_date=firmware_date,
        )


VE_QUERY: VeCommand = VeCommand()


# ---------------------------------------------------------------------------
# ??M*
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManufacturingInfoRequest:
    """Arguments for :data:`MANUFACTURING_INFO` — no user-provided fields."""


@dataclass(frozen=True, slots=True)
class ManufacturingInfoCommand(Command[ManufacturingInfoRequest, ManufacturingInfo]):
    """``??M*`` — 10-line manufacturing-info table (8v28+, numeric families).

    The session gates this command on firmware family and version before
    dispatching, so the encoder never reaches a GP or pre-8v28 device.
    """

    name: str = "manufacturing_info"
    token: str = "??M*"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINES
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    # No session-level firmware floor; the factory's
    # `_manufacturing_info_reachable` plus a try-and-recover wrap make the
    # call's reachability decision per family. See design §16.6 / §16.6.2
    # for why the primer's 8v28 minimum was relaxed (and why V1_V7 is
    # included — the 5v12 capture showed ??M* works there too). GP is
    # included because a GP07R100 capture on 2026-04-17 showed the
    # command works prefix-less on GP (design §16.6.8) — the parser
    # then dispatches to the GP dialect based on the reply shape.
    min_firmware: FirmwareVersion | None = None
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.GP, FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )
    expected_lines: int | None = 10
    # GP reads go prefix-less (design §16.6.8); non-GP prefix is already
    # empty, so this is a no-op there.
    prefix_less: bool = True

    def encode(
        self,
        ctx: DecodeContext,
        request: ManufacturingInfoRequest,
    ) -> bytes:
        r"""Emit ``<unit_id><prefix>??M*\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ManufacturingInfo:
        """Parse the 10-line ``??M*`` table into :class:`ManufacturingInfo`."""
        del ctx
        if not isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected multi-line response, got single line",
            )
        return parse_manufacturing_info(response)


MANUFACTURING_INFO: ManufacturingInfoCommand = ManufacturingInfoCommand()


# ---------------------------------------------------------------------------
# ??D*
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataFrameFormatRequest:
    """Arguments for :data:`DATA_FRAME_FORMAT_QUERY` — no user-provided fields."""


def _df_format_is_complete(lines: Sequence[bytes]) -> bool:
    """Terminate ``??D*`` when the leading count-header has been satisfied.

    A common Alicat ``??D*`` shape is a count-header first line followed
    by one line per field: e.g. ``A D01 10`` declaring ten field rows
    come next. When that header is present we can terminate exactly; when
    it isn't we fall through to the protocol client's idle-timeout,
    which is correct but slower. Hardware-captured fixtures will tell us
    whether we can tighten this (design §5.6 TODO).
    """
    if not lines:
        return False
    tokens = lines[0].decode("ascii", errors="replace").split()
    if len(tokens) < _DF_HEADER_MIN_TOKENS or not tokens[2].isdigit():
        return False
    declared = int(tokens[2])
    # Header + declared field lines.
    return len(lines) >= declared + 1


@dataclass(frozen=True, slots=True)
class DataFrameFormatQuery(Command[DataFrameFormatRequest, "DataFrameFormat"]):
    """``??D*`` — query the device's advertised data-frame layout."""

    name: str = "data_frame_format_query"
    token: str = "??D*"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINES
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    # Safety cap: no real data frame carries more than a few dozen fields.
    # The count-header path above terminates earlier when it applies.
    expected_lines: int | None = 64
    # GP reads go prefix-less (design §16.6.8).
    prefix_less: bool = True

    def encode(
        self,
        ctx: DecodeContext,
        request: DataFrameFormatRequest,
    ) -> bytes:
        r"""Emit ``<unit_id><prefix>??D*\r``."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> DataFrameFormat:
        """Parse the advertised data-frame layout into :class:`DataFrameFormat`."""
        del ctx
        if not isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected multi-line response, got single line",
            )
        return parse_data_frame_table(response)


DATA_FRAME_FORMAT_QUERY: DataFrameFormatQuery = DataFrameFormatQuery(
    is_complete=_df_format_is_complete,
)
