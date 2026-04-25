"""Tests for :class:`alicatlib.sync.portal.SyncPortal`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

from alicatlib.errors import AlicatTimeoutError
from alicatlib.sync import SyncPortal, run_sync

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


class TestLifecycle:
    def test_running_flag_tracks_context(self) -> None:
        portal = SyncPortal()
        assert portal.running is False
        with portal:
            assert portal.running is True
        assert portal.running is False  # type: ignore[unreachable]

    def test_portal_is_one_shot(self) -> None:
        portal = SyncPortal()
        with portal:
            pass
        with pytest.raises(RuntimeError, match="not reusable"):
            portal.__enter__()

    def test_call_before_enter_raises(self) -> None:
        portal = SyncPortal()

        async def noop() -> int:
            return 1

        with pytest.raises(RuntimeError, match="not running"):
            portal.call(noop)

    def test_call_after_exit_raises(self) -> None:
        portal = SyncPortal()
        with portal:
            pass

        async def noop() -> int:
            return 1

        with pytest.raises(RuntimeError, match="not running"):
            portal.call(noop)


class TestCall:
    def test_positional_args(self) -> None:
        async def add(a: int, b: int) -> int:
            return a + b

        with SyncPortal() as portal:
            assert portal.call(add, 2, 3) == 5

    def test_keyword_args(self) -> None:
        async def greet(name: str, *, greeting: str = "hi") -> str:
            return f"{greeting} {name}"

        with SyncPortal() as portal:
            assert portal.call(greet, "world", greeting="hello") == "hello world"

    def test_reentrant_calls(self) -> None:
        async def identity(x: int) -> int:
            return x

        with SyncPortal() as portal:
            assert [portal.call(identity, n) for n in range(5)] == [0, 1, 2, 3, 4]

    def test_exception_propagates_unwrapped(self) -> None:
        async def raises() -> None:
            raise AlicatTimeoutError("timeout")

        with SyncPortal() as portal, pytest.raises(AlicatTimeoutError, match="timeout"):
            portal.call(raises)

    def test_single_member_group_is_unwrapped(self) -> None:
        async def inner() -> None:
            raise AlicatTimeoutError("timeout")

        async def outer() -> None:
            async with anyio.create_task_group() as tg:
                tg.start_soon(inner)

        with SyncPortal() as portal, pytest.raises(AlicatTimeoutError, match="timeout"):
            portal.call(outer)

    def test_multi_member_group_is_preserved(self) -> None:
        async def fail_a() -> None:
            raise AlicatTimeoutError("a")

        async def fail_b() -> None:
            raise ValueError("b")

        async def outer() -> None:
            async with anyio.create_task_group() as tg:
                tg.start_soon(fail_a)
                tg.start_soon(fail_b)

        with SyncPortal() as portal, pytest.raises(BaseExceptionGroup) as excinfo:
            portal.call(outer)
        assert len(excinfo.value.exceptions) == 2


class TestWrapAsyncContextManager:
    def test_enters_and_exits_like_sync_cm(self) -> None:
        events: list[str] = []

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def acm() -> AsyncGenerator[str]:
            events.append("enter")
            try:
                yield "inside"
            finally:
                events.append("exit")

        with SyncPortal() as portal, portal.wrap_async_context_manager(acm()) as value:
            assert value == "inside"
            events.append("body")

        assert events == ["enter", "body", "exit"]


class TestWrapAsyncIter:
    def test_iterates_values(self) -> None:
        async def producer() -> AsyncIterator[int]:
            for i in range(3):
                yield i

        with SyncPortal() as portal:
            it = portal.wrap_async_iter(producer())
            assert list(it) == [0, 1, 2]

    def test_context_manager_closes_iterator(self) -> None:
        closed = False

        async def producer() -> AsyncIterator[int]:
            nonlocal closed
            try:
                for i in range(100):
                    yield i
            finally:
                closed = True

        with SyncPortal() as portal, portal.wrap_async_iter(producer()) as it:
            assert next(it) == 0
            assert next(it) == 1

        assert closed is True

    def test_close_is_idempotent(self) -> None:
        async def producer() -> AsyncIterator[int]:
            yield 1

        with SyncPortal() as portal:
            it = portal.wrap_async_iter(producer())
            it.close()
            it.close()

    def test_stop_async_iteration_maps_to_stop_iteration(self) -> None:
        async def producer() -> AsyncIterator[int]:
            if False:
                yield 0  # type: ignore[unreachable]

        with SyncPortal() as portal:
            it = portal.wrap_async_iter(producer())
            with pytest.raises(StopIteration):
                next(it)

    def test_exceptions_in_producer_propagate(self) -> None:
        async def producer() -> AsyncIterator[int]:
            yield 1
            raise AlicatTimeoutError("producer failed")

        with SyncPortal() as portal:
            it = portal.wrap_async_iter(producer())
            assert next(it) == 1
            with pytest.raises(AlicatTimeoutError, match="producer failed"):
                next(it)


class TestRunSync:
    def test_runs_single_coroutine(self) -> None:
        async def double(x: int) -> int:
            return x * 2

        assert run_sync(double, 21) == 42

    def test_exception_unwraps(self) -> None:
        async def inner() -> None:
            raise AlicatTimeoutError("boom")

        async def outer() -> None:
            async with anyio.create_task_group() as tg:
                tg.start_soon(inner)

        with pytest.raises(AlicatTimeoutError, match="boom"):
            run_sync(outer)
