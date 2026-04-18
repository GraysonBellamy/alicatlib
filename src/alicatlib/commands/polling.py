r"""Polling commands — primer ``A\r`` poll and ``DV`` request-data query.

:data:`POLL_DATA` is the workhorse: send the unit ID + EOL, get one data
frame back, parse it against the :class:`DataFrameFormat` the session
cached at startup. The command returns a pure :class:`ParsedFrame` — the
session wraps it with ``received_at`` / ``monotonic_ns`` into a
:class:`DataFrame` at the read site (design §5.6).

:data:`REQUEST_DATA` (``DV``) is the targeted sibling of ``POLL_DATA``:
instead of returning every cached field, it reports a caller-chosen list
of 1–13 statistics averaged over an explicit window. Wire shape
(captured 2026-04-17, ``tests/fixtures/responses/request_data_dv.txt``)::

    request: ``<uid>DV <time_ms> <stat1> [stat2...]\r``
    reply:   ``<val1> [val2...]\r``   (NO unit-id prefix — unique in the
                                       catalog)

The reply carries no statistic identifiers, so the decoder returns a
pure ``tuple[float | None, ...]`` in the order requested; the facade
:meth:`alicatlib.devices.base.Device.request` zips that with the request's
statistic list and adds ``received_at`` to produce a
:class:`~alicatlib.devices.models.MeasurementSet`. The ``--`` sentinel
(invalid statistic code) maps to ``None``.

Design reference: ``docs/design.md`` §5.4, §5.6, §5.9, §5.20 (item 4 —
averaging range), §15.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.errors import AlicatParseError, AlicatValidationError, ErrorContext
from alicatlib.firmware import FirmwareFamily
from alicatlib.protocol.parser import parse_fields, parse_optional_float
from alicatlib.registry import Statistic, statistic_registry
from alicatlib.registry._codes_gen import STATISTIC_BY_CODE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from alicatlib.devices.data_frame import ParsedFrame

__all__ = [
    "POLL_DATA",
    "REQUEST_DATA",
    "PollData",
    "PollRequest",
    "RequestData",
    "RequestDataRequest",
]


#: Primer-stated max averaging window; zero is documented but the device
#: rejects it on the wire (fixture ``request_data_dv.txt``), so we reject
#: pre-I/O with a clearer message than the generic ``?``.
_MIN_AVERAGING_MS: Final[int] = 1
_MAX_AVERAGING_MS: Final[int] = 9999

#: Primer §DV: up to 13 statistics per call.
_MIN_DV_STATISTICS: Final[int] = 1
_MAX_DV_STATISTICS: Final[int] = 13

#: Wire sentinel for "statistic code unknown to this device" in a DV
#: reply. Distinct from the ``?`` command-level rejection: DV returns
#: values for every requested slot, filling a run of dashes where the
#: device can't produce a reading. Primer documents ``--``; hardware
#: validation (2026-04-17) on MW-10SLPM-D @ 10v04 showed a 7-dash sentinel
#: (``-------``) when a controller-only statistic like
#: MASS_FLOW_SETPT is requested from a meter. The width appears to
#: match the corresponding data-frame column width, so the parser
#: treats any pure-dash token (``^-+$``) as the absent sentinel.
_ABSENT_TOKEN: Final[str] = "--"  # noqa: S105 — Alicat wire sentinel, not a credential


_MIN_ABSENT_DASHES: Final[int] = 2


def _is_absent(token: str) -> bool:
    """True when ``token`` is a run of one or more dashes."""
    return len(token) >= _MIN_ABSENT_DASHES and set(token) == {"-"}


def _resolve_statistic_code(value: Statistic | str) -> tuple[Statistic, int]:
    """Coerce ``value`` to its canonical :class:`Statistic` and numeric code.

    Mirrors :func:`alicatlib.commands.units._resolve_statistic_code`; kept
    local so the polling module stays free of a circular cross-command
    dependency.
    """
    stat = statistic_registry.coerce(value)
    for code, member in STATISTIC_BY_CODE.items():
        if member is stat:
            return stat, code
    raise AlicatValidationError(
        f"{stat!r} has no numeric code — codes.json / _codes_gen.py drift?",
    )


@dataclass(frozen=True, slots=True)
class PollRequest:
    """Arguments for :data:`POLL_DATA` — no user-provided fields."""


@dataclass(frozen=True, slots=True)
class PollData(Command[PollRequest, "ParsedFrame"]):
    r"""Poll the device's current data frame (primer ``A\r``).

    Encodes as just ``{unit_id}{prefix}\r`` — no token. Decodes against
    the session-cached :class:`DataFrameFormat` carried on the
    :class:`DecodeContext`.

    The decode layer returns a :class:`ParsedFrame` rather than a
    :class:`DataFrame` because timing belongs to the I/O layer, not the
    pure decode step. The session's ``execute()`` wraps via
    :meth:`DataFrame.from_parsed` before returning from the facade
    (``Device.poll()``), so users never see a raw :class:`ParsedFrame`
    unless they go through the ``session.execute(POLL_DATA, ...)``
    escape hatch. See design §5.6.
    """

    name: str = "poll_data"
    # No command token — the protocol uses an empty-token poll. The field
    # is preserved for uniformity with the rest of the catalog; tests that
    # assert emitted bytes use the full encode() output, not just token.
    token: str = ""
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = frozenset(DeviceKind)
    # Poll is read-only. GP firmware's ``$$`` applies only to writes
    # (design §16.6.8 — a GP07R100 accepts ``A\r`` / ``A??M*\r`` but
    # rejects ``A$$\r`` / ``A$$??M*\r``).
    prefix_less: bool = True

    def encode(
        self,
        ctx: DecodeContext,
        request: PollRequest,
    ) -> bytes:
        r"""Emit the device's poll bytes — ``<unit_id><prefix>\r``, no token."""
        del request
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> ParsedFrame:
        """Parse the raw data frame against ``ctx.data_frame_format``."""
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        if ctx.data_frame_format is None:
            raise AlicatParseError(
                "poll_data requires ctx.data_frame_format; session must probe ??D* first",
                field_name="data_frame_format",
                expected="DataFrameFormat",
                actual=None,
                context=ErrorContext(command_name=self.name, raw_response=response),
            )
        return ctx.data_frame_format.parse(response)


POLL_DATA: PollData = PollData()


# ---------------------------------------------------------------------------
# REQUEST_DATA (``DV``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestDataRequest:
    """Arguments for :data:`REQUEST_DATA`.

    Attributes:
        statistics: 1–13 :class:`Statistic` members (or alias strings /
            canonical values). Order is preserved on the wire and in the
            returned values tuple.
        averaging_ms: Rolling averaging window in milliseconds (primer:
            1–9999). The device rejects ``0`` on the wire, so the encoder
            rejects it pre-I/O with :class:`AlicatValidationError`.
    """

    statistics: Sequence[Statistic | str]
    averaging_ms: int = 1


@dataclass(frozen=True, slots=True)
class RequestData(Command[RequestDataRequest, tuple[float | None, ...]]):
    r"""``DV`` — request a caller-chosen subset of statistics.

    Decoder returns a positional tuple of parsed values aligned with
    :attr:`RequestDataRequest.statistics`. The ``--`` wire sentinel (an
    invalid statistic code passed through per-slot) maps to ``None``.

    The wire reply has **no** unit-id prefix — unique in the catalog and
    load-bearing for the decoder. The facade
    :meth:`alicatlib.devices.base.Device.request` wraps the tuple into a
    :class:`~alicatlib.devices.models.MeasurementSet` with the correct
    ``unit_id`` / ``averaging_ms`` / ``received_at`` mapping (same
    pure-parse → timing-wrap split as :data:`POLL_DATA`).
    """

    name: str = "request_data"
    token: str = "DV"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = frozenset(DeviceKind)
    # Captured 2026-04-17 on V10 10v20 (MC-5SLPM-D). DV is a legacy Alicat
    # query so we extend the firmware gate to the numeric families per the
    # same monotonic-gate rule FPF uses (design §5.4): include families
    # where *some* devices work; runtime rejection handles per-device gaps.
    # GP omitted until a GP capture confirms it.
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(
        self,
        ctx: DecodeContext,
        request: RequestDataRequest,
    ) -> bytes:
        r"""Emit ``<unit_id><prefix>DV <time_ms> <stat1> [stat2...]\r``.

        Validates pre-I/O per design §5.20 item 4: averaging in
        ``1..9999`` ms, 1..13 statistics.
        """
        if not (_MIN_AVERAGING_MS <= request.averaging_ms <= _MAX_AVERAGING_MS):
            raise AlicatValidationError(
                f"{self.name}: averaging_ms must be in "
                f"[{_MIN_AVERAGING_MS}, {_MAX_AVERAGING_MS}], "
                f"got {request.averaging_ms}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"averaging_ms": request.averaging_ms},
                ),
            )
        count = len(request.statistics)
        if not (_MIN_DV_STATISTICS <= count <= _MAX_DV_STATISTICS):
            raise AlicatValidationError(
                f"{self.name}: statistics count must be in "
                f"[{_MIN_DV_STATISTICS}, {_MAX_DV_STATISTICS}], got {count}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"statistics_count": count},
                ),
            )
        codes = [_resolve_statistic_code(s)[1] for s in request.statistics]
        prefix = ctx.command_prefix.decode("ascii")
        stats_part = " ".join(str(c) for c in codes)
        return f"{ctx.unit_id}{prefix}{self.token} {request.averaging_ms} {stats_part}\r".encode(
            "ascii",
        )

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> tuple[float | None, ...]:
        """Parse ``<val1> [val2...]`` into a positional tuple.

        ``--`` slots resolve to ``None``; every other token parses as a
        float. Unit-id prefix absence is the load-bearing wire property
        — if a future firmware adds one, this decoder needs an update
        (pin via a fresh fixture).
        """
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name)
        if not fields:
            raise AlicatParseError(
                f"{self.name}: empty reply",
                field_name="values",
                expected="at least one value",
                actual=text,
                context=ErrorContext(command_name=self.name, raw_response=response),
            )
        return tuple(
            None if _is_absent(value) else parse_optional_float(value, field=f"value[{i}]")
            for i, value in enumerate(fields)
        )


REQUEST_DATA: RequestData = RequestData()
