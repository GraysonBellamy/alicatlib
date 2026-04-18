"""Transport layer abstraction — moves bytes, knows nothing about Alicat.

The :class:`Transport` :pep:`544` Protocol is the interface every backend
implements. :class:`SerialSettings` is the port-configuration dataclass
consumed by :class:`alicatlib.transport.serial.SerialTransport`.

Design reference: ``docs/design.md`` §5.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from anyserial import ByteSize, Parity, StopBits

__all__ = [
    "ByteSize",
    "Parity",
    "SerialSettings",
    "StopBits",
    "Transport",
]


class Transport(Protocol):
    """Byte-level transport.

    Every I/O boundary takes an explicit timeout. On expiry, implementations
    raise :class:`alicatlib.errors.AlicatTimeoutError` — never return an empty
    or partial ``bytes`` silently. Backend exceptions normalize to
    :class:`alicatlib.errors.AlicatTransportError` (or a subclass) with
    ``__cause__`` preserving the original exception.

    Lifecycle is single-shot: :meth:`open` once, :meth:`close` once.
    """

    async def open(self) -> None:
        """Open the underlying port. Idempotent re-calls are an error."""
        ...

    async def close(self) -> None:
        """Close the underlying port. Safe to call when already closed."""
        ...

    async def reopen(self, *, baudrate: int) -> None:
        """Close and re-open the underlying port at a new baud rate.

        Used by :meth:`Session.change_baud_rate` to retune the port
        after the device has already switched baud rates mid-sequence
        (primer ``NCB`` command — see design §5.7). Serial transports
        close the port, update the cached settings, and open at the
        new baud; non-serial transports (TCP, future) may raise
        :class:`NotImplementedError` — baud rates don't apply there.

        Implementations must leave the transport in a consistent
        state: either fully reopened at the new baud, or clearly
        closed so callers can recognise a failure. Silent partial
        states are the worst failure mode for this method.
        """
        ...

    async def write(self, data: bytes, *, timeout: float) -> None:
        """Write every byte of ``data``. Raise ``AlicatTimeoutError`` on expiry.

        A bounded write timeout is mandatory because sends can block on
        RS-485 hardware flow control, a stuck device, or (on TCP) a full send
        buffer. Callers that block indefinitely hide real hangs.
        """
        ...

    async def read_until(self, separator: bytes, timeout: float) -> bytes:
        """Read bytes up to and including the next occurrence of ``separator``.

        Raises :class:`alicatlib.errors.AlicatTimeoutError` if the separator
        does not arrive before ``timeout``. Bytes received after the separator
        remain buffered for the next call — implementations must not discard
        them.
        """
        ...

    async def read_available(
        self,
        idle_timeout: float,
        max_bytes: int | None = None,
    ) -> bytes:
        """Read until the line goes idle for ``idle_timeout`` seconds.

        Never raises on idle expiry — an idle timeout is the *expected* exit.
        Returns whatever was accumulated (possibly empty). Used for
        best-effort drain / stream-stop recovery, not for request/response.
        """
        ...

    async def drain_input(self) -> None:
        """Discard any buffered input bytes. Best-effort; never raises."""
        ...

    @property
    def is_open(self) -> bool:
        """Whether :meth:`open` has run without a matching :meth:`close`."""
        ...

    @property
    def label(self) -> str:
        """Short identifier (port path, URL, ``"fake://..."``) used in errors."""
        ...


@dataclass(frozen=True, slots=True)
class SerialSettings:
    """Serial-port configuration.

    Mirrors :class:`anyserial.SerialConfig` plus a ``port`` path. ``exclusive``
    defaults ``True`` so that two processes can't scribble over the same
    device — the Alicat wire protocol isn't multi-master tolerant.
    """

    port: str
    baudrate: int = 19200
    bytesize: ByteSize = ByteSize.EIGHT
    parity: Parity = Parity.NONE
    stopbits: StopBits = StopBits.ONE
    rtscts: bool = False
    xonxoff: bool = False
    exclusive: bool = True
