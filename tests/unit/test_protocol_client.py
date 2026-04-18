"""Tests for :class:`alicatlib.protocol.client.AlicatProtocolClient`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

from alicatlib.errors import (
    AlicatCommandRejectedError,
    AlicatProtocolError,
    AlicatTimeoutError,
)
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.transport import FakeTransport


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# query_line — single-line request/response.
# ---------------------------------------------------------------------------


class TestQueryLine:
    @pytest.mark.anyio
    async def test_basic_roundtrip_strips_eol(self) -> None:
        fake = FakeTransport({b"A\r": b"A +0.000 +25.0 N2\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)
        result = await client.query_line(b"A\r")
        assert result == b"A +0.000 +25.0 N2"
        assert fake.writes == (b"A\r",)

    @pytest.mark.anyio
    async def test_bare_question_mark_raises_rejection(self) -> None:
        fake = FakeTransport({b"BAD\r": b"?\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)
        with pytest.raises(AlicatCommandRejectedError):
            await client.query_line(b"BAD\r")

    @pytest.mark.anyio
    async def test_unit_id_prefixed_question_mark_raises_rejection(self) -> None:
        """Real devices emit ``A ?`` on rejection, not a bare ``?``."""
        fake = FakeTransport({b"ABAD\r": b"A ?\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)
        with pytest.raises(AlicatCommandRejectedError):
            await client.query_line(b"ABAD\r")

    @pytest.mark.anyio
    async def test_empty_response_raises_protocol_error(self) -> None:
        """A naked EOL (empty response) is malformed — must not be silent."""
        fake = FakeTransport({b"A\r": b"\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)
        with pytest.raises(AlicatProtocolError):
            await client.query_line(b"A\r")

    @pytest.mark.anyio
    async def test_response_containing_question_mark_is_not_a_rejection(self) -> None:
        """``?`` is only a rejection when it's the whole response."""
        fake = FakeTransport({b"A\r": b"A ?whatever\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)
        result = await client.query_line(b"A\r")
        assert result == b"A ?whatever"

    @pytest.mark.anyio
    async def test_read_timeout_surfaces(self) -> None:
        fake = FakeTransport()  # no scripted reply
        await fake.open()
        client = AlicatProtocolClient(fake)
        with pytest.raises(AlicatTimeoutError) as ei:
            await client.query_line(b"A\r", timeout=0.05)
        assert ei.value.context.extra.get("phase") == "read"

    @pytest.mark.anyio
    async def test_write_timeout_distinguished_from_read(self) -> None:
        """The write-phase timeout context must be distinguishable from read."""
        fake = FakeTransport()
        await fake.open()
        fake.force_write_timeout()
        client = AlicatProtocolClient(fake)
        with pytest.raises(AlicatTimeoutError) as ei:
            await client.query_line(b"A\r")
        assert ei.value.context.extra.get("phase") == "write"


# ---------------------------------------------------------------------------
# query_lines — multiline with three termination signals (priority order).
# ---------------------------------------------------------------------------


class TestQueryLinesMaxLines:
    @pytest.mark.anyio
    async def test_max_lines_caps_collection(self) -> None:
        fake = FakeTransport(
            {b"A??M*\r": [b"l1\r", b"l2\r", b"l3\r", b"l4\r", b"l5\r"]},
        )
        await fake.open()
        client = AlicatProtocolClient(fake)
        result = await client.query_lines(b"A??M*\r", max_lines=3)
        assert result == (b"l1", b"l2", b"l3")

    @pytest.mark.anyio
    async def test_max_lines_avoids_idle_timeout_metric(self) -> None:
        """Hitting max_lines is a clean exit — the idle-timeout metric must not tick."""
        fake = FakeTransport({b"A\r": [b"l1\r", b"l2\r", b"l3\r"]})
        await fake.open()
        client = AlicatProtocolClient(fake)
        await client.query_lines(b"A\r", max_lines=3)
        assert client.idle_timeout_exits == 0


class TestQueryLinesIsComplete:
    @pytest.mark.anyio
    async def test_is_complete_predicate_terminates(self) -> None:
        fake = FakeTransport(
            {b"A\r": [b"l1\r", b"l2\r", b"END\r", b"l4\r"]},
        )
        await fake.open()
        client = AlicatProtocolClient(fake)

        def done(lines: Sequence[bytes]) -> bool:
            return bool(lines) and lines[-1] == b"END"

        result = await client.query_lines(b"A\r", is_complete=done)
        assert result == (b"l1", b"l2", b"END")
        assert client.idle_timeout_exits == 0

    @pytest.mark.anyio
    async def test_is_complete_checked_before_max_lines(self) -> None:
        """Priority order: is_complete beats max_lines."""
        fake = FakeTransport({b"A\r": [b"l1\r", b"DONE\r", b"l3\r", b"l4\r"]})
        await fake.open()
        client = AlicatProtocolClient(fake)

        def done(lines: Sequence[bytes]) -> bool:
            return bool(lines) and lines[-1] == b"DONE"

        result = await client.query_lines(b"A\r", is_complete=done, max_lines=10)
        assert result == (b"l1", b"DONE")


class TestQueryLinesIdleTimeout:
    @pytest.mark.anyio
    async def test_idle_timeout_exit_increments_metric(self) -> None:
        """When neither is_complete nor max_lines is set, idle-timeout fires."""
        fake = FakeTransport({b"A\r": [b"l1\r", b"l2\r"]})
        await fake.open()
        client = AlicatProtocolClient(fake, multiline_idle_timeout=0.05)
        result = await client.query_lines(b"A\r")
        assert result == (b"l1", b"l2")
        assert client.idle_timeout_exits == 1

    @pytest.mark.anyio
    async def test_reset_idle_timeout_metric(self) -> None:
        fake = FakeTransport({b"A\r": b"l1\r"})
        await fake.open()
        client = AlicatProtocolClient(fake, multiline_idle_timeout=0.05)
        await client.query_lines(b"A\r")
        assert client.idle_timeout_exits == 1
        client.reset_idle_timeout_metric()
        assert client.idle_timeout_exits == 0

    @pytest.mark.anyio
    async def test_first_line_timeout_is_error_not_empty_tuple(self) -> None:
        """If the first line never arrives, raise — don't silently return ()."""
        fake = FakeTransport()
        await fake.open()
        client = AlicatProtocolClient(fake)
        with pytest.raises(AlicatTimeoutError):
            await client.query_lines(b"A\r", first_timeout=0.05)

    @pytest.mark.anyio
    async def test_first_line_rejection_raises(self) -> None:
        fake = FakeTransport({b"BAD\r": b"?\r"})
        await fake.open()
        client = AlicatProtocolClient(fake)
        with pytest.raises(AlicatCommandRejectedError):
            await client.query_lines(b"BAD\r")


# ---------------------------------------------------------------------------
# write_only
# ---------------------------------------------------------------------------


class TestWriteOnly:
    @pytest.mark.anyio
    async def test_write_only_emits_bytes_without_reading(self) -> None:
        fake = FakeTransport()
        await fake.open()
        client = AlicatProtocolClient(fake)
        await client.write_only(b"@@ 5\r")
        assert fake.writes == (b"@@ 5\r",)

    @pytest.mark.anyio
    async def test_write_only_respects_write_timeout(self) -> None:
        fake = FakeTransport()
        await fake.open()
        fake.force_write_timeout()
        client = AlicatProtocolClient(fake)
        with pytest.raises(AlicatTimeoutError) as ei:
            await client.write_only(b"A\r")
        assert ei.value.context.extra.get("phase") == "write"


# ---------------------------------------------------------------------------
# Concurrency — one in-flight at a time.
# ---------------------------------------------------------------------------


class TestOneInFlight:
    @pytest.mark.anyio
    async def test_concurrent_callers_serialise(self) -> None:
        """Two tasks share the client; the second must wait for the first."""
        fake = FakeTransport(
            {b"A\r": b"A ok\r", b"B\r": b"B ok\r"},
            latency_s=0.05,
        )
        await fake.open()
        client = AlicatProtocolClient(fake, default_timeout=1.0)

        start_times: list[float] = []
        end_times: list[float] = []
        loop_start = anyio.current_time()

        async def caller(cmd: bytes) -> None:
            start_times.append(anyio.current_time() - loop_start)
            await client.query_line(cmd)
            end_times.append(anyio.current_time() - loop_start)

        async with anyio.create_task_group() as tg:
            tg.start_soon(caller, b"A\r")
            tg.start_soon(caller, b"B\r")

        # Exactly two calls ran.
        assert len(start_times) == 2
        assert len(end_times) == 2
        # One command's end came before the other's end — serialisation proof.
        end_times.sort()
        # The two ends are separated by at least one latency period (≥0.05s).
        assert end_times[1] - end_times[0] >= 0.04


# ---------------------------------------------------------------------------
# drain_before_write — recovery hook
# ---------------------------------------------------------------------------


class TestDrainBeforeWrite:
    @pytest.mark.anyio
    async def test_drain_before_write_clears_stale_bytes(self) -> None:
        """With drain enabled, unsolicited bytes in the buffer are discarded
        before the next command, so its reply isn't mis-read as the previous
        command's result.
        """
        fake = FakeTransport({b"A\r": b"A ok\r"})
        await fake.open()
        fake.feed(b"stale\r")  # would otherwise be returned to the caller
        client = AlicatProtocolClient(fake, drain_before_write=True)
        result = await client.query_line(b"A\r")
        assert result == b"A ok"

    @pytest.mark.anyio
    async def test_without_drain_stale_bytes_leak_to_caller(self) -> None:
        """Default behaviour — stale bytes come back as the response. This
        test pins that contract so enabling ``drain_before_write`` is a
        visible choice, not an undocumented side effect.
        """
        fake = FakeTransport({b"A\r": b"A ok\r"})
        await fake.open()
        fake.feed(b"stale\r")
        client = AlicatProtocolClient(fake)  # drain_before_write defaults False
        result = await client.query_line(b"A\r")
        assert result == b"stale"
