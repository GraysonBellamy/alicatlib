"""Multi-device orchestrator — :class:`AlicatManager`.

The manager coordinates many :class:`~alicatlib.devices.base.Device`
instances across one or more serial ports. Operations across different
physical ports run concurrently through
:func:`anyio.create_task_group`; operations against the same port
serialise through that port's shared
:class:`~alicatlib.protocol.client.AlicatProtocolClient` lock.

Port identity is **canonicalised** before comparison so a device
referenced via both ``/dev/ttyUSB0`` and
``/dev/serial/by-id/usb-FTDI-…`` (or ``COM3`` and ``com3`` on
Windows) collapses to one client — critical for the single-in-flight
invariant. Pre-built :class:`Transport` / :class:`AlicatProtocolClient`
sources use the object's :func:`id` as the key so caller-owned
transports aren't accidentally shared.

Error handling is controlled by :class:`ErrorPolicy`:

- :attr:`ErrorPolicy.RAISE` — manager collects all results, and if
  any device failed, raises an :class:`ExceptionGroup` at the end
  (never silently drops results).
- :attr:`ErrorPolicy.RETURN` — every device produces a
  :class:`DeviceResult` container; callers inspect ``.error``.

Resource lifecycle goes through an internal tracking structure that
unwinds LIFO on :meth:`close` or ``__aexit__``. Per-port clients are
ref-counted so the last :meth:`remove` on a shared port triggers the
client's close.

Design reference: ``docs/design.md`` §5.13.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast

import anyio

from alicatlib._logging import get_logger
from alicatlib.devices.base import Device
from alicatlib.devices.factory import open_device
from alicatlib.errors import (
    AlicatError,
    AlicatValidationError,
    ErrorContext,
)
from alicatlib.protocol.client import AlicatProtocolClient
from alicatlib.transport.base import SerialSettings
from alicatlib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager
    from types import TracebackType
    from typing import Self

    from alicatlib.commands import Command
    from alicatlib.devices.data_frame import DataFrame
    from alicatlib.devices.models import MeasurementSet
    from alicatlib.registry import Statistic
    from alicatlib.transport.base import Transport

__all__ = [
    "AlicatManager",
    "DeviceResult",
    "ErrorPolicy",
]


_logger = get_logger("manager")


class ErrorPolicy(Enum):
    """How the manager surfaces per-device failures.

    Under :attr:`RAISE`, the manager collects every device's result
    and — if any call failed — raises an :class:`ExceptionGroup`
    containing the per-device exceptions after the task group joins.
    Under :attr:`RETURN`, each device produces a :class:`DeviceResult`
    and the caller inspects ``.error`` per entry.

    Design reference: ``docs/design.md`` §5.13.
    """

    RAISE = "raise"
    RETURN = "return"


@dataclass(frozen=True, slots=True)
class DeviceResult[T]:
    """Per-device result container — value **or** error, never both.

    The union is encoded as two optional fields (rather than an
    ``Either`` / ``Result`` ADT) so mypy's narrowing on ``ok`` reads
    cleanly at call sites without pattern matching.

    Attributes:
        value: The successful result, or ``None`` if the call failed.
        error: The captured :class:`~alicatlib.errors.AlicatError`, or
            ``None`` if the call succeeded.
    """

    value: T | None
    error: AlicatError | None

    @property
    def ok(self) -> bool:
        """``True`` when the device produced a value (``error is None``)."""
        return self.error is None


# ---------------------------------------------------------------------------
# Port canonicalization
# ---------------------------------------------------------------------------


_WINDOWS_DEVICE_PREFIX = "\\\\.\\"


def _canonical_port_key(port: str) -> str:
    r"""Collapse equivalent port names to a single key.

    POSIX: follows symlinks via :func:`os.path.realpath` so
    ``/dev/ttyUSB0`` and ``/dev/serial/by-id/...-if00-port0`` (the
    FTDI-style symlink that survives reboots) resolve to the same
    physical device. Falls back to the raw string if the path doesn't
    exist (useful under test fixtures).

    Windows: strips the ``\\.\`` device-namespace prefix and
    uppercases, so ``COM3`` / ``com3`` / ``\\.\COM3`` all match.

    Not used for pre-built :class:`~alicatlib.transport.base.Transport`
    or :class:`~alicatlib.protocol.client.AlicatProtocolClient`
    sources — those use :func:`id` as the key (the caller has already
    expressed ownership).
    """
    if sys.platform == "win32":
        return port.removeprefix(_WINDOWS_DEVICE_PREFIX).upper()
    return os.path.realpath(port) if Path(port).exists() else port


# ---------------------------------------------------------------------------
# Internal tracking structures
# ---------------------------------------------------------------------------


def _empty_refs() -> set[str]:
    return set()


@dataclass(slots=True)
class _PortEntry:
    """Ref-counted per-port resources shared across devices on the bus."""

    key: str
    client: AlicatProtocolClient
    transport: Transport | None
    owns_transport: bool
    refs: set[str] = field(default_factory=_empty_refs)


@dataclass(slots=True)
class _DeviceEntry:
    """One managed :class:`Device` + its lifecycle reference."""

    name: str
    device: Device
    port_key: str | None
    # The ``open_device(...)`` context manager we're holding open for
    # this device. ``None`` when the caller handed us a pre-built
    # :class:`Device` and explicitly kept lifecycle ownership.
    device_ctx: AbstractAsyncContextManager[Device] | None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class AlicatManager:
    """Coordinator for many devices across one or more serial ports.

    Operations run concurrently across different physical ports (via
    :func:`anyio.create_task_group`) and serialise on the same-port
    client lock. Per-device failures are surfaced per
    :attr:`error_policy`:

    - :attr:`ErrorPolicy.RAISE`: the manager still collects results
      from every device, then raises an :class:`ExceptionGroup` if
      any failed.
    - :attr:`ErrorPolicy.RETURN`: the mapping's values carry
      :class:`DeviceResult` containers with ``.value`` or ``.error``.

    Usage::

        async with AlicatManager() as mgr:
            await mgr.add("fuel", "/dev/ttyUSB0")
            await mgr.add("air", "/dev/ttyUSB1")
            frames = await mgr.poll()
    """

    def __init__(self, *, error_policy: ErrorPolicy = ErrorPolicy.RAISE) -> None:
        self._error_policy = error_policy
        self._devices: dict[str, _DeviceEntry] = {}
        self._ports: dict[str, _PortEntry] = {}
        # Guards state mutation on ``add`` / ``remove`` / ``close``.
        # The per-port client lock serialises I/O, so we only need
        # to serialise the manager's bookkeeping here.
        self._state_lock = anyio.Lock()
        self._closed = False

    # ------------------------------------------------------------------ props

    @property
    def error_policy(self) -> ErrorPolicy:
        """The :class:`ErrorPolicy` this manager was constructed with."""
        return self._error_policy

    @property
    def names(self) -> tuple[str, ...]:
        """Insertion-ordered tuple of managed device names."""
        return tuple(self._devices.keys())

    @property
    def closed(self) -> bool:
        """``True`` once :meth:`close` has been called."""
        return self._closed

    # ----------------------------------------------------------- context manager

    async def __aenter__(self) -> Self:
        """Enter the async context — returns ``self`` for chaining."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close every managed device + port on exit."""
        del exc_type, exc, tb
        await self.close()

    # ----------------------------------------------------------------- add/remove

    async def add(
        self,
        name: str,
        source: Device | str | Transport | AlicatProtocolClient,
        *,
        unit_id: str = "A",
        serial: SerialSettings | None = None,
        timeout: float = 0.5,
    ) -> Device:
        """Register and open a device under ``name``.

        The ``source`` discriminates lifecycle ownership:

        - ``Device`` — pre-built (via :func:`open_device` outside the
          manager). The manager only tracks the name mapping; it does
          *not* take lifecycle ownership.
        - ``str`` — serial port path (``"/dev/ttyUSB0"``, ``"COM3"``).
          The manager creates a
          :class:`~alicatlib.transport.serial.SerialTransport` and
          :class:`AlicatProtocolClient`, canonicalises the port key,
          and reuses them across multi-device buses (RS-485).
        - :class:`Transport` — duck-typed transport. The manager wraps
          it in a new client but does *not* take transport ownership
          (the caller keeps open/close responsibility).
        - :class:`AlicatProtocolClient` — use as-is; the manager does
          not close it.

        Args:
            name: Unique manager-level identifier. Must not already
                exist on this manager.
            source: One of the four lifecycle shapes above.
            unit_id: Bus-level letter for the device. ``"A"`` is the
                polling default; multiple devices on the same port
                get distinct unit ids.
            serial: :class:`SerialSettings` override. Only honoured
                when ``source`` is a port-string — ignored otherwise
                (pre-built transports carry their own settings).
            timeout: Default command timeout passed through to
                :func:`open_device`.

        Returns:
            The identified :class:`Device` (a :class:`FlowMeter`,
            :class:`FlowController`, etc. subclass).

        Raises:
            AlicatValidationError: ``name`` already exists or
                ``serial`` was supplied with a non-string source.
            AlicatConnectionError: The manager is closed.
        """
        async with self._state_lock:
            self._check_open()
            if name in self._devices:
                raise AlicatValidationError(
                    f"manager: name {name!r} already in use",
                    context=ErrorContext(extra={"name": name}),
                )
            if serial is not None and not isinstance(source, str):
                raise AlicatValidationError(
                    "manager.add(serial=...) only applies to string port sources; "
                    "pre-built Transport / AlicatProtocolClient carry their own settings",
                    context=ErrorContext(extra={"name": name}),
                )

            port_key, port_entry, device_ctx = await self._resolve_source(
                source,
                unit_id=unit_id,
                serial=serial,
                timeout=timeout,
            )

            # ``open_device`` context-enter runs identification + probes.
            # If it raises, we must not leave the port's ref count dangling.
            try:
                if device_ctx is not None:
                    device = await device_ctx.__aenter__()
                else:
                    # ``source`` was a pre-built Device.
                    assert isinstance(source, Device)  # noqa: S101 — narrow for mypy
                    device = source
            except BaseException:
                if port_entry is not None and port_key is not None and name not in port_entry.refs:
                    # We created a brand-new port just for this add —
                    # unwind it rather than leaking the transport.
                    await self._maybe_teardown_port(port_key, port_entry)
                raise

            self._devices[name] = _DeviceEntry(
                name=name,
                device=device,
                port_key=port_key,
                device_ctx=device_ctx,
            )
            if port_entry is not None:
                port_entry.refs.add(name)

            _logger.info(
                "manager.add",
                extra={
                    "device_name": name,
                    "port_key": port_key,
                    "unit_id": unit_id,
                    "model": device.info.model,
                    "firmware": str(device.info.firmware),
                },
            )
            return device

    async def remove(self, name: str) -> None:
        """Unregister and close the device named ``name``.

        If ``name`` was the last device on a shared port, the
        transport and client for that port are closed too. A
        pre-built :class:`Device` source is only dropped from the
        manager's registry — the caller retains lifecycle ownership.
        """
        async with self._state_lock:
            self._check_open()
            if name not in self._devices:
                raise AlicatValidationError(
                    f"manager: no device named {name!r}",
                    context=ErrorContext(extra={"name": name}),
                )
            entry = self._devices.pop(name)
            await self._teardown_device(entry)
            _logger.info("manager.remove", extra={"device_name": name})

    def get(self, name: str) -> Device:
        """Return the device registered under ``name`` (raises if unknown)."""
        try:
            return self._devices[name].device
        except KeyError:
            raise AlicatValidationError(
                f"manager: no device named {name!r}",
                context=ErrorContext(extra={"name": name}),
            ) from None

    async def close(self) -> None:
        """Tear down every managed device and port (LIFO).

        Idempotent: safe to call from both :meth:`__aexit__` and
        explicit user code. Individual close failures are caught and
        logged so one device's shutdown error doesn't strand the
        others.
        """
        async with self._state_lock:
            if self._closed:
                return
            # Unwind in reverse insertion order — LIFO per design §5.13.
            for name in reversed(list(self._devices.keys())):
                entry = self._devices.pop(name)
                try:
                    await self._teardown_device(entry)
                except Exception as err:
                    _logger.warning(
                        "manager.close_device_failed",
                        extra={"device_name": name, "error": repr(err)},
                    )
            # Any port entries that survived (e.g. because a pre-built
            # client source never got refs torn down) are left alone —
            # the caller owns them.
            self._closed = True

    # --------------------------------------------------------------- concurrent I/O

    async def poll(
        self,
        names: Sequence[str] | None = None,
    ) -> Mapping[str, DeviceResult[DataFrame]]:
        """Poll every (or named) device concurrently across ports.

        Returns a mapping from device name to :class:`DeviceResult`
        even under :attr:`ErrorPolicy.RAISE` — but under that policy,
        any failed device's error is re-raised as an
        :class:`ExceptionGroup` after all devices have completed.
        """
        targets = self._resolve_names(names)

        async def _poll(device: Device) -> DataFrame:
            return await device.poll()

        return await self._dispatch("poll", targets, _poll)

    async def request(
        self,
        statistics: Sequence[Statistic | str],
        names: Sequence[str] | None = None,
        *,
        averaging_ms: int = 1,
    ) -> Mapping[str, DeviceResult[MeasurementSet]]:
        """Run :meth:`Device.request` across devices concurrently.

        Every targeted device receives the same statistic list and
        averaging window — mirroring the primer's ``DV`` semantics.
        """
        targets = self._resolve_names(names)

        async def _request(device: Device) -> MeasurementSet:
            return await device.request(statistics, averaging_ms=averaging_ms)

        return await self._dispatch("request", targets, _request)

    async def execute[Req, Resp](
        self,
        command: Command[Req, Resp],
        requests_by_name: Mapping[str, Req],
    ) -> Mapping[str, DeviceResult[Resp]]:
        """Dispatch a per-device ``Command`` across the requested names.

        ``requests_by_name`` chooses both which devices participate and
        what arguments each gets — supporting the common case of
        "same command, different setpoint per device".
        """
        for name in requests_by_name:
            if name not in self._devices:
                raise AlicatValidationError(
                    f"manager.execute: no device named {name!r}",
                    context=ErrorContext(command_name=command.name, extra={"name": name}),
                )
        targets = tuple(requests_by_name.keys())
        name_by_device_id = {id(entry.device): entry.name for entry in self._devices.values()}

        async def _execute(device: Device) -> Resp:
            return await device.session.execute(
                command,
                requests_by_name[name_by_device_id[id(device)]],
            )

        return await self._dispatch(command.name, targets, _execute)

    # ---------------------------------------------------------------- internals

    def _check_open(self) -> None:
        if self._closed:
            raise AlicatValidationError(
                "manager is closed",
                context=ErrorContext(extra={"closed": True}),
            )

    def _resolve_names(self, names: Sequence[str] | None) -> tuple[str, ...]:
        """Return the target device names, validating any user-provided list."""
        if names is None:
            return tuple(self._devices.keys())
        targets = tuple(names)
        unknown = [n for n in targets if n not in self._devices]
        if unknown:
            raise AlicatValidationError(
                f"manager: unknown device name(s) {sorted(unknown)!r}",
                context=ErrorContext(extra={"unknown": sorted(unknown)}),
            )
        return targets

    async def _resolve_source(
        self,
        source: Device | str | Transport | AlicatProtocolClient,
        *,
        unit_id: str,
        serial: SerialSettings | None,
        timeout: float,
    ) -> tuple[
        str | None,
        _PortEntry | None,
        AbstractAsyncContextManager[Device] | None,
    ]:
        """Map ``source`` to ``(port_key, port_entry, device_ctx)``.

        - Pre-built :class:`Device`: everything is ``None`` — manager
          holds no lifecycle resources.
        - ``str``: canonicalise the path and share or create a
          :class:`_PortEntry`.
        - :class:`Transport`: key by ``id``; no sharing.
        - :class:`AlicatProtocolClient`: key by ``id``; no sharing;
          transport stays the caller's responsibility.
        """
        if isinstance(source, Device):
            return None, None, None
        if isinstance(source, str):
            port_key = _canonical_port_key(source)
            port_entry = self._ports.get(port_key)
            if port_entry is None:
                settings = serial if serial is not None else SerialSettings(port=source)
                transport = SerialTransport(settings)
                await transport.open()
                client = AlicatProtocolClient(transport, default_timeout=timeout)
                port_entry = _PortEntry(
                    key=port_key,
                    client=client,
                    transport=transport,
                    owns_transport=True,
                )
                self._ports[port_key] = port_entry
            device_ctx = cast(
                "AbstractAsyncContextManager[Device]",
                open_device(port_entry.client, unit_id=unit_id, timeout=timeout),
            )
            return port_key, port_entry, device_ctx
        if isinstance(source, AlicatProtocolClient):
            port_key = f"client:{id(source)}"
            port_entry = self._ports.get(port_key)
            if port_entry is None:
                port_entry = _PortEntry(
                    key=port_key,
                    client=source,
                    transport=None,
                    owns_transport=False,
                )
                self._ports[port_key] = port_entry
            device_ctx = cast(
                "AbstractAsyncContextManager[Device]",
                open_device(port_entry.client, unit_id=unit_id, timeout=timeout),
            )
            return port_key, port_entry, device_ctx
        # Duck-typed Transport (Protocol isn't runtime-checkable).
        port_key = f"transport:{id(source)}"
        port_entry = self._ports.get(port_key)
        if port_entry is None:
            client = AlicatProtocolClient(source, default_timeout=timeout)
            port_entry = _PortEntry(
                key=port_key,
                client=client,
                transport=source,
                owns_transport=False,
            )
            self._ports[port_key] = port_entry
        device_ctx = cast(
            "AbstractAsyncContextManager[Device]",
            open_device(port_entry.client, unit_id=unit_id, timeout=timeout),
        )
        return port_key, port_entry, device_ctx

    async def _teardown_device(self, entry: _DeviceEntry) -> None:
        """Exit ``open_device`` for one device and maybe tear down its port."""
        if entry.device_ctx is not None:
            await entry.device_ctx.__aexit__(None, None, None)
        if entry.port_key is None:
            return
        port_entry = self._ports.get(entry.port_key)
        if port_entry is None:
            return
        port_entry.refs.discard(entry.name)
        if not port_entry.refs:
            await self._maybe_teardown_port(entry.port_key, port_entry)

    async def _maybe_teardown_port(self, port_key: str, port_entry: _PortEntry) -> None:
        """Close transport + drop registry entry for a port with no more refs."""
        if port_entry.owns_transport and port_entry.transport is not None:
            try:
                if port_entry.transport.is_open:
                    await port_entry.transport.close()
            except Exception as err:
                _logger.warning(
                    "manager.close_port_failed",
                    extra={"port_key": port_key, "error": repr(err)},
                )
        self._ports.pop(port_key, None)

    async def _dispatch[T](
        self,
        label: str,
        names: Sequence[str],
        op: Callable[[Device], Awaitable[T]],
    ) -> Mapping[str, DeviceResult[T]]:
        """Run ``op(device)`` across ``names`` with port-aware concurrency.

        Implements the §5.13 concurrency rule:

        - Devices grouped by ``port_key`` run their tasks sequentially
          within the group (the shared client lock already serialises
          I/O; explicit sequencing keeps the dispatch model readable).
        - Different ``port_key`` groups run concurrently in a single
          :func:`anyio.create_task_group`.

        Under :attr:`ErrorPolicy.RAISE`, collected failures are raised
        as an :class:`ExceptionGroup` after every device has finished.
        """
        results: dict[str, DeviceResult[T]] = {}
        errors: list[AlicatError] = []
        groups: dict[str, list[str]] = {}
        for n in names:
            entry = self._devices[n]
            port_key = entry.port_key if entry.port_key is not None else f"solo:{n}"
            groups.setdefault(port_key, []).append(n)

        async def _run_group(member_names: list[str]) -> None:
            for member in member_names:
                device = self._devices[member].device
                try:
                    value: T = await op(device)
                except AlicatError as err:
                    results[member] = DeviceResult(value=None, error=err)
                    errors.append(err)
                else:
                    results[member] = DeviceResult(value=value, error=None)

        async with anyio.create_task_group() as tg:
            for member_names in groups.values():
                tg.start_soon(_run_group, member_names)

        if self._error_policy is ErrorPolicy.RAISE and errors:
            raise ExceptionGroup(f"manager.{label}: one or more devices failed", errors)
        return results
