"""Sync wrappers for :mod:`alicatlib.devices.discovery`.

One-shot discovery primitives — each call creates, drives, and tears
down its own :class:`SyncPortal` unless the caller passes one in. That
matches the async helpers' "fire and forget" shape (open a port, probe,
close).

Design reference: ``docs/design.md`` §5.12 and §5.16.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alicatlib.devices.discovery import (
    DEFAULT_DISCOVERY_BAUDRATES,
    DiscoveryResult,
)
from alicatlib.devices.discovery import (
    find_devices as async_find_devices,
)
from alicatlib.devices.discovery import (
    list_serial_ports as async_list_serial_ports,
)
from alicatlib.devices.discovery import (
    probe as async_probe,
)
from alicatlib.sync.portal import SyncPortal, run_sync

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "DEFAULT_DISCOVERY_BAUDRATES",
    "DiscoveryResult",
    "find_devices",
    "list_serial_ports",
    "probe",
]


def list_serial_ports(*, portal: SyncPortal | None = None) -> list[str]:
    """Blocking :func:`alicatlib.devices.discovery.list_serial_ports`."""
    if portal is not None:
        return portal.call(async_list_serial_ports)
    return run_sync(async_list_serial_ports)


def probe(
    port: str,
    *,
    unit_id: str = "A",
    baudrate: int = 19200,
    timeout: float = 0.2,
    portal: SyncPortal | None = None,
) -> DiscoveryResult:
    """Blocking :func:`alicatlib.devices.discovery.probe`."""
    if portal is not None:
        return portal.call(
            async_probe,
            port,
            unit_id=unit_id,
            baudrate=baudrate,
            timeout=timeout,
        )
    with SyncPortal() as owned:
        return owned.call(
            async_probe,
            port,
            unit_id=unit_id,
            baudrate=baudrate,
            timeout=timeout,
        )


def find_devices(
    ports: Iterable[str] | None = None,
    *,
    unit_ids: Sequence[str] = ("A",),
    baudrates: Sequence[int] = DEFAULT_DISCOVERY_BAUDRATES,
    timeout: float = 0.2,
    max_concurrency: int = 8,
    stop_on_first_hit: bool = False,
    portal: SyncPortal | None = None,
) -> tuple[DiscoveryResult, ...]:
    """Blocking :func:`alicatlib.devices.discovery.find_devices`."""
    port_list = None if ports is None else list(ports)
    if portal is not None:
        return portal.call(
            async_find_devices,
            port_list,
            unit_ids=unit_ids,
            baudrates=baudrates,
            timeout=timeout,
            max_concurrency=max_concurrency,
            stop_on_first_hit=stop_on_first_hit,
        )
    with SyncPortal() as owned:
        return owned.call(
            async_find_devices,
            port_list,
            unit_ids=unit_ids,
            baudrates=baudrates,
            timeout=timeout,
            max_concurrency=max_concurrency,
            stop_on_first_hit=stop_on_first_hit,
        )
