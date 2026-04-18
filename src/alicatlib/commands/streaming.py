r"""Streaming-mode command spec + raw wire-byte helpers.

The Alicat streaming-mode surface is narrow: three wire shapes, only one
of which is a normal request/response command.

- ``NCS`` — query or set the streaming interval in milliseconds. Normal
  request/response (``ResponseMode.LINE``); V10 >= 10v05 per primer p. 22.
  Modelled as :data:`STREAMING_RATE` and dispatched through
  :meth:`Session.execute` before or after the streaming context, not
  during it (the session's streaming gate refuses every command while
  streaming is active — see :class:`~alicatlib.devices.streaming.StreamingSession`).
- ``{unit_id}@ @`` — start streaming. Not a ``Command`` instance: enters
  a device mode, does not round-trip a reply. The raw bytes are
  produced by :func:`encode_start_stream` and written directly by
  :class:`~alicatlib.devices.streaming.StreamingSession` under the
  client lock.
- ``@@ {new_unit_id}`` — stop streaming. Same rationale; bytes produced
  by :func:`encode_stop_stream`. Already used by
  :func:`~alicatlib.devices.factory._recover_from_stream` for the
  stale-stream recovery path — ``StreamingSession`` reuses the same
  wire form for symmetry and co-location.

The start/stop helpers are pure functions on strings; keeping them in
the commands layer (next to ``NCS``) means the devices-layer runtime
imports one module for the whole streaming wire surface.

Design reference: ``docs/design.md`` §5.8.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.errors import AlicatValidationError, ErrorContext
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_fields, parse_int

__all__ = [
    "MIN_FIRMWARE_NCS",
    "STREAMING_RATE",
    "StreamingRate",
    "StreamingRateRequest",
    "StreamingRateResult",
    "encode_start_stream",
    "encode_stop_stream",
]


#: Primer p. 22 pins ``NCS`` at V10 >= 10v05; earlier firmware has no
#: knob for the streaming rate (it's a fixed 50 ms cadence). The family
#: gate keeps GP / V1_V7 / V8_V9 out; the min_firmware gate keeps
#: pre-10v05 V10 devices from getting silent rejections.
MIN_FIRMWARE_NCS: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=5,
    raw="10v05",
)


_ALL_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(DeviceKind)


def encode_start_stream(unit_id: str) -> bytes:
    r"""Return the ``{unit_id}@ @\r`` wire bytes (primer p. 10).

    Not a :class:`Command` — starting a stream transitions the device
    into a mode where its unit id becomes ``@`` and it pushes frames
    without prompting. The :class:`~alicatlib.devices.streaming.StreamingSession`
    runtime writes these bytes directly under the client lock, never
    through :meth:`Session.execute`.
    """
    return f"{unit_id}@ @\r".encode("ascii")


def encode_stop_stream(new_unit_id: str) -> bytes:
    r"""Return the ``@@ {new_unit_id}\r`` wire bytes (primer p. 10).

    Same rationale as :func:`encode_start_stream` — this is a mode
    transition, not a request/response command. Already used by
    :func:`~alicatlib.devices.factory._recover_from_stream` for
    unconditional stale-stream recovery at ``open_device`` time;
    :class:`~alicatlib.devices.streaming.StreamingSession` reuses the
    same helper on context exit.
    """
    return f"@@ {new_unit_id}\r".encode("ascii")


# ---------------------------------------------------------------------------
# STREAMING_RATE (``NCS``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamingRateRequest:
    """Arguments for :data:`STREAMING_RATE`.

    Attributes:
        rate_ms: Streaming interval in milliseconds. ``None`` issues the
            query form. A non-negative integer sets the interval — ``0``
            is the primer's "send as fast as the wire allows" setting
            and is distinct from ``None``.
    """

    rate_ms: int | None = None


@dataclass(frozen=True, slots=True)
class StreamingRateResult:
    """Reply payload for :data:`STREAMING_RATE`.

    Attributes:
        unit_id: Echoed unit id from the device.
        rate_ms: Current streaming interval, in milliseconds.
    """

    unit_id: str
    rate_ms: int


@dataclass(frozen=True, slots=True)
class StreamingRate(Command[StreamingRateRequest, StreamingRateResult]):
    r"""``NCS`` — query or set the streaming interval.

    Wire shape (primer p. 22):

    - Query: ``<uid><prefix>NCS\r``
    - Set:   ``<uid><prefix>NCS <interval_ms>\r``

    Response (primer-derived, hardware-correctable): ``<uid> <interval_ms>``.
    The primer says the device "confirms the rate"; a real capture may
    refine the reply shape, which is a one-line regex change to the
    decoder per design §15.3.
    """

    name: str = "streaming_rate"
    token: str = "NCS"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = MIN_FIRMWARE_NCS
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V10},
    )

    def encode(
        self,
        ctx: DecodeContext,
        request: StreamingRateRequest,
    ) -> bytes:
        """Emit the NCS query or set bytes.

        ``rate_ms`` must be a non-negative ``int``. ``0`` is a valid
        device setting (as-fast-as-possible); ``None`` distinguishes
        the query form.
        """
        prefix = ctx.command_prefix.decode("ascii")
        if request.rate_ms is None:
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        # Runtime guards for callers bypassing the type system.
        # ``bool`` is an ``int`` subclass, so the type-annotated
        # ``int | None`` admits ``True`` / ``False`` — but those would
        # silently encode as ``1`` / ``0`` ms, an accidental rate the
        # caller almost certainly didn't mean. Reject explicitly; the
        # four sentinels (``None``, ``0``, ``False``, ``""``) must stay
        # distinct per the encoder rule in design §5.4.
        # ``float`` and other numeric types are rejected to keep the
        # wire format integral — ``50.0`` would format as ``"50.0"``
        # and the device would reject it.
        rate_ms = request.rate_ms
        if isinstance(rate_ms, bool) or type(rate_ms) is not int:
            raise AlicatValidationError(
                f"{self.name}: rate_ms must be int, got {type(rate_ms).__name__}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"rate_ms": rate_ms},
                ),
            )
        if rate_ms < 0:
            raise AlicatValidationError(
                f"{self.name}: rate_ms must be >= 0, got {rate_ms}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"rate_ms": rate_ms},
                ),
            )
        return f"{ctx.unit_id}{prefix}{self.token} {rate_ms}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> StreamingRateResult:
        """Parse ``<uid> <interval_ms>`` into :class:`StreamingRateResult`.

        Two-field reply per primer p. 22 — the device echoes its unit id
        and the effective interval. Hardware-correctable per design §15.3;
        any observed extra field surfaces as a parse error pointing at
        the raw bytes.
        """
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=2)
        unit_id, rate_s = fields
        return StreamingRateResult(
            unit_id=unit_id,
            rate_ms=parse_int(rate_s, field="rate_ms"),
        )


STREAMING_RATE: StreamingRate = StreamingRate()
