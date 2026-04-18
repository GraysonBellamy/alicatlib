"""Runtime tests for :class:`alicatlib.devices.streaming.StreamingSession`.

Exercises the full streaming lifecycle against :class:`FakeTransport`:

- ``__aenter__`` writes ``{uid}@ @`` under the port lock.
- ``__aexit__`` always writes ``@@ {uid}`` — including on body raise.
- The client's streaming latch is set on enter, cleared on exit.
- Normal :meth:`Session.execute` refuses commands while streaming.
- The producer parses frames through the session's cached
  :class:`DataFrameFormat` and yields :class:`DataFrame` instances.
- Strict-mode parse errors propagate out of ``__anext__``.
- Lenient-mode parse errors are logged and skipped.
- Double-enter fails loudly (one streamer per client).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import anyio
import pytest

from alicatlib.commands import Capability
from alicatlib.commands.polling import POLL_DATA, PollRequest
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.data_frame import (
    DataFrame,
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.devices.models import DeviceInfo
from alicatlib.devices.session import Session
from alicatlib.devices.streaming import OverflowPolicy, StreamingSession
from alicatlib.errors import (
    AlicatParseError,
    AlicatStreamingModeError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import Statistic
from alicatlib.transport import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _info(firmware: str = "10v05") -> DeviceInfo:
    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model="MC-100SCCM-D",
        serial="123456",
        manufactured="2021-01-01",
        calibrated="2021-02-01",
        calibrated_by="ACS",
        software=firmware,
        firmware=FirmwareVersion.parse(firmware),
        firmware_date=date(2021, 5, 19),
        kind=DeviceKind.FLOW_CONTROLLER,
        media=Medium.GAS,
        capabilities=Capability.NONE,
    )


def _mc_frame_format() -> DataFrameFormat:
    """2-field data-frame format matching the fixtures used in other tests."""

    def _text(value: str) -> float | str | None:
        return value

    def _decimal(value: str) -> float | str | None:
        return parse_optional_float(value, field="decimal")

    names = [
        ("Unit_ID", "text", _text, Statistic.NONE),
        ("Mass_Flow", "decimal", _decimal, Statistic.MASS_FLOW),
    ]
    return DataFrameFormat(
        fields=tuple(
            DataFrameField(
                name=n,
                raw_name=n,
                type_name=t,
                statistic=s,
                unit=None,
                conditional=False,
                parser=p,
            )
            for n, t, p, s in names
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


async def _make_session(
    script: Mapping[bytes, ScriptedReply] | None = None,
    *,
    with_frame_format: bool = True,
) -> tuple[Session, FakeTransport, AlicatProtocolClient]:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    # Shorter default_timeout keeps parse-error + gate tests snappy —
    # streaming tests drive the transport's own read_until via the
    # producer, not via query_line.
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    session = Session(
        client,
        unit_id="A",
        info=_info(),
        data_frame_format=_mc_frame_format() if with_frame_format else None,
        port_label="fake://test",
    )
    return session, fake, client


def _feed_frame(fake: FakeTransport, mass_flow: float) -> None:
    """Push one data frame into the fake transport's read buffer."""
    fake.feed(f"A {mass_flow:.3f}\r".encode("ascii"))


# ---------------------------------------------------------------------------
# Enter / exit wire sequencing
# ---------------------------------------------------------------------------


class TestEnterExitWireBytes:
    @pytest.mark.anyio
    async def test_enter_writes_start_stream(self) -> None:
        """``__aenter__`` writes the primer's start-stream bytes verbatim."""
        session, fake, client = await _make_session()
        stream = StreamingSession(session)
        async with stream:
            assert b"A@ @\r" in fake.writes
            assert client.is_streaming
        # After exit, stop-stream must be on the wire.
        assert b"@@ A\r" in fake.writes

    @pytest.mark.anyio
    async def test_exit_clears_streaming_latch(self) -> None:
        session, _, client = await _make_session()
        stream = StreamingSession(session)
        async with stream:
            assert client.is_streaming
        assert not client.is_streaming

    @pytest.mark.anyio
    async def test_exit_on_body_raise_still_sends_stop(self) -> None:
        """A consumer crash must not leave the device flooding the bus."""
        session, fake, client = await _make_session()
        stream = StreamingSession(session)
        with pytest.raises(RuntimeError, match="boom"):
            async with stream:
                raise RuntimeError("boom")
        assert b"@@ A\r" in fake.writes
        assert not client.is_streaming

    @pytest.mark.anyio
    async def test_rate_ms_configures_ncs_before_start(self) -> None:
        """``rate_ms`` triggers an NCS round-trip *before* the start-stream write."""
        session, fake, _ = await _make_session({b"ANCS 50\r": b"A 50\r"})
        stream = StreamingSession(session, rate_ms=50)
        async with stream:
            pass
        # NCS must appear before the start-stream bytes.
        ncs_idx = fake.writes.index(b"ANCS 50\r")
        start_idx = fake.writes.index(b"A@ @\r")
        assert ncs_idx < start_idx

    @pytest.mark.anyio
    async def test_rate_ms_none_skips_ncs(self) -> None:
        """``rate_ms=None`` leaves the device's current rate alone."""
        session, fake, _ = await _make_session()
        stream = StreamingSession(session, rate_ms=None)
        async with stream:
            pass
        for w in fake.writes:
            assert b"NCS" not in w

    @pytest.mark.anyio
    async def test_double_enter_is_rejected(self) -> None:
        """The same StreamingSession instance is not reusable after exit."""
        session, _, _ = await _make_session()
        stream = StreamingSession(session)
        async with stream:
            pass
        with pytest.raises(RuntimeError, match="not reusable"):
            async with stream:
                pass

    @pytest.mark.anyio
    async def test_concurrent_stream_rejected(self) -> None:
        """One streamer per client — a second session's stream must fail fast."""
        session, _, client = await _make_session()
        # Fake a second session sharing the client, each pointed at its own stream.
        other_session = Session(
            client,
            unit_id="B",
            info=_info(),
            data_frame_format=_mc_frame_format(),
            port_label="fake://test",
        )
        first = StreamingSession(session)
        async with first:
            second = StreamingSession(other_session)
            with pytest.raises(AlicatStreamingModeError):
                async with second:
                    pass


# ---------------------------------------------------------------------------
# Iteration / producer
# ---------------------------------------------------------------------------


class TestIteration:
    @pytest.mark.anyio
    async def test_yields_parsed_frames(self) -> None:
        """Feeding frames into the transport yields :class:`DataFrame`s."""
        session, fake, _ = await _make_session()
        _feed_frame(fake, 1.0)
        _feed_frame(fake, 2.5)
        _feed_frame(fake, 3.25)

        frames: list[DataFrame] = []
        stream = StreamingSession(session)
        async with stream:
            async for frame in stream:
                frames.append(frame)
                if len(frames) == 3:
                    break

        assert len(frames) == 3
        assert [f.values["Mass_Flow"] for f in frames] == [1.0, 2.5, 3.25]
        for f in frames:
            assert f.unit_id == "A"

    @pytest.mark.anyio
    async def test_frames_arrive_in_order(self) -> None:
        session, fake, _ = await _make_session()
        stream = StreamingSession(session)
        async with stream:
            for v in (10.0, 20.0, 30.0):
                _feed_frame(fake, v)
            values: list[float | str | None] = []
            for _ in range(3):
                frame = await stream.__anext__()
                values.append(frame.values["Mass_Flow"])
            assert values == [10.0, 20.0, 30.0]

    @pytest.mark.anyio
    async def test_space_prefixed_streamed_frame_parses(self) -> None:
        """Real hardware drops the unit-id letter on streaming frames.

        Primer p. 10 / design §5.8: when streaming starts, the device's
        unit id becomes ``@`` and emitted frames arrive with a leading
        space where the letter was. The producer must normalize by
        prepending the session's real unit id before
        :meth:`DataFrameFormat.parse` runs — otherwise every frame
        appears truncated by one required token.

        Regression guard for finding #1 from 2026-04-17:
        on MC-500SCCM-D @ 10v20.0-R24, frames arrive as
        ``b' +014.64 ... N2\\r'`` with a leading ``b' '`` instead of
        ``b'A'``. Pre-fix the lenient parser logged "skipping malformed
        frame" for every frame; post-fix the frame parses cleanly and
        ``frame.unit_id`` carries the session's configured unit id.
        """
        session, fake, _ = await _make_session()
        # Real-device shape: leading space instead of unit-id letter.
        fake.feed(b" 42.000\r")
        # Same frame but with leading "@" (the alternate streaming-mode
        # indicator from the primer text); must also normalize.
        fake.feed(b"@ 43.000\r")

        stream = StreamingSession(session)
        frames: list[DataFrame] = []
        async with stream:
            async for frame in stream:
                frames.append(frame)
                if len(frames) == 2:
                    break

        assert len(frames) == 2
        assert [f.values["Mass_Flow"] for f in frames] == [42.0, 43.0]
        # Unit id comes from the synthesized prefix, i.e. the session.
        for f in frames:
            assert f.unit_id == "A"


# ---------------------------------------------------------------------------
# Parse-error behaviour
# ---------------------------------------------------------------------------


class TestParseErrors:
    @pytest.mark.anyio
    async def test_lenient_mode_skips_bad_frame(self) -> None:
        """Default (strict=False): malformed frame logs + skipped, stream continues."""
        session, fake, _ = await _make_session()
        fake.feed(b"totally not a valid frame\r")
        _feed_frame(fake, 42.0)

        stream = StreamingSession(session, strict=False)
        async with stream:
            # First good frame may be the second physical frame; iterate
            # until we see 42.0, or give up after a short window.
            frame = None
            async for f in stream:
                if f.values["Mass_Flow"] == 42.0:
                    frame = f
                    break
            assert frame is not None
            assert frame.values["Mass_Flow"] == 42.0

    @pytest.mark.anyio
    async def test_strict_mode_propagates(self) -> None:
        """strict=True: parse error tears down the producer and surfaces on __anext__."""
        session, fake, _ = await _make_session()
        fake.feed(b"bad\r")  # 1 token — required fields >= 2, so this is a parse error

        stream = StreamingSession(session, strict=True)
        async with stream:
            # Give the producer a moment to hit the parse error.
            with anyio.move_on_after(0.5):
                with pytest.raises(AlicatParseError):
                    async for _ in stream:
                        pass


# ---------------------------------------------------------------------------
# Dispatch gate while streaming
# ---------------------------------------------------------------------------


class TestDispatchGate:
    @pytest.mark.anyio
    async def test_execute_refused_while_streaming(self) -> None:
        """Session.execute must fail fast with AlicatStreamingModeError."""
        session, _, _ = await _make_session()
        stream = StreamingSession(session)
        async with stream:
            with pytest.raises(AlicatStreamingModeError):
                await session.execute(POLL_DATA, PollRequest())

    @pytest.mark.anyio
    async def test_execute_works_before_and_after(self) -> None:
        """Outside the streaming scope, dispatch is normal."""
        session, _, _ = await _make_session({b"A\r": b"A 1.000\r"})
        stream = StreamingSession(session)
        # Before stream — works.
        before = await session.execute(POLL_DATA, PollRequest())
        assert before.unit_id == "A"
        async with stream:
            pass
        # After stream — still works.
        after = await session.execute(POLL_DATA, PollRequest())
        assert after.unit_id == "A"


# ---------------------------------------------------------------------------
# Overflow policy
# ---------------------------------------------------------------------------


class TestOverflow:
    @pytest.mark.anyio
    async def test_drop_oldest_counts_drops(self) -> None:
        """With a tiny buffer, an idle consumer sees drops recorded."""
        session, fake, _ = await _make_session()
        # Pre-load more frames than buffer_size=2 can hold; the producer
        # will fill the buffer and then start dropping.
        for v in range(10):
            _feed_frame(fake, float(v))

        stream = StreamingSession(
            session,
            overflow=OverflowPolicy.DROP_OLDEST,
            buffer_size=2,
        )
        async with stream:
            # Give the producer a chance to drain the feed into its buffer.
            await anyio.sleep(0.05)
            # Consume what's there.
            collected: list[float | str | None] = []
            with anyio.move_on_after(0.2):
                async for frame in stream:
                    collected.append(frame.values["Mass_Flow"])
                    if len(collected) >= 2:
                        break
        # Some frames must have been dropped (buffer_size=2 < 10 frames).
        assert stream.dropped_frames > 0
