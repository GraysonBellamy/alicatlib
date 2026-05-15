"""Sample acquisition — ``record()`` emits typed ``Sample`` streams.

Public surface:

- :class:`Sample` — one device poll with full timing provenance.
- :func:`record` — absolute-cadence async context manager.
- :class:`Recording` — wrapper carrying the stream + live summary + rate.
- :class:`OverflowPolicy` — backpressure control knob.
- :class:`AcquisitionSummary` — per-run counters (mutable, updated in
  place by the recorder).
- :class:`PollSource` — Protocol the recorder accepts (satisfied by
  :class:`~alicatlib.manager.AlicatManager`).
- :class:`PollSourceAdapter` — single-device adapter so a bare
  :class:`~alicatlib.devices.base.Device` can drive :func:`record`.

See ``docs/design.md`` §5.14.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alicatlib.errors import AlicatError
from alicatlib.manager import DeviceResult
from alicatlib.streaming.recorder import (
    AcquisitionSummary,
    OverflowPolicy,
    PollSource,
    Recording,
    record,
)
from alicatlib.streaming.sample import Sample

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from alicatlib.devices.base import Device
    from alicatlib.devices.reading import Reading

__all__ = [
    "AcquisitionSummary",
    "OverflowPolicy",
    "PollSource",
    "PollSourceAdapter",
    "Recording",
    "Sample",
    "record",
]


class PollSourceAdapter:
    """Wrap one :class:`Device` as a :class:`PollSource` for :func:`record`.

    Capa's old ``_SingleDevicePollSource`` shim reinvented this; the
    adapter lives here so the wiring is one line at the call site::

        adapter = PollSourceAdapter("fuel", device)
        async with record(adapter, rate_hz=10) as recording:
            ...

    The ``names`` filter is honoured per the cross-lib spec §E: when
    the caller passes a name set that does not include this device's
    name, ``poll()`` returns an empty mapping rather than polling
    anyway. The recorder always passes a complete name set in
    single-device mode so filtering is harmless; the empty-mapping
    behaviour is the correct cross-lib semantic.
    """

    def __init__(self, name: str, device: Device) -> None:
        self._name = name
        self._device = device

    @property
    def name(self) -> str:
        """The manager-style name this adapter publishes the device under."""
        return self._name

    @property
    def device(self) -> Device:
        """The wrapped async :class:`Device`."""
        return self._device

    async def poll(
        self,
        names: Iterable[str] | None = None,
    ) -> Mapping[str, DeviceResult[Reading]]:
        """Poll the wrapped device and return a single-entry mapping."""
        if names is not None and self._name not in set(names):
            return {}
        try:
            reading = await self._device.poll()
        except AlicatError as err:
            failure: DeviceResult[Reading] = DeviceResult(value=None, error=err)
            return {self._name: failure}
        return {self._name: DeviceResult.success(reading)}
