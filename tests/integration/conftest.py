"""Integration-test fixtures.

Fake-backed integration tests live here when they span multiple layers.
Hardware-backed tests skip unless the user sets the env vars below.

Env vars (all optional; hardware tests skip if ``ALICATLIB_TEST_PORT`` is
unset):

- ``ALICATLIB_TEST_PORT`` — serial device path, e.g. ``/dev/ttyUSB0``.
  Required for every hardware test to actually run.
- ``ALICATLIB_TEST_UNIT_ID`` — single letter ``A``–``Z`` of the device on
  the bus. Defaults to ``A``.
- ``ALICATLIB_TEST_FIRMWARE`` — firmware string as reported by the device
  (e.g. ``10v05``, ``GP``). Commands that firmware-gate need this supplied
  explicitly when ``??M*`` identification is unavailable.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.transport import SerialSettings, SerialTransport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


_HARDWARE_PORT_ENV = "ALICATLIB_TEST_PORT"
_HARDWARE_UNIT_ID_ENV = "ALICATLIB_TEST_UNIT_ID"
_HARDWARE_FIRMWARE_ENV = "ALICATLIB_TEST_FIRMWARE"
_HARDWARE_BAUD_ENV = "ALICATLIB_TEST_BAUD"
_STATEFUL_ENV = "ALICATLIB_ENABLE_STATEFUL_TESTS"
_DESTRUCTIVE_ENV = "ALICATLIB_ENABLE_DESTRUCTIVE_TESTS"
_TRUE_STRINGS: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _env_flag_enabled(name: str) -> bool:
    """Return ``True`` when env var ``name`` is set to a truthy string."""
    value = os.environ.get(name, "").strip().lower()
    return value in _TRUE_STRINGS


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Auto-skip ``hardware_stateful`` / ``hardware_destructive`` unless opted in.

    Design §5.15 pins the env-var contract. This hook fulfils it: a test
    tagged ``hardware_stateful`` skips unless :envvar:`ALICATLIB_ENABLE_STATEFUL_TESTS`
    is truthy, and ``hardware_destructive`` skips unless
    :envvar:`ALICATLIB_ENABLE_DESTRUCTIVE_TESTS` is truthy. Plain
    ``hardware`` tests still rely on the per-test
    :fixture:`hardware_port` skip (set :envvar:`ALICATLIB_TEST_PORT` to
    unblock), because a device can be attached without the user also
    wanting their calibration changed.
    """
    del config  # not needed — env is authoritative
    stateful_on = _env_flag_enabled(_STATEFUL_ENV)
    destructive_on = _env_flag_enabled(_DESTRUCTIVE_ENV)
    stateful_skip = pytest.mark.skip(
        reason=f"set {_STATEFUL_ENV}=1 to run state-changing hardware tests",
    )
    destructive_skip = pytest.mark.skip(
        reason=f"set {_DESTRUCTIVE_ENV}=1 to run destructive hardware tests",
    )
    for item in items:
        if "hardware_destructive" in item.keywords and not destructive_on:
            item.add_marker(destructive_skip)
        elif "hardware_stateful" in item.keywords and not stateful_on:
            item.add_marker(stateful_skip)


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def hardware_port() -> str:
    """Path of the serial device under test; skip the test if unset."""
    port = os.environ.get(_HARDWARE_PORT_ENV)
    if not port:
        pytest.skip(
            f"Set {_HARDWARE_PORT_ENV}=/dev/ttyUSB0 (or similar) to run hardware tests.",
        )
    return port


@pytest.fixture
def hardware_unit_id() -> str:
    """Single-letter unit id on the bus. Defaults to 'A'."""
    return os.environ.get(_HARDWARE_UNIT_ID_ENV, "A")


@pytest.fixture
def hardware_baud() -> int:
    """Baud rate of the device under test. Defaults to 19200 (Alicat doc default).

    Override via :envvar:`ALICATLIB_TEST_BAUD`. Note: a real Alicat 8v17
    was found at 115200 in the field, so the documented default is *not*
    a guarantee — explicit configuration wins.
    """
    raw = os.environ.get(_HARDWARE_BAUD_ENV, "19200")
    try:
        return int(raw)
    except ValueError:
        pytest.fail(f"{_HARDWARE_BAUD_ENV}={raw!r} must be an integer")


@pytest.fixture
def hardware_firmware() -> FirmwareVersion:
    """Device firmware version. Defaults to ``10v05``.

    Users must set ``ALICATLIB_TEST_FIRMWARE`` if their device runs a
    different family (e.g. ``GP`` for gauge-pressure units) — otherwise
    commands with firmware-family gating will mis-identify.
    """
    return FirmwareVersion.parse(os.environ.get(_HARDWARE_FIRMWARE_ENV, "10v05"))


@pytest.fixture
async def hardware_client(
    hardware_port: str,
    hardware_baud: int,
) -> AsyncIterator[AlicatProtocolClient]:
    """Open the real device, yield a wired protocol client, close on teardown."""
    transport = SerialTransport(SerialSettings(port=hardware_port, baudrate=hardware_baud))
    await transport.open()
    try:
        yield AlicatProtocolClient(transport)
    finally:
        await transport.close()


@pytest.fixture
def hardware_model_hint() -> str | None:
    """Optional ``model_hint`` for :func:`open_device`.

    Required when the device is GP-family or firmware < 8v28 (``??M*``
    is unavailable there); ignored on modern firmware. Reuses the
    ``ALICATLIB_TEST_MODEL_HINT`` env var defined by the read-only
    hardware test so callers don't have to remember two knobs.
    """
    return os.environ.get("ALICATLIB_TEST_MODEL_HINT")


@pytest.fixture
async def hardware_device(
    hardware_port: str,
    hardware_unit_id: str,
    hardware_baud: int,
    hardware_model_hint: str | None,
) -> AsyncIterator[object]:
    """Open the real device via :func:`open_device` and yield the facade.

    Closes the device on teardown. Returns :class:`object` for signature
    simplicity — tests type-cast to the concrete facade
    (``FlowMeter`` / ``FlowController``) inside the test body so the
    fixture stays usable across device kinds.
    """
    from alicatlib.devices.factory import open_device

    async with open_device(
        hardware_port,
        unit_id=hardware_unit_id,
        serial=SerialSettings(port=hardware_port, baudrate=hardware_baud),
        model_hint=hardware_model_hint,
    ) as dev:
        yield dev
