"""One-in-flight request/response client over a :class:`Transport`.

:class:`AlicatProtocolClient` is the narrow waist between the command layer
(which knows Alicat semantics) and the transport layer (which knows bytes).
It enforces:

- Exactly one command in flight per client, via :class:`anyio.Lock`.
- Every write bounded by an explicit ``write_timeout`` — read vs write
  timeouts are tagged distinctly in :class:`ErrorContext` so observability
  can tell a jammed bus from a non-responsive device.
- Multiline termination priority: ``is_complete(lines)`` → ``max_lines`` →
  idle-timeout fallback. The fallback is the slow path; a metric counts how
  often each command falls through to it so we can find commands missing
  their termination contract.

Design reference: ``docs/design.md`` §5.2.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Final

import anyio

from alicatlib._logging import get_logger
from alicatlib.errors import (
    AlicatCommandRejectedError,
    AlicatProtocolError,
    AlicatTimeoutError,
    ErrorContext,
)
from alicatlib.protocol.framing import EOL, strip_eol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from alicatlib.transport.base import Transport

__all__ = ["AlicatProtocolClient"]


_logger = get_logger("protocol")


# The device's error marker is a bare ``?`` — either alone or preceded by the
# unit id (``A ?``). The primer only specifies the bare form; we also accept
# the prefixed form because that's what real devices emit.
_REJECTION_RE: Final[re.Pattern[bytes]] = re.compile(rb"^\s*([A-Za-z]\s+)?\?\s*$")


class AlicatProtocolClient:
    """Request/response client that serialises commands over a transport.

    Each method acquires an internal :class:`anyio.Lock` for its duration, so
    callers from different tasks may invoke methods concurrently — the lock
    queues them.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        eol: bytes = EOL,
        default_timeout: float = 0.5,
        multiline_timeout: float = 1.0,
        multiline_idle_timeout: float = 0.1,
        write_timeout: float = 0.5,
        drain_before_write: bool = False,
    ) -> None:
        self._transport = transport
        self._eol = eol
        self._default_timeout = default_timeout
        self._multiline_timeout = multiline_timeout
        self._multiline_idle_timeout = multiline_idle_timeout
        self._write_timeout = write_timeout
        self._drain_before_write = drain_before_write
        self._lock = anyio.Lock()
        self._idle_timeout_exits = 0
        # Streaming-mode latch. Set to True by
        # :class:`~alicatlib.devices.streaming.StreamingSession` on
        # entry; every :class:`~alicatlib.devices.session.Session`
        # sharing this client refuses to dispatch commands while True
        # (design §5.8). One streamer per port is the hard invariant;
        # the latch is the mechanism that enforces it across sessions
        # that all share this client.
        self._streaming = False

    # ------------------------------------------------------------------ metrics

    @property
    def idle_timeout_exits(self) -> int:
        """Number of :meth:`query_lines` calls that exited via idle-timeout.

        Rises when commands don't declare ``is_complete`` / ``max_lines`` —
        treat a growing counter for a specific command as a bug report against
        that command's spec (see design §5.2, §5.4).
        """
        return self._idle_timeout_exits

    def reset_idle_timeout_metric(self) -> None:
        """Reset :attr:`idle_timeout_exits` to zero. Primarily for tests."""
        self._idle_timeout_exits = 0

    # ------------------------------------------------------------------ session hooks

    @property
    def lock(self) -> anyio.Lock:
        """Port-level command lock, shared across every :class:`Session`.

        Normal command dispatch goes through :meth:`query_line` /
        :meth:`query_lines` / :meth:`write_only`, which acquire this
        lock internally. Lifecycle-changing operations on
        :class:`Session` (``change_unit_id``, ``change_baud_rate``) need
        to hold the lock for a *multi-step* sequence — they borrow it
        directly so the device and client stay in sync across the
        write → verify → reconfigure boundary. See design §5.7.
        """
        return self._lock

    @property
    def transport(self) -> Transport:
        """Underlying :class:`Transport`.

        Exposed for lifecycle operations that need direct byte-level
        access under the shared lock (``change_baud_rate`` needs
        :meth:`Transport.reopen`). Normal command dispatch should
        stay on the public ``query_*`` / ``write_only`` API.
        """
        return self._transport

    @property
    def eol(self) -> bytes:
        """The EOL terminator this client expects on read boundaries."""
        return self._eol

    @property
    def is_streaming(self) -> bool:
        """``True`` while a :class:`StreamingSession` owns this client.

        Set by :class:`~alicatlib.devices.streaming.StreamingSession` on
        entry and cleared on exit. The
        :class:`~alicatlib.devices.session.Session` dispatch path
        consults this and fails fast with
        :class:`~alicatlib.errors.AlicatStreamingModeError` rather than
        writing a command onto a bus the device is already flooding
        with unsolicited frames (design §5.8).
        """
        return self._streaming

    def _mark_streaming(self, streaming: bool) -> None:
        """Set the streaming latch. Intended for :class:`StreamingSession` only.

        Package-private (the underscore is load-bearing): user code
        flips streaming by entering a :class:`StreamingSession`, not by
        poking this setter. Kept distinct from a public ``@property``
        setter so the mutation verb stays visible at call sites
        (``client._mark_streaming(True)``).
        """
        self._streaming = streaming

    def guard_response(self, response: bytes, *, command: bytes) -> None:
        """Public alias for :meth:`_guard_response`.

        Session lifecycle paths that bypass :meth:`query_line` still
        need the ``?``-rejection / empty-response guards; exposing the
        check lets them get the same error shape without duplicating
        the regex.
        """
        self._guard_response(response, command=command)

    # ------------------------------------------------------------------ public API

    async def query_line(
        self,
        command: bytes,
        *,
        timeout: float | None = None,
        write_timeout: float | None = None,
    ) -> bytes:
        """Send a single-line command and return the single-line response.

        The returned bytes have the EOL already stripped. A bare ``?`` /
        unit-id-prefixed ``?`` surfaces as :class:`AlicatCommandRejectedError`;
        an empty response surfaces as :class:`AlicatProtocolError`.
        """
        read_to = timeout if timeout is not None else self._default_timeout
        write_to = write_timeout if write_timeout is not None else self._write_timeout
        async with self._lock:
            await self._prepare_for_write()
            self._trace_tx(command)
            await self._transport.write(command, timeout=write_to)
            raw = await self._transport.read_until(self._eol, timeout=read_to)
            self._trace_rx(raw)
            stripped = strip_eol(raw, eol=self._eol)
            try:
                self._guard_response(stripped, command=command)
            except (AlicatCommandRejectedError, AlicatProtocolError):
                # Some firmware emits a two-part reply on rejection (a bare
                # `\r` then `?\r`, observed on 6v21 FPF-on-absent-statistic).
                # Sleep briefly so trailing bytes land, then drain so the
                # next command starts clean.
                await anyio.sleep(0.02)
                await self._transport.drain_input()
                raise
            return stripped

    async def query_lines(
        self,
        command: bytes,
        *,
        first_timeout: float | None = None,
        idle_timeout: float | None = None,
        max_lines: int | None = None,
        is_complete: Callable[[Sequence[bytes]], bool] | None = None,
        write_timeout: float | None = None,
    ) -> tuple[bytes, ...]:
        """Send a multiline command and collect lines until termination.

        Termination priority (design §5.2):

        1. ``is_complete(lines)`` returns ``True`` — caller-supplied predicate
           for tables with a computable end condition.
        2. ``len(lines) >= max_lines`` — hard cap, useful for fixed-shape
           tables like ``??M*`` (10 lines).
        3. ``idle_timeout`` expires — fallback for unknown-length responses.
           Increments :attr:`idle_timeout_exits`; the slow path.

        Returned bytes have EOL stripped.
        """
        first_to = first_timeout if first_timeout is not None else self._multiline_timeout
        idle_to = idle_timeout if idle_timeout is not None else self._multiline_idle_timeout
        write_to = write_timeout if write_timeout is not None else self._write_timeout
        async with self._lock:
            await self._prepare_for_write()
            self._trace_tx(command)
            await self._transport.write(command, timeout=write_to)
            raw_first = await self._transport.read_until(self._eol, timeout=first_to)
            self._trace_rx(raw_first)
            first = strip_eol(raw_first, eol=self._eol)
            try:
                self._guard_response(first, command=command)
            except (AlicatCommandRejectedError, AlicatProtocolError):
                # Drain residual bytes so the next command starts clean —
                # cf. the same handling in ``query_line``.
                await anyio.sleep(0.02)
                await self._transport.drain_input()
                raise
            lines: list[bytes] = [first]
            while True:
                if is_complete is not None and is_complete(lines):
                    break
                if max_lines is not None and len(lines) >= max_lines:
                    break
                try:
                    raw_next = await self._transport.read_until(self._eol, timeout=idle_to)
                except AlicatTimeoutError:
                    # Fall-through: no more lines arrived within idle_to.
                    # Don't treat as error — it's a legitimate termination.
                    self._idle_timeout_exits += 1
                    break
                self._trace_rx(raw_next)
                line = strip_eol(raw_next, eol=self._eol)
                lines.append(line)
            return tuple(lines)

    async def write_only(
        self,
        command: bytes,
        *,
        timeout: float | None = None,
    ) -> None:
        """Send a command with no expected reply (e.g. ``@@ stop-stream``)."""
        write_to = timeout if timeout is not None else self._write_timeout
        async with self._lock:
            await self._prepare_for_write()
            self._trace_tx(command)
            await self._transport.write(command, timeout=write_to)

    # ------------------------------------------------------------------ internals

    async def _prepare_for_write(self) -> None:
        if self._drain_before_write:
            await self._transport.drain_input()

    def _trace_tx(self, command: bytes) -> None:
        """Emit a DEBUG wire-trace for an outbound byte string.

        Guarded by :meth:`~logging.Logger.isEnabledFor` so the
        ``repr()`` is only formatted when a handler actually wants
        DEBUG. Structured extras carry the raw bytes and the byte
        count; users who configure a JSON formatter get a
        machine-readable trace, and users who stay on the default
        format see the bytes via ``%(message)s`` of
        ``repr(command)``.
        """
        if not _logger.isEnabledFor(logging.DEBUG):
            return
        _logger.debug(
            "protocol.wire.tx %r",
            command,
            extra={"direction": "tx", "raw": command, "len": len(command)},
        )

    def _trace_rx(self, raw: bytes) -> None:
        """Emit a DEBUG wire-trace for an inbound (pre-strip) line."""
        if not _logger.isEnabledFor(logging.DEBUG):
            return
        _logger.debug(
            "protocol.wire.rx %r",
            raw,
            extra={"direction": "rx", "raw": raw, "len": len(raw)},
        )

    def _guard_response(self, response: bytes, *, command: bytes) -> None:
        if not response:
            raise AlicatProtocolError(
                "empty response from device",
                context=ErrorContext(command_bytes=command, raw_response=response),
            )
        if _REJECTION_RE.match(response):
            raise AlicatCommandRejectedError(
                f"device rejected command: {response!r}",
                context=ErrorContext(command_bytes=command, raw_response=response),
            )
