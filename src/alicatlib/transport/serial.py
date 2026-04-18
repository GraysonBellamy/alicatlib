"""Serial-port transport backed by :mod:`anyserial`.

:class:`SerialTransport` wraps :class:`anyserial.SerialPort`. Every I/O call
is bounded by :func:`anyio.fail_after` (reads, writes) or
:func:`anyio.move_on_after` (idle-timeout reads). Backend exceptions
normalize to :mod:`alicatlib.errors` types with ``__cause__`` preserved.

Design reference: ``docs/design.md`` §5.1.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from typing import TYPE_CHECKING

import anyio
from anyserial import (
    FlowControl,
    PortBusyError,
    PortNotFoundError,
    SerialClosedError,
    SerialConfig,
    SerialDisconnectedError,
    SerialError,
    open_serial_port,
)

from alicatlib.errors import (
    AlicatConnectionError,
    AlicatTimeoutError,
    AlicatTransportError,
    ErrorContext,
)

if TYPE_CHECKING:
    from anyserial import SerialPort

    from alicatlib.transport.base import SerialSettings

__all__ = ["SerialTransport"]

# Per-call read chunk. Bigger is fine — anyserial returns whatever the kernel
# has ready and never blocks waiting to fill the buffer.
_RECEIVE_CHUNK: int = 4096


class SerialTransport:
    """:class:`Transport` backed by a real serial port via ``anyserial``.

    Tests that don't need hardware can use
    :class:`alicatlib.transport.fake.FakeTransport` instead; the two conform
    to the same structural :class:`Transport` protocol.
    """

    def __init__(self, settings: SerialSettings) -> None:
        self._settings = settings
        self._port: SerialPort | None = None
        # Bytes read past a separator in :meth:`read_until` are held here so
        # the next call sees them first. Serial I/O is chunk-oriented — we
        # can't hand the kernel "give me up to separator" without this.
        self._pushback = bytearray()

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        if self._port is not None:
            raise AlicatConnectionError(
                f"{self.label} is already open",
                context=ErrorContext(port=self.label),
            )
        config = SerialConfig(
            baudrate=self._settings.baudrate,
            byte_size=self._settings.bytesize,
            parity=self._settings.parity,
            stop_bits=self._settings.stopbits,
            flow_control=FlowControl(
                xon_xoff=self._settings.xonxoff,
                rts_cts=self._settings.rtscts,
            ),
            exclusive=self._settings.exclusive,
        )
        try:
            self._port = await open_serial_port(self._settings.port, config)
        except (PortBusyError, PortNotFoundError, SerialDisconnectedError) as exc:
            raise AlicatConnectionError(
                f"could not open {self.label}: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc
        except SerialError as exc:
            raise AlicatTransportError(
                f"backend error opening {self.label}: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc

    async def close(self) -> None:
        port = self._port
        if port is None:
            return
        self._port = None
        self._pushback.clear()
        # Close is best-effort; we've already detached the port reference.
        with contextlib.suppress(SerialError):
            await port.aclose()

    async def reopen(self, *, baudrate: int) -> None:
        """Close and reopen the port at ``baudrate``.

        Called by :meth:`Session.change_baud_rate` after the device
        has already switched — the transport has to retune to stay in
        sync. The cached :class:`SerialSettings` is updated so the
        new baud survives subsequent lifecycle calls (close +
        future open round-trip at the same rate).

        If :meth:`open` fails on the new baud the transport is left
        closed; the caller (the session's baud-change shield) is
        responsible for surfacing that as a ``BROKEN`` session state
        with recovery guidance.
        """
        await self.close()
        # dataclasses.replace on a frozen dataclass — cheap and type-safe.
        self._settings = replace(self._settings, baudrate=baudrate)
        await self.open()

    # ------------------------------------------------------------------ I/O

    async def write(self, data: bytes, *, timeout: float) -> None:
        port = self._require_port()
        try:
            with anyio.fail_after(timeout):
                await port.send(data)
        except TimeoutError as exc:
            raise AlicatTimeoutError(
                f"write on {self.label} timed out after {timeout}s",
                context=ErrorContext(port=self.label, extra={"phase": "write"}),
            ) from exc
        except (SerialClosedError, SerialDisconnectedError) as exc:
            raise AlicatConnectionError(
                f"write on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label, extra={"phase": "write"}),
            ) from exc
        except SerialError as exc:
            raise AlicatTransportError(
                f"write on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label, extra={"phase": "write"}),
            ) from exc

    async def read_until(self, separator: bytes, timeout: float) -> bytes:
        port = self._require_port()
        buf = bytearray(self._pushback)
        self._pushback.clear()
        try:
            with anyio.fail_after(timeout):
                while separator not in buf:
                    chunk = await port.receive(_RECEIVE_CHUNK)
                    if not chunk:
                        # anyserial treats EOF as an exception; this branch is
                        # a belt-and-braces guard.
                        continue
                    buf.extend(chunk)
        except TimeoutError as exc:
            # Preserve whatever we did read — the next call may pick up where
            # this one left off once the device sends the rest.
            self._pushback.extend(buf)
            raise AlicatTimeoutError(
                f"read_until({separator!r}) on {self.label} timed out after {timeout}s",
                context=ErrorContext(port=self.label, extra={"phase": "read"}),
            ) from exc
        except (SerialClosedError, SerialDisconnectedError) as exc:
            raise AlicatConnectionError(
                f"read on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label, extra={"phase": "read"}),
            ) from exc
        except SerialError as exc:
            raise AlicatTransportError(
                f"read on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label, extra={"phase": "read"}),
            ) from exc

        idx = buf.find(separator)
        end = idx + len(separator)
        result = bytes(buf[:end])
        leftover = bytes(buf[end:])
        if leftover:
            self._pushback.extend(leftover)
        return result

    async def read_available(
        self,
        idle_timeout: float,
        max_bytes: int | None = None,
    ) -> bytes:
        port = self._require_port()
        buf = bytearray(self._pushback)
        self._pushback.clear()
        cap = max_bytes if max_bytes and max_bytes > 0 else None
        while True:
            if cap is not None and len(buf) >= cap:
                break
            with anyio.move_on_after(idle_timeout) as scope:
                try:
                    chunk = await port.receive(_RECEIVE_CHUNK)
                except (SerialClosedError, SerialDisconnectedError):
                    break
                except SerialError:
                    break
                buf.extend(chunk)
            if scope.cancelled_caught:
                break
        if cap is not None and len(buf) > cap:
            leftover = bytes(buf[cap:])
            self._pushback.extend(leftover)
            return bytes(buf[:cap])
        return bytes(buf)

    async def drain_input(self) -> None:
        self._pushback.clear()
        port = self._port
        if port is None:
            return
        # Best-effort — a drain failure shouldn't propagate.
        with contextlib.suppress(SerialError):
            await port.reset_input_buffer()

    # ------------------------------------------------------------------ props

    @property
    def is_open(self) -> bool:
        return self._port is not None and self._port.is_open

    @property
    def label(self) -> str:
        return self._settings.port

    # ------------------------------------------------------------------ internals

    def _require_port(self) -> SerialPort:
        port = self._port
        if port is None:
            raise AlicatConnectionError(
                f"{self.label} is not open",
                context=ErrorContext(port=self.label),
            )
        return port
