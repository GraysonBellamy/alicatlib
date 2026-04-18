"""Device discovery — enumerate serial ports and identify Alicat devices.

Three entry points, each wider than the last:

- :func:`list_serial_ports` — thin wrapper over
  :func:`anyserial.list_serial_ports` returning device paths.
- :func:`probe` — open one port at one baudrate, run the full
  identification pipeline, return a :class:`DiscoveryResult`.
- :func:`find_devices` — run :func:`probe` over the cross-product of
  ``ports × unit_ids × baudrates``, bounded by
  :class:`anyio.CapacityLimiter`, returning every result (ok or errored).

Real fleets are mixed — baud rates vary, units aren't always at ``A``,
and a GP box sits next to a 10v05 one. :func:`find_devices` does not
raise on individual probe failure; every combination produces a
:class:`DiscoveryResult` and the caller decides what to do with the
errors. The library never prints — formatting a human-readable report
belongs to example scripts / CLIs, not the core (design §5.12).

Design reference: ``docs/design.md`` §5.12.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING, Final

import anyio
import anyserial

from alicatlib.devices.factory import identify_device
from alicatlib.errors import AlicatError
from alicatlib.protocol.client import AlicatProtocolClient
from alicatlib.transport.base import SerialSettings
from alicatlib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from alicatlib.devices.models import DeviceInfo

__all__ = [
    "DEFAULT_DISCOVERY_BAUDRATES",
    "DiscoveryResult",
    "find_devices",
    "list_serial_ports",
    "probe",
]


#: Default baud-rate sweep. ``19200`` is the Alicat factory default;
#: ``115200`` is the most common alternative after a ``NCB`` change.
#: Real-world fleets often mix the two, so trying both by default
#: means a misguessed baud doesn't leave a device invisible.
DEFAULT_DISCOVERY_BAUDRATES: Final[tuple[int, ...]] = (19200, 115200)

#: Default probe timeout — short, because discovery is speculative. A
#: missing device should fail fast so we move on to the next candidate.
#: Per-call override still available on :func:`probe` / :func:`find_devices`.
_DEFAULT_PROBE_TIMEOUT_S: Final[float] = 0.2

#: Default concurrency ceiling for :func:`find_devices` — 8 is well
#: below typical OS limits for open serial handles and keeps the cross-
#: product of a big sweep (10 ports × 2 baud × 5 unit ids = 100) from
#: saturating the system. Bounded by :class:`anyio.CapacityLimiter`.
_DEFAULT_MAX_CONCURRENCY: Final[int] = 8


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Outcome of a single :func:`probe` attempt.

    Exactly one of :attr:`info` / :attr:`error` is populated — ok results
    carry a fully-identified :class:`DeviceInfo`, failed ones carry the
    typed :class:`AlicatError` from the identification pipeline. The
    :attr:`ok` convenience lets callers filter without ``hasattr``.
    """

    port: str
    unit_id: str
    baudrate: int
    info: DeviceInfo | None
    error: AlicatError | None

    @property
    def ok(self) -> bool:
        """Whether identification completed successfully."""
        return self.error is None


async def list_serial_ports() -> list[str]:
    """Enumerate serial-port device paths visible to the OS.

    Thin wrapper over :func:`anyserial.list_serial_ports`. Returns
    device-path strings (``/dev/ttyUSB0``, ``COM3`` …) in whatever order
    the backend reports.

    The native backend does not require the ``anyserial[discovery-pyserial]``
    extra; platforms where it misses devices can install that extra and
    switch by setting the ``backend="pyserial"`` kwarg on
    :func:`anyserial.list_serial_ports` directly.
    """
    return [port.device for port in await anyserial.list_serial_ports()]


async def _probe_with_client(
    client: AlicatProtocolClient,
    *,
    port: str,
    unit_id: str,
    baudrate: int,
) -> DiscoveryResult:
    """Identify using a pre-wired client, catching every :class:`AlicatError`.

    Extracted from :func:`probe` so tests can drive the identification
    path with :class:`FakeTransport` — :class:`SerialTransport` doesn't
    test-inject cleanly, and the identification logic is the interesting
    part of probing.
    """
    try:
        info = await identify_device(client, unit_id)
    except AlicatError as err:
        return DiscoveryResult(
            port=port,
            unit_id=unit_id,
            baudrate=baudrate,
            info=None,
            error=err,
        )
    return DiscoveryResult(
        port=port,
        unit_id=unit_id,
        baudrate=baudrate,
        info=info,
        error=None,
    )


async def probe(
    port: str,
    *,
    unit_id: str = "A",
    baudrate: int = 19200,
    timeout: float = _DEFAULT_PROBE_TIMEOUT_S,
) -> DiscoveryResult:
    """Probe one port at one baudrate for one unit id.

    Never raises — every failure becomes :attr:`DiscoveryResult.error`
    so that a bulk :func:`find_devices` call collects a uniform result
    set. Opening errors (permission denied, port busy, no such device)
    are caught here the same as identification errors; the caller sees
    one shape whether the device is offline, misconfigured, or silent.
    """
    settings = SerialSettings(port=port, baudrate=baudrate)
    transport = SerialTransport(settings)
    try:
        await transport.open()
    except AlicatError as err:
        return DiscoveryResult(
            port=port,
            unit_id=unit_id,
            baudrate=baudrate,
            info=None,
            error=err,
        )
    try:
        client = AlicatProtocolClient(
            transport,
            default_timeout=timeout,
            # Multiline (``??M*``) deserves a bit more headroom — the
            # factory-default ratio of 2x matches the protocol client
            # itself.
            multiline_timeout=timeout * 2,
        )
        return await _probe_with_client(
            client,
            port=port,
            unit_id=unit_id,
            baudrate=baudrate,
        )
    finally:
        # Best-effort teardown — a close failure here shouldn't hide
        # the identification result the caller came for.
        with contextlib.suppress(AlicatError):
            await transport.close()


async def find_devices(
    ports: Iterable[str] | None = None,
    *,
    unit_ids: Sequence[str] = ("A",),
    baudrates: Sequence[int] = DEFAULT_DISCOVERY_BAUDRATES,
    timeout: float = _DEFAULT_PROBE_TIMEOUT_S,
    max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
) -> tuple[DiscoveryResult, ...]:
    """Probe the cross-product ``ports × unit_ids × baudrates`` concurrently.

    When ``ports`` is ``None`` the sweep enumerates every port visible
    via :func:`list_serial_ports` — convenient for "what's plugged in?"
    but note that a large fleet plus multiple baudrates multiplies out
    quickly (10 ports × 2 baud × 5 unit ids = 100 probes).

    Concurrency is bounded by ``max_concurrency`` via
    :class:`anyio.CapacityLimiter`; at most that many serial handles
    are ever open simultaneously. The function never raises — every
    probe's result lands in the returned tuple, ``ok`` or not, in a
    stable order (``ports`` × ``unit_ids`` × ``baudrates``, row-major).
    """
    if ports is None:
        ports = await list_serial_ports()
    port_list = list(ports)

    combinations = list(product(port_list, unit_ids, baudrates))
    results: list[DiscoveryResult | None] = [None] * len(combinations)
    limiter = anyio.CapacityLimiter(max_concurrency)

    async def _run(index: int, port: str, unit_id: str, baudrate: int) -> None:
        async with limiter:
            results[index] = await probe(
                port,
                unit_id=unit_id,
                baudrate=baudrate,
                timeout=timeout,
            )

    async with anyio.create_task_group() as tg:
        for index, (port, unit_id, baudrate) in enumerate(combinations):
            tg.start_soon(_run, index, port, unit_id, baudrate)

    # Every slot is populated — the task group only exits after every
    # spawned task returns — but mypy can't prove that, hence the cast.
    return tuple(r for r in results if r is not None)
