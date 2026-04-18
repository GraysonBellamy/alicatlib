"""In-process fake transport for tests.

:class:`FakeTransport` implements the :class:`Transport` Protocol without
touching a serial port. Tests script the expected write→response mapping and
assert the recorded command bytes.

Design reference: ``docs/design.md`` §5.1.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import anyio

from alicatlib.errors import (
    AlicatConnectionError,
    AlicatTimeoutError,
    ErrorContext,
)

__all__ = ["FakeTransport", "ScriptedReply"]


#: A scripted reply. Bytes are emitted verbatim; sequences are concatenated in
#: order; callables receive the exact write payload and return bytes or a
#: sequence of bytes (useful for fuzzier scripts).
type ScriptedReply = bytes | Sequence[bytes] | Callable[[bytes], bytes | Sequence[bytes]]


def _normalize_reply(reply: bytes | Sequence[bytes]) -> bytes:
    if isinstance(reply, bytes):
        return reply
    return b"".join(reply)


class FakeTransport:
    """Scripted :class:`Transport` for tests.

    Arguments:
        script: Mapping of ``write_bytes → reply``. Every known write queues
            the corresponding reply into the read buffer. Unknown writes are
            recorded but produce no reply — subsequent reads will then hit
            ``idle_timeout`` / ``timeout``, which is the intended failure
            mode (tests see a real timeout if they forgot to script a
            command).
        label: Identifier used in errors.
        latency_s: Per-operation artificial delay, useful for simulating a
            slow device.
    """

    def __init__(
        self,
        script: Mapping[bytes, ScriptedReply] | None = None,
        *,
        label: str = "fake://test",
        latency_s: float = 0.0,
    ) -> None:
        self._script: dict[bytes, ScriptedReply] = dict(script or {})
        self._writes: list[bytes] = []
        self._read_buffer = bytearray()
        self._is_open = False
        self._label = label
        self._latency_s = latency_s
        self._force_read_timeout = False
        self._force_write_timeout = False
        self._force_disconnected = False
        # Track calls to ``reopen`` so baud-change tests can assert the
        # transport observed the reconfiguration. ``None`` until
        # :meth:`reopen` is called; then holds the last requested
        # baudrate. ``reopen_count`` lets tests distinguish "never
        # called" from "called with the default baud".
        self._last_reopen_baud: int | None = None
        self._reopen_count: int = 0
        self._force_reopen_error: bool = False

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        if self._is_open:
            raise AlicatConnectionError(
                f"{self._label} is already open",
                context=ErrorContext(port=self._label),
            )
        self._is_open = True

    async def close(self) -> None:
        self._is_open = False

    async def reopen(self, *, baudrate: int) -> None:
        """Simulate a baud-rate change — close, record, reopen.

        If ``force_reopen_error()`` has been called the reopen raises
        :class:`AlicatConnectionError` after the close, leaving the
        transport closed. That's the "reopen wedged" path tested at
        the session layer for :attr:`SessionState.BROKEN` transitions.
        """
        await self.close()
        self._reopen_count += 1
        self._last_reopen_baud = baudrate
        if self._force_reopen_error:
            raise AlicatConnectionError(
                f"forced reopen error on {self._label}",
                context=ErrorContext(port=self._label),
            )
        await self.open()

    # ------------------------------------------------------------------ I/O

    async def write(self, data: bytes, *, timeout: float) -> None:
        self._ensure_open()
        if self._force_write_timeout:
            raise AlicatTimeoutError(
                f"write on {self._label} timed out after {timeout}s (forced)",
                context=ErrorContext(port=self._label, extra={"phase": "write"}),
            )
        if self._latency_s:
            await anyio.sleep(self._latency_s)
        payload = bytes(data)
        self._writes.append(payload)
        reply = self._script.get(payload)
        if reply is None:
            return
        if callable(reply):
            produced = reply(payload)
            self._read_buffer.extend(_normalize_reply(produced))
        else:
            self._read_buffer.extend(_normalize_reply(reply))

    async def read_until(self, separator: bytes, timeout: float) -> bytes:
        self._ensure_open()
        if self._force_read_timeout:
            raise AlicatTimeoutError(
                f"read_until on {self._label} timed out after {timeout}s (forced)",
                context=ErrorContext(port=self._label, extra={"phase": "read"}),
            )
        if self._latency_s:
            await anyio.sleep(self._latency_s)
        idx = self._read_buffer.find(separator)
        if idx < 0:
            raise AlicatTimeoutError(
                f"read_until({separator!r}) on {self._label} timed out after {timeout}s",
                context=ErrorContext(port=self._label, extra={"phase": "read"}),
            )
        end = idx + len(separator)
        result = bytes(self._read_buffer[:end])
        del self._read_buffer[:end]
        return result

    async def read_available(
        self,
        idle_timeout: float,
        max_bytes: int | None = None,
    ) -> bytes:
        self._ensure_open()
        if self._latency_s:
            await anyio.sleep(self._latency_s)
        if max_bytes is None or max_bytes >= len(self._read_buffer):
            result = bytes(self._read_buffer)
            self._read_buffer.clear()
        else:
            result = bytes(self._read_buffer[:max_bytes])
            del self._read_buffer[:max_bytes]
        return result

    async def drain_input(self) -> None:
        self._read_buffer.clear()

    # ------------------------------------------------------------------ props

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def label(self) -> str:
        return self._label

    # ------------------------------------------------------------------ test API

    @property
    def writes(self) -> tuple[bytes, ...]:
        """Every write payload recorded since construction, in order."""
        return tuple(self._writes)

    def feed(self, data: bytes) -> None:
        """Push unsolicited bytes into the read buffer.

        Useful for simulating a device that was left streaming, or garbage on
        the line that the session must drain on recovery.
        """
        self._read_buffer.extend(data)

    def add_script(self, command: bytes, reply: ScriptedReply) -> None:
        """Register or overwrite a scripted reply for ``command``."""
        self._script[bytes(command)] = reply

    def force_read_timeout(self, enabled: bool = True) -> None:
        """Force the next :meth:`read_until` to raise ``AlicatTimeoutError``."""
        self._force_read_timeout = enabled

    def force_write_timeout(self, enabled: bool = True) -> None:
        """Force the next :meth:`write` to raise ``AlicatTimeoutError``."""
        self._force_write_timeout = enabled

    def force_reopen_error(self, enabled: bool = True) -> None:
        """Force the next :meth:`reopen` to raise :class:`AlicatConnectionError`.

        Used by :class:`Session.change_baud_rate` tests to exercise
        the BROKEN-state transition without a real serial adapter.
        """
        self._force_reopen_error = enabled

    @property
    def reopen_count(self) -> int:
        """Number of :meth:`reopen` calls since construction."""
        return self._reopen_count

    @property
    def last_reopen_baud(self) -> int | None:
        """Baud rate requested by the most recent :meth:`reopen`, or ``None``."""
        return self._last_reopen_baud

    # ------------------------------------------------------------------ internals

    def _ensure_open(self) -> None:
        if not self._is_open:
            raise AlicatConnectionError(
                f"{self._label} is not open",
                context=ErrorContext(port=self._label),
            )
