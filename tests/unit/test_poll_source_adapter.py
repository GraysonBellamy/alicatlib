"""Tests for :class:`alicatlib.streaming.PollSourceAdapter`.

The adapter is the single-device bridge between a bare
:class:`alicatlib.devices.base.Device` and the recorder's
:class:`PollSource` Protocol — the cross-lib spec §E shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from alicatlib import DeviceResult, PollSourceAdapter
from alicatlib.errors import AlicatError, AlicatTimeoutError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from alicatlib.devices.reading import Reading


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class _FakeReading:
    """Stand-in for :class:`Reading` so we don't have to mint a real one."""

    unit_id = "A"


def _fake_reading() -> Reading:
    return cast("Reading", _FakeReading())


class _StubDevice:
    """Minimal duck-typed device exposing the methods the adapter calls."""

    def __init__(self, reading: Reading | None = None, error: AlicatError | None = None) -> None:
        self._reading = reading
        self._error = error
        self.calls = 0

    async def poll(self) -> Reading:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._reading is not None
        return self._reading


@pytest.mark.anyio
async def test_success_wraps_reading() -> None:
    reading = _fake_reading()
    device = _StubDevice(reading=reading)
    adapter = PollSourceAdapter("fuel", device)  # type: ignore[arg-type]

    result: Mapping[str, DeviceResult[Reading]] = await adapter.poll()
    assert set(result.keys()) == {"fuel"}
    assert result["fuel"].ok
    assert result["fuel"].value is reading
    assert result["fuel"].error is None


@pytest.mark.anyio
async def test_failure_wraps_error() -> None:
    err = AlicatTimeoutError("device silent")
    device = _StubDevice(error=err)
    adapter = PollSourceAdapter("air", device)  # type: ignore[arg-type]

    result = await adapter.poll()
    assert "air" in result
    assert not result["air"].ok
    assert result["air"].error is err


@pytest.mark.anyio
async def test_names_filter_excludes_returns_empty() -> None:
    """When ``names`` is supplied and excludes our name, return empty mapping.

    The recorder always passes a complete name set, so this is harmless
    in single-device mode; capa's old shim ignored the filter, this
    adapter honours it per cross-lib spec §E.
    """
    device = _StubDevice(reading=_fake_reading())
    adapter = PollSourceAdapter("fuel", device)  # type: ignore[arg-type]

    result = await adapter.poll(names=("air", "h2"))
    assert result == {}
    assert device.calls == 0  # filter short-circuits before I/O


@pytest.mark.anyio
async def test_names_filter_includes_polls_device() -> None:
    device = _StubDevice(reading=_fake_reading())
    adapter = PollSourceAdapter("fuel", device)  # type: ignore[arg-type]

    result = await adapter.poll(names=("fuel", "air"))
    assert "fuel" in result
    assert result["fuel"].ok
    assert device.calls == 1


@pytest.mark.anyio
async def test_names_none_polls_device() -> None:
    """The default (``names=None``) means "poll me unconditionally"."""
    device = _StubDevice(reading=_fake_reading())
    adapter = PollSourceAdapter("fuel", device)  # type: ignore[arg-type]

    result = await adapter.poll(names=None)
    assert "fuel" in result


@pytest.mark.anyio
async def test_names_iterable_consumed_once() -> None:
    """Single-pass iterables are valid — exercise via a generator."""

    def _names() -> Iterable[str]:
        yield "fuel"

    device = _StubDevice(reading=_fake_reading())
    adapter = PollSourceAdapter("fuel", device)  # type: ignore[arg-type]

    result = await adapter.poll(names=_names())
    assert "fuel" in result
