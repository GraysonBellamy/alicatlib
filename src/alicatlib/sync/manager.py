"""Sync manager facade — portal-driven wrapper over :class:`AlicatManager`.

:class:`SyncAlicatManager` wraps the async
:class:`~alicatlib.manager.AlicatManager` through a
:class:`~alicatlib.sync.portal.SyncPortal`. Every coroutine method on
the async manager becomes a blocking method here; the synchronous
:meth:`AlicatManager.get` stays synchronous and delegates directly.

Lifecycle mirrors the async side: the class is a ``with`` context
manager. By default each instance owns its own portal; callers that
need several facades to share one event loop can pass ``portal=`` to
reuse a long-lived :class:`SyncPortal`.

Design reference: ``docs/design.md`` §5.13 and §5.16.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Self

from alicatlib.manager import AlicatManager, DeviceResult, ErrorPolicy
from alicatlib.sync.device import SyncDevice, unwrap_sync_device, wrap_device
from alicatlib.sync.portal import SyncPortal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType

    from alicatlib.commands import Command
    from alicatlib.devices.base import Device
    from alicatlib.devices.data_frame import DataFrame
    from alicatlib.devices.models import MeasurementSet
    from alicatlib.protocol import AlicatProtocolClient
    from alicatlib.registry import Statistic
    from alicatlib.transport.base import SerialSettings, Transport

__all__ = [
    "DeviceResult",
    "ErrorPolicy",
    "SyncAlicatManager",
]


class SyncAlicatManager:
    """Blocking facade over :class:`alicatlib.manager.AlicatManager`.

    Example:
        >>> with SyncAlicatManager() as mgr:  # doctest: +SKIP
        ...     mgr.add("fuel", "/dev/ttyUSB0")
        ...     mgr.add("air", "/dev/ttyUSB1")
        ...     frames = mgr.poll()

    Args:
        error_policy: Forwarded to :class:`AlicatManager`.
        portal: Optional pre-built :class:`SyncPortal` to share an
            event-loop thread with other sync facades. Default is a
            per-instance portal created on ``__enter__``.
    """

    def __init__(
        self,
        *,
        error_policy: ErrorPolicy = ErrorPolicy.RAISE,
        portal: SyncPortal | None = None,
    ) -> None:
        self._error_policy = error_policy
        self._portal_override = portal
        self._stack: ExitStack | None = None
        self._portal: SyncPortal | None = None
        self._mgr: AlicatManager | None = None
        self._wrapped: dict[str, SyncDevice] = {}
        self._entered = False

    # --------------------------------------------------------------- properties

    @property
    def error_policy(self) -> ErrorPolicy:
        """The :class:`ErrorPolicy` this manager was constructed with."""
        return self._error_policy

    @property
    def names(self) -> tuple[str, ...]:
        """Insertion-ordered tuple of managed device names."""
        mgr = self._mgr
        if mgr is None:
            return ()
        return mgr.names

    @property
    def closed(self) -> bool:
        """``True`` once :meth:`close` or ``__exit__`` has run."""
        mgr = self._mgr
        return mgr is None or mgr.closed

    @property
    def portal(self) -> SyncPortal:
        """The :class:`SyncPortal` this manager's coroutines run on."""
        portal = self._portal
        if portal is None:
            raise RuntimeError("SyncAlicatManager is not open")
        return portal

    # --------------------------------------------------------------- lifecycle

    def __enter__(self) -> Self:
        """Start the portal, build the async manager, enter its CM."""
        if self._entered:
            raise RuntimeError("SyncAlicatManager is not reusable after exit")
        self._entered = True
        stack = ExitStack()
        try:
            portal = (
                self._portal_override
                if self._portal_override is not None
                else stack.enter_context(SyncPortal())
            )
            mgr = AlicatManager(error_policy=self._error_policy)
            stack.enter_context(portal.wrap_async_context_manager(mgr))
            self._portal = portal
            self._mgr = mgr
            self._stack = stack
        except BaseException:
            stack.close()
            self._portal = None
            self._mgr = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the managed devices + portal (if owned)."""
        stack, self._stack = self._stack, None
        self._wrapped.clear()
        self._mgr = None
        self._portal = None
        if stack is not None:
            stack.__exit__(exc_type, exc, tb)

    # --------------------------------------------------------------- add/remove

    def add(
        self,
        name: str,
        source: SyncDevice | Device | str | Transport | AlicatProtocolClient,
        *,
        unit_id: str = "A",
        serial: SerialSettings | None = None,
        timeout: float = 0.5,
    ) -> SyncDevice:
        """Blocking :meth:`AlicatManager.add`.

        Accepts a :class:`SyncDevice` as ``source`` in addition to the
        async shapes — the wrapper is unwrapped to the underlying
        :class:`Device` before delegation, matching the manager's
        "pre-built device, no lifecycle ownership" contract.
        """
        mgr = self._require_mgr()
        async_source: Device | str | Transport | AlicatProtocolClient = unwrap_sync_device(source)
        async_device = self.portal.call(
            mgr.add,
            name,
            async_source,
            unit_id=unit_id,
            serial=serial,
            timeout=timeout,
        )
        wrapped = wrap_device(async_device, self.portal)
        self._wrapped[name] = wrapped
        return wrapped

    def remove(self, name: str) -> None:
        """Blocking :meth:`AlicatManager.remove`."""
        mgr = self._require_mgr()
        self._wrapped.pop(name, None)
        self.portal.call(mgr.remove, name)

    def get(self, name: str) -> SyncDevice:
        """Return the sync wrapper for the device registered under ``name``.

        The async manager's :meth:`AlicatManager.get` is already
        synchronous — this method caches the sync wrapper so repeated
        ``get`` calls return the same :class:`SyncDevice` instance per
        device name.
        """
        cached = self._wrapped.get(name)
        if cached is not None:
            return cached
        mgr = self._require_mgr()
        async_device = mgr.get(name)  # raises AlicatValidationError on unknown
        wrapped = wrap_device(async_device, self.portal)
        self._wrapped[name] = wrapped
        return wrapped

    def close(self) -> None:
        """Blocking :meth:`AlicatManager.close` — idempotent."""
        self._wrapped.clear()
        mgr = self._mgr
        if mgr is None:
            return
        portal = self._portal
        if portal is None:
            return
        portal.call(mgr.close)

    # --------------------------------------------------------------- concurrent I/O

    def poll(
        self,
        names: Sequence[str] | None = None,
    ) -> Mapping[str, DeviceResult[DataFrame]]:
        """Blocking :meth:`AlicatManager.poll`."""
        mgr = self._require_mgr()
        return self.portal.call(mgr.poll, names)

    def request(
        self,
        statistics: Sequence[Statistic | str],
        names: Sequence[str] | None = None,
        *,
        averaging_ms: int = 1,
    ) -> Mapping[str, DeviceResult[MeasurementSet]]:
        """Blocking :meth:`AlicatManager.request`."""
        mgr = self._require_mgr()
        return self.portal.call(
            mgr.request,
            statistics,
            names,
            averaging_ms=averaging_ms,
        )

    def execute[Req, Resp](
        self,
        command: Command[Req, Resp],
        requests_by_name: Mapping[str, Req],
    ) -> Mapping[str, DeviceResult[Resp]]:
        """Blocking :meth:`AlicatManager.execute`."""
        mgr = self._require_mgr()
        return self.portal.call(mgr.execute, command, requests_by_name)

    # --------------------------------------------------------------- internals

    def _require_mgr(self) -> AlicatManager:
        mgr = self._mgr
        if mgr is None:
            raise RuntimeError("SyncAlicatManager is not open")
        return mgr
