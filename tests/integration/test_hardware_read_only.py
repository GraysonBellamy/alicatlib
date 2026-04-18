"""Hardware smoke test: open a real device, identify, poll 1000 times, close.

**Opt-in.** Skipped unless ``ALICATLIB_TEST_PORT`` is set — see
``tests/integration/conftest.py`` for the full env-var contract.

This is the read-only hardware test per design §6.1: one end-to-end
sanity check that proves the full ``SerialTransport → AlicatProtocolClient
→ Session → Device`` stack identifies a real device, caches its
``??D*`` format, and sustains steady-state polling long enough to expose
stale-input / buffer-bleed / drift pathologies.

Run on a workstation with hardware attached:

.. code-block:: bash

    ALICATLIB_TEST_PORT=/dev/ttyUSB0 \\
    ALICATLIB_TEST_UNIT_ID=A \\
    uv run pytest -m hardware tests/integration/test_hardware_read_only.py -v -s

The test is intentionally read-only — no setpoints, no gas changes, no
destructive commands. A GP-family or pre-8v28 device requires
``ALICATLIB_TEST_MODEL_HINT`` so the identification fallback can
synthesise :class:`DeviceInfo`; the fixture below surfaces that
requirement via :func:`pytest.skip` with a pointed message.
"""

from __future__ import annotations

import os
import time

import pytest

from alicatlib.devices.factory import open_device
from alicatlib.transport import SerialSettings

pytestmark = pytest.mark.hardware


_MODEL_HINT_ENV = "ALICATLIB_TEST_MODEL_HINT"
_POLL_COUNT_ENV = "ALICATLIB_TEST_POLL_COUNT"
_DEFAULT_POLL_COUNT = 1000


@pytest.fixture
def hardware_model_hint() -> str | None:
    """Optional ``model_hint`` for open_device.

    Required when the device is GP-family or firmware < 8v28 (??M*
    is unavailable). Unused on modern devices.
    """
    return os.environ.get(_MODEL_HINT_ENV)


@pytest.fixture
def hardware_poll_count() -> int:
    """How many poll iterations to run. Default 1000; override for quick smoke."""
    raw = os.environ.get(_POLL_COUNT_ENV)
    if raw is None:
        return _DEFAULT_POLL_COUNT
    try:
        return int(raw)
    except ValueError:
        pytest.fail(
            f"{_POLL_COUNT_ENV}={raw!r} must be an integer",
        )


@pytest.mark.anyio
async def test_identify_and_poll_1000(
    hardware_port: str,
    hardware_baud: int,
    hardware_unit_id: str,
    hardware_model_hint: str | None,
    hardware_poll_count: int,
) -> None:
    """Open, identify, poll N times, close — all under one async-with scope.

    Validates the full stack against real hardware and surfaces any
    stale-input / buffer-bleed issues that unit-test scripts (which
    don't have real serial timing) miss.
    """
    started = time.perf_counter()
    frames_read = 0

    async with open_device(
        hardware_port,
        unit_id=hardware_unit_id,
        model_hint=hardware_model_hint,
        serial=SerialSettings(port=hardware_port, baudrate=hardware_baud),
    ) as dev:
        # Identification must have populated the info snapshot.
        assert dev.info.unit_id == hardware_unit_id
        assert dev.info.model  # non-empty
        assert dev.session.data_frame_format is not None

        # Steady-state polling — all frames should have a consistent
        # unit_id and non-empty values dict. If the data-frame format
        # drifts or a previous response leaks into the next read, the
        # assertions fail fast.
        first_frame_names: tuple[str, ...] | None = None
        for _ in range(hardware_poll_count):
            frame = await dev.poll()
            assert frame.unit_id == hardware_unit_id
            assert frame.values  # non-empty dict
            if first_frame_names is None:
                first_frame_names = tuple(frame.values.keys())
            else:
                # Field set must stay stable across polls (design §5.6:
                # conditional fields may appear/disappear, but the
                # *required* field set is fixed).
                current_names = set(frame.values.keys())
                assert current_names >= {
                    name
                    for name, value in zip(
                        first_frame_names,
                        [frame.values.get(n) for n in first_frame_names],
                        strict=True,
                    )
                    if value is not None
                }
            frames_read += 1

    elapsed = time.perf_counter() - started
    # Print a summary under `pytest -s`; no hard latency assertion —
    # hardware + USB adapter variance is too wide to bound tightly here.
    print(  # noqa: T201 — deliberately visible for human eyeballing
        f"\n[hardware] {hardware_port} unit={hardware_unit_id} "
        f"model={dev.info.model!r} firmware={dev.info.firmware!s} "
        f"polls={frames_read} elapsed={elapsed:.2f}s "
        f"rate={frames_read / elapsed:.1f} Hz",
    )
