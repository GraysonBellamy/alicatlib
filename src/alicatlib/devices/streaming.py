r"""Streaming-mode runtime — :class:`StreamingSession`.

Streaming mode is a *port-level state transition*, not a
request/response command (design §5.8). The device stops responding to
prompts, overwrites its unit id with ``@``, and pushes data frames
continuously until stopped. This module owns that runtime:

- Setup — optionally configures ``NCS`` (streaming rate), marks the
  shared :class:`~alicatlib.protocol.client.AlicatProtocolClient` as
  streaming, writes the primer's ``{unit_id}@ @\r`` start-stream bytes
  directly under the port lock (bypassing :meth:`Session.execute`
  because we own the mode transition, not the command layer).
- Producer — a background task reads frames from the transport into a
  bounded :mod:`anyio.streams.memory` object stream, parsing each line
  with the session's cached
  :class:`~alicatlib.devices.data_frame.DataFrameFormat`. Overflow is
  controlled by :class:`OverflowPolicy` (design §5.14 — re-used from
  the sample recorder so the knob is one concept across acquisition
  surfaces). Parse errors are logged and skipped unless
  ``strict=True``.
- Teardown — always writes the primer's ``@@ {unit_id}\r`` stop-stream
  bytes, drains any trailing frames, and clears the streaming latch.
  ``__aexit__`` does this even when the body raised, so a crashed
  consumer never leaves the device flooding the bus.

Shape:

.. code-block:: python

    async with dev.stream(rate_ms=50) as stream:
        async for frame in stream:
            process(frame)

Design reference: ``docs/design.md`` §5.8.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from time import monotonic_ns
from typing import TYPE_CHECKING, Self

import anyio
from anyio.lowlevel import checkpoint

from alicatlib._logging import get_logger
from alicatlib.commands.streaming import (
    STREAMING_RATE,
    StreamingRateRequest,
    encode_start_stream,
    encode_stop_stream,
)
from alicatlib.devices.data_frame import DataFrame
from alicatlib.errors import (
    AlicatParseError,
    AlicatStreamingModeError,
    AlicatTimeoutError,
    AlicatTransportError,
    ErrorContext,
)
from alicatlib.protocol.framing import strip_eol
from alicatlib.streaming import OverflowPolicy

if TYPE_CHECKING:
    from types import TracebackType

    from anyio.abc import TaskGroup
    from anyio.streams.memory import (
        MemoryObjectReceiveStream,
        MemoryObjectSendStream,
    )

    from alicatlib.devices.data_frame import DataFrameFormat
    from alicatlib.devices.session import Session


__all__ = ["OverflowPolicy", "StreamingSession"]


_logger = get_logger("streaming")


#: Default bounded buffer depth for the producer/consumer split.
#: 256 frames at 50 ms cadence is ~13 s of backlog — long enough to
#: absorb a slow consumer without stalling the producer, short enough
#: that ``DROP_OLDEST`` visibly discards stale data instead of letting
#: the queue grow unbounded.
_DEFAULT_BUFFER_SIZE: int = 256


#: Idle-timeout window for the producer's ``read_until`` — the device
#: normally emits a frame every ``NCS`` ms (default 50 ms on V10), so
#: 1 s is long enough that a healthy stream never times out and short
#: enough that a dead bus surfaces as a log line within a second.
_PRODUCER_READ_TIMEOUT_S: float = 1.0


#: Idle-drain window on stop-stream. Primer says the device stops
#: pushing frames "immediately" after ``@@``, but a partial frame may
#: still be in transit; 100 ms is the same figure the factory's
#: stale-stream recovery uses (design §16.6), so the two paths drain
#: to the same empty state.
_STOP_DRAIN_WINDOW_S: float = 0.1


class StreamingSession:
    """Async context manager + async iterator for streaming data frames.

    Users construct this via :meth:`Device.stream`, not directly. The
    public contract is the dunder surface — ``__aenter__`` /
    ``__aexit__`` for scope and ``__aiter__`` / ``__anext__`` for the
    data. Once the context exits, the instance is not reusable; the
    next stream requires a new call to :meth:`Device.stream`.

    Args:
        session: The owning :class:`Session`. Streaming shares the
            session's port lock and its cached
            :class:`DataFrameFormat`.
        rate_ms: If not ``None``, configures ``NCS`` before entering
            streaming mode. ``0`` is the primer's "as-fast-as-possible"
            setting; distinct from ``None`` (which means "leave the
            device's current rate alone"). Firmware-gated at V10 >=
            10v05 by the underlying ``STREAMING_RATE`` command, so
            passing a value on older firmware fails pre-I/O at the
            session gate.
        strict: If ``True``, :class:`AlicatParseError` from a malformed
            frame propagates out of :meth:`__anext__` and tears down
            the stream via the task group. If ``False`` (default), the
            error is logged at WARN and the producer continues.
        overflow: Back-pressure policy when the bounded producer buffer
            is full. Defaults to :attr:`OverflowPolicy.DROP_OLDEST` —
            latest-data-wins is the right default for high-rate
            telemetry. :attr:`OverflowPolicy.BLOCK` is valid but risks
            the OS-level serial buffer dropping bytes if the consumer
            stays behind for long; ``DROP_NEWEST`` keeps the oldest
            queued frame.
        buffer_size: Producer/consumer buffer depth.

    Attributes:
        dropped_frames: Count of frames the producer had to discard
            because the consumer was behind and ``overflow`` is not
            ``BLOCK``. Available after the CM exits.
    """

    def __init__(
        self,
        session: Session,
        *,
        rate_ms: int | None = None,
        strict: bool = False,
        overflow: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
    ) -> None:
        self._session = session
        self._client = session._client  # pyright: ignore[reportPrivateUsage] — co-owned state
        self._rate_ms = rate_ms
        self._strict = strict
        self._overflow = overflow
        self._buffer_size = buffer_size
        self._format: DataFrameFormat | None = session.data_frame_format
        self._send: MemoryObjectSendStream[DataFrame] | None = None
        self._recv: MemoryObjectReceiveStream[DataFrame] | None = None
        self._task_group: TaskGroup | None = None
        self._entered = False
        self._producer_failure: BaseException | None = None
        self.dropped_frames: int = 0

    # ---------------------------------------------------------------- context

    async def __aenter__(self) -> Self:
        """Enter streaming mode.

        Sequence:

        1. Lazy-probe ``??D*`` if the session has no cached
           :class:`DataFrameFormat` — streaming has to parse every
           frame, so a missing format is a hard error the moment the
           producer starts.
        2. Optionally configure ``NCS`` rate. Done *before* flipping
           the streaming latch so the command still runs as a normal
           request/response.
        3. Acquire the port lock, verify the client isn't already
           streaming, flip the latch, write the start-stream bytes,
           release the lock. Holding the lock across the latch + write
           is what makes the mode transition atomic w.r.t. other
           sessions on the same client.
        4. Start the producer task inside a task group. The group
           lives for the duration of the context and is cancelled by
           :meth:`__aexit__`.
        """
        if self._entered:
            raise RuntimeError("StreamingSession is not reusable after exit")
        self._entered = True

        # Step 1: cached format or lazy-probe.
        if self._format is None:
            self._format = await self._session.refresh_data_frame_format()

        # Step 2: optional NCS rate config. Skipped for rate_ms=None.
        # ``0`` is a real setting ("as fast as possible") — distinct
        # from None per the StreamingRateRequest contract.
        if self._rate_ms is not None:
            await self._session.execute(
                STREAMING_RATE,
                StreamingRateRequest(rate_ms=self._rate_ms),
            )

        # Step 3: atomic mode transition under the port lock.
        async with self._client.lock:
            if self._client.is_streaming:
                raise AlicatStreamingModeError(
                    "client is already streaming — only one streamer per port",
                    context=ErrorContext(
                        unit_id=self._session.unit_id,
                        port=self._session.port_label,
                        extra={"streaming": True},
                    ),
                )
            self._client._mark_streaming(True)  # pyright: ignore[reportPrivateUsage]
            try:
                await self._client.transport.write(
                    encode_start_stream(self._session.unit_id),
                    timeout=self._session.config.write_timeout_s,
                )
            except BaseException:
                # Pre-write failure never left the device streaming;
                # clear the latch so the next enter can proceed.
                self._client._mark_streaming(False)  # pyright: ignore[reportPrivateUsage]
                raise

        # Step 4: producer task. The task group is entered here and
        # exited in ``__aexit__`` — the producer's lifetime matches the
        # streaming context exactly.
        self._send, self._recv = anyio.create_memory_object_stream[DataFrame](
            max_buffer_size=self._buffer_size,
        )
        task_group = anyio.create_task_group()
        await task_group.__aenter__()
        task_group.start_soon(self._producer_loop)
        self._task_group = task_group
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit streaming mode — always sends stop-stream.

        Order is load-bearing:

        1. Cancel the producer task group and close the send side of
           the buffer so any pending ``__anext__`` receives
           ``StopAsyncIteration``.
        2. Send stop-stream bytes and drain. If the body raised this
           still has to happen — the device would otherwise keep
           pushing frames onto a bus no one is reading.
        3. Clear the streaming latch so other sessions on this client
           resume dispatching.
        """
        del exc_type, exc, tb

        # Step 1: cancel producer. The task group's cancel_scope unwinds
        # the read loop; we then exit the task group to join the task.
        # Swallow cancellation artifacts — this is a cleanup path, any
        # failure at this layer is secondary to the stop-stream write.
        tg = self._task_group
        self._task_group = None
        if tg is not None:
            tg.cancel_scope.cancel()
            # AnyIO cancellation of the producer task wraps the Cancelled
            # into an ExceptionGroup that we surface on tg.__aexit__.
            # This is a cleanup path; a genuine failure inside the
            # producer has already been recorded via
            # ``self._producer_failure`` (re-raised by __anext__), so
            # suppressing here only hides the expected cancel artefacts.
            with contextlib.suppress(BaseException):
                await tg.__aexit__(None, None, None)

        # Close both sides of the memory object stream. The send side
        # releases any consumer still blocked in ``__anext__`` with
        # ``EndOfStream``; closing the receive side too is required
        # because anyio emits an ``Unclosed`` warning (which pytest
        # promotes to a failure via ``sys.unraisablehook``) if either
        # end is garbage-collected without ``aclose``. Both are
        # idempotent.
        if self._send is not None:
            await self._send.aclose()
        if self._recv is not None:
            await self._recv.aclose()

        # Step 2: stop-stream + drain, under the lock. This is the
        # symmetric partner to the start-stream write in __aenter__;
        # write happens even if the producer failed or the body raised.
        try:
            async with self._client.lock:
                # Transport may already be torn down — the device is
                # the caller's problem at that point, but the latch
                # still needs clearing below so we suppress and move
                # on rather than swallow a failing stop-stream silently.
                with contextlib.suppress(AlicatTransportError):
                    await self._client.transport.write(
                        encode_stop_stream(self._session.unit_id),
                        timeout=self._session.config.write_timeout_s,
                    )
                with contextlib.suppress(AlicatTransportError):
                    await self._client.transport.read_available(
                        idle_timeout=_STOP_DRAIN_WINDOW_S,
                    )
        finally:
            # Step 3: always clear the latch, even if the transport
            # writes above raised. Leaving it set would permanently
            # brick the client for request/response use.
            self._client._mark_streaming(False)  # pyright: ignore[reportPrivateUsage]

    # ---------------------------------------------------------------- iterator

    def __aiter__(self) -> Self:
        """Return self — :class:`StreamingSession` is its own iterator."""
        return self

    async def __anext__(self) -> DataFrame:
        """Return the next buffered :class:`DataFrame`.

        Raises :class:`StopAsyncIteration` when the producer has closed
        the send side (either on context exit, or under
        ``strict=True`` after a parse error tore the task group down).
        A strict-mode parse error is re-raised here so the caller's
        ``async for`` loop surfaces the real exception, not a silent
        stop.
        """
        if self._recv is None:
            raise RuntimeError(
                "StreamingSession.__anext__ called outside its async-with body",
            )
        try:
            return await self._recv.receive()
        except anyio.EndOfStream:
            if self._producer_failure is not None:
                failure = self._producer_failure
                self._producer_failure = None
                raise failure from None
            raise StopAsyncIteration from None

    # ---------------------------------------------------------------- producer

    async def _producer_loop(self) -> None:
        """Read frames from the transport until cancelled or strict-failing.

        The loop never holds the port lock — we acquired and released
        it in ``__aenter__`` to write the mode-transition bytes, and we
        re-acquire it in ``__aexit__`` for the stop-stream write.
        While streaming, nothing else dispatches on this client (the
        streaming gate in ``Session._dispatch`` sees to that), so the
        transport's read side is ours exclusively.
        """
        assert self._send is not None  # noqa: S101 — narrow for type checker
        assert self._format is not None  # noqa: S101 — narrow for type checker
        try:
            while True:
                # Explicit checkpoint: the real transport's
                # ``read_until`` yields at its internal await; the
                # ``FakeTransport`` can return synchronously (raises
                # immediately on empty buffer). Without this
                # checkpoint, the loop below becomes a tight sync
                # busy-loop under FakeTransport and cancellation from
                # ``__aexit__`` never fires. Adding a checkpoint here
                # costs nothing on real hardware and fixes the test
                # path deterministically.
                await checkpoint()
                try:
                    raw = await self._client.transport.read_until(
                        self._client.eol,
                        timeout=_PRODUCER_READ_TIMEOUT_S,
                    )
                except AlicatTimeoutError:
                    # Idle window expired without a frame. Don't treat
                    # as failure — the device may be paused or the
                    # rate may be below our read window. Keep looping
                    # until cancelled.
                    continue
                except AlicatTransportError as err:
                    # Transport torn down mid-stream (port yanked,
                    # process shutting down). Record the failure so
                    # __anext__ can raise it, then exit the loop.
                    self._producer_failure = err
                    return

                stripped = strip_eol(raw, eol=self._client.eol)
                if not stripped:
                    # Bare EOL — skip rather than feed an empty frame
                    # into the parser.
                    continue

                # Streaming mode replaces the unit-id letter on the
                # wire (primer p. 10 / design §5.8: "its unit ID becomes
                # @"). Empirically on 10v20 the letter is dropped entirely,
                # leaving a leading space; the primer text allows a
                # literal '@' in the same slot. Normalize either form
                # back to the request/response shape so the single
                # DataFrameFormat.parse path handles both.
                first = stripped[:1]
                if first == b" ":
                    stripped = self._session.unit_id.encode("ascii") + stripped
                elif first == b"@":
                    stripped = self._session.unit_id.encode("ascii") + stripped[1:]

                try:
                    parsed = self._format.parse(stripped)
                except AlicatParseError as err:
                    if self._strict:
                        self._producer_failure = err
                        return
                    _logger.warning(
                        "streaming: skipping malformed frame",
                        extra={
                            "unit_id": self._session.unit_id,
                            "raw": stripped,
                            "error": str(err),
                        },
                    )
                    continue

                frame = DataFrame.from_parsed(
                    parsed,
                    format=self._format,
                    received_at=datetime.now(UTC),
                    monotonic_ns=monotonic_ns(),
                )
                await self._dispatch_frame(frame)
        finally:
            # Close the send side so consumers get StopAsyncIteration.
            # Safe to call twice — aclose is idempotent.
            if self._send is not None:  # pyright: ignore[reportUnnecessaryComparison]
                await self._send.aclose()

    async def _dispatch_frame(self, frame: DataFrame) -> None:
        """Hand ``frame`` to the consumer, honouring the overflow policy."""
        assert self._send is not None  # noqa: S101 — narrow for type checker
        assert self._recv is not None  # noqa: S101 — narrow for type checker
        if self._overflow is OverflowPolicy.BLOCK:
            await self._send.send(frame)
            return

        try:
            self._send.send_nowait(frame)
            return
        except anyio.WouldBlock:
            pass

        self.dropped_frames += 1
        if self._overflow is OverflowPolicy.DROP_NEWEST:
            return
        # DROP_OLDEST: evict one from the receive side, then retry.
        # If another consumer drained between our checks the retry may
        # still WouldBlock; in that case we drop ``frame`` and move on —
        # the buffer is pathologically contested, and one drop is
        # better than stalling the producer.
        try:
            self._recv.receive_nowait()
        except anyio.WouldBlock:
            return
        try:
            self._send.send_nowait(frame)
        except anyio.WouldBlock:
            return
