"""Blocking portal primitive — sync access to the async core.

:class:`SyncPortal` wraps :func:`anyio.from_thread.start_blocking_portal`
so the rest of the sync facade (device, manager, recording, sinks,
discovery) can share one dispatch primitive.

Shape:

* **Lifecycle is a plain ``with`` block.** Each portal owns one
  background event-loop thread; the portal closes when the block exits.
  Portals are one-shot — re-entering after exit raises.
* **``call(func, *args, **kwargs)`` runs a coroutine.** ``kwargs`` are
  bound through :func:`functools.partial` because
  :meth:`anyio.from_thread.BlockingPortal.call` only accepts positional
  arguments.
* **Single-member :class:`ExceptionGroup` s are unwrapped.** Our async
  core runs inside task groups (manager, recorder, factory), so AnyIO
  occasionally rewraps a single exception into a group. Unwrap so
  callers see the concrete :class:`~alicatlib.errors.AlicatError`
  subclass they branch on. Multi-member groups pass through unchanged —
  those carry real aggregate failures (design §5.13).
* **``wrap_async_context_manager`` delegates** to the portal's own
  helper — no extra behaviour, but exposed through :class:`SyncPortal`
  so callers reach for one surface.
* **``wrap_async_iter`` bridges async iteration.** The returned
  :class:`SyncAsyncIterator` is both iterable and closeable; outer
  sync CMs (e.g. ``sync.record()``) call :meth:`close` on exit to
  cancel the producer promptly.

Design reference: ``docs/design.md`` §5.16.
"""

from __future__ import annotations

import contextlib
from functools import partial
from typing import TYPE_CHECKING, Any, Self, cast

from anyio.from_thread import start_blocking_portal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from contextlib import AbstractAsyncContextManager, AbstractContextManager
    from types import TracebackType

    from anyio.from_thread import BlockingPortal

__all__ = ["SyncPortal", "run_sync"]


def _unwrap_single_group(exc: BaseException) -> BaseException:
    """Strip single-member :class:`BaseExceptionGroup` wrappers.

    AnyIO task groups with a single failing task emit a one-element
    :class:`ExceptionGroup`; unwrap those so sync callers can catch
    concrete exception types directly. A group that still holds two or
    more members represents a genuine aggregate failure and is
    returned unchanged.
    """
    while isinstance(exc, BaseExceptionGroup):
        group = cast("BaseExceptionGroup[BaseException]", exc)
        if len(group.exceptions) != 1:
            return group
        exc = group.exceptions[0]
    return exc


class SyncPortal:
    """Per-context wrapper around :class:`anyio.from_thread.BlockingPortal`.

    Example:
        >>> with SyncPortal() as portal:
        ...     result = portal.call(some_async_func, arg1, arg2)

    Args:
        backend: AnyIO backend to run on. Defaults to ``"asyncio"``; the
            sync facade does not expose trio-specific features, so there
            is no reason to change this unless the surrounding process
            already runs a trio loop.
    """

    def __init__(self, *, backend: str = "asyncio") -> None:
        self._backend = backend
        self._cm: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._entered = False

    @property
    def running(self) -> bool:
        """``True`` between :meth:`__enter__` and :meth:`__exit__`."""
        return self._portal is not None

    def __enter__(self) -> Self:
        """Start the portal's background thread and event loop."""
        if self._entered:
            raise RuntimeError("SyncPortal is not reusable after exit")
        self._entered = True
        cm = start_blocking_portal(self._backend)
        self._portal = cm.__enter__()
        self._cm = cm
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the portal and join its thread."""
        cm, self._cm = self._cm, None
        self._portal = None
        if cm is not None:
            cm.__exit__(exc_type, exc, tb)

    def _require_portal(self) -> BlockingPortal:
        if self._portal is None:
            raise RuntimeError("SyncPortal is not running")
        return self._portal

    def call[**P, T](
        self,
        func: Callable[P, Awaitable[T]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run ``func(*args, **kwargs)`` on the portal's event loop.

        Single-member :class:`ExceptionGroup` wrappers are stripped so
        callers can catch concrete exception types.
        """
        portal = self._require_portal()
        bound: Callable[[], Awaitable[T]] = (
            partial(func, *args, **kwargs) if kwargs else partial(func, *args)
        )
        try:
            return portal.call(bound)
        except Exception as exc:
            unwrapped = _unwrap_single_group(exc)
            if unwrapped is exc:
                raise
            raise unwrapped from None

    def wrap_async_context_manager[T](
        self, acm: AbstractAsyncContextManager[T]
    ) -> AbstractContextManager[T]:
        """Present an async context manager as a sync context manager."""
        return self._require_portal().wrap_async_context_manager(acm)

    def wrap_async_iter[T](self, async_iter: AsyncIterator[T]) -> SyncAsyncIterator[T]:
        """Present an async iterator as a blocking, closeable iterator.

        The returned object is iterable (``for x in it: ...``) and
        supports :meth:`close` / context-manager use so outer wrappers
        can cancel the producer on early exit.
        """
        self._require_portal()
        return SyncAsyncIterator(self, async_iter)


class SyncAsyncIterator[T]:
    """Blocking view over an async iterator, bound to a :class:`SyncPortal`."""

    def __init__(self, portal: SyncPortal, async_iter: AsyncIterator[T]) -> None:
        self._portal = portal
        self._aiter = async_iter
        self._closed = False

    def __iter__(self) -> Iterator[T]:
        return self

    def __next__(self) -> T:
        if self._closed:
            raise StopIteration
        try:
            return self._portal.call(self._aiter.__anext__)
        except StopAsyncIteration:
            self._closed = True
            raise StopIteration from None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Cancel the underlying async iterator if it exposes ``aclose``.

        Safe to call more than once. Cleanup failures are swallowed —
        ``close`` runs on teardown paths where re-raising masks the
        real failure.
        """
        if self._closed:
            return
        self._closed = True
        if not self._portal.running:
            return
        aclose: Callable[[], Awaitable[Any]] | None = getattr(self._aiter, "aclose", None)
        if aclose is None:
            return
        with contextlib.suppress(Exception):
            self._portal.call(aclose)


def run_sync[**P, T](
    func: Callable[P, Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run one coroutine in a throwaway :class:`SyncPortal`.

    Suitable for short-lived operations — the discovery helpers, for
    example — where the portal thread's start/stop cost is acceptable.
    For repeated calls, reuse a long-lived :class:`SyncPortal`.
    """
    with SyncPortal() as portal:
        return portal.call(func, *args, **kwargs)
