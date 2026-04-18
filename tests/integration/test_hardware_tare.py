"""Hardware smoke test: tare commands — ``T`` / ``TP`` / ``PC``.

Opt-in — skipped unless :envvar:`ALICATLIB_TEST_PORT` *and*
:envvar:`ALICATLIB_ENABLE_STATEFUL_TESTS=1` are both set. All tare
operations mutate the device's calibration state (zero-reference
shift) — they're genuinely state-changing, so the tests are tagged
:pytest.mark:`hardware_stateful` and gated by the conftest hook.

Design §5.18 pt 6: every tare has an unverifiable precondition —
"no flow" for ``T``, "line depressurised" for ``TP`` / ``PC`` — and
the facade emits an INFO log naming that precondition on each call.
The smoke test below can't check the log because that's a unit-test
concern; it checks the post-op :class:`TareResult` came back with a
non-None frame.

Run (with the device valves closed and lines vented)::

    ALICATLIB_TEST_PORT=/dev/ttyUSB0 ALICATLIB_ENABLE_STATEFUL_TESTS=1 \\
    uv run pytest -m hardware_stateful tests/integration/test_hardware_tare.py -v -s
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from alicatlib.commands.base import Capability
from alicatlib.devices.flow_meter import FlowMeter

if TYPE_CHECKING:
    from alicatlib.devices.base import Device

pytestmark = pytest.mark.hardware_stateful


@pytest.mark.anyio
async def test_tare_flow_returns_a_frame(hardware_device: Device) -> None:
    """``T`` returns a post-op data frame; facade wraps into :class:`TareResult`."""
    from alicatlib.errors import AlicatFirmwareError

    if not isinstance(hardware_device, FlowMeter):
        pytest.skip("tare_flow applies to flow meters / controllers")

    try:
        result = await hardware_device.tare_flow()
    except AlicatFirmwareError:
        pytest.skip("tare_flow (T) requires numeric firmware (not GP)")
    assert result.frame is not None
    assert result.frame.unit_id == hardware_device.unit_id

    print(  # noqa: T201
        f"\n[hardware] tare_flow frame keys={sorted(result.frame.values.keys())}",
    )


@pytest.mark.anyio
async def test_tare_gauge_pressure_returns_a_frame(
    hardware_device: Device,
) -> None:
    """``TP`` zeroes the gauge-pressure reading; returns a post-op frame."""
    from alicatlib.errors import AlicatFirmwareError

    if not isinstance(hardware_device, FlowMeter):
        pytest.skip("tare_gauge_pressure applies to flow / pressure devices")

    try:
        result = await hardware_device.tare_gauge_pressure()
    except AlicatFirmwareError:
        pytest.skip("tare_gauge_pressure (TP) requires numeric firmware (not GP)")
    assert result.frame is not None
    assert result.frame.unit_id == hardware_device.unit_id


@pytest.mark.anyio
async def test_tare_absolute_pressure_when_tareable_abs_present(
    hardware_device: Device,
) -> None:
    """``PC`` requires :attr:`Capability.TAREABLE_ABSOLUTE_PRESSURE`.

    The library has no safe probe for this capability (test-writing
    ``PC`` would actually tare the device), so the default on every
    device is absent. Users who know their hardware supports ``PC``
    opt in via ``assume_capabilities=Capability.TAREABLE_ABSOLUTE_PRESSURE``
    on :func:`open_device` — none of the current hardware fixture
    paths set this, so the test skips on every device we've captured.

    Kept around so the test surface exercises the gate when a real
    PC-capable pressure meter/controller shows up (via a future
    env-var / fixture refinement that injects ``assume_capabilities``).

    Design context: §16.6.7 — four flow-controller devices advertised
    ``BAROMETER`` via ``FPF 15 > 0`` yet rejected or silently ignored
    ``PC``, which forced the split between ``BAROMETER`` (reports a
    barometric reading) and ``TAREABLE_ABSOLUTE_PRESSURE`` (has a
    process-port abs sensor that can be re-zeroed).
    """
    if not isinstance(hardware_device, FlowMeter):
        pytest.skip("tare_absolute_pressure applies to flow / pressure devices")

    if Capability.TAREABLE_ABSOLUTE_PRESSURE not in hardware_device.info.capabilities:
        pytest.skip(
            "device does not advertise Capability.TAREABLE_ABSOLUTE_PRESSURE; "
            "PC would raise AlicatMissingHardwareError pre-I/O by design. "
            "See design §16.6.7 for the BAROMETER-vs-TAREABLE split.",
        )

    result = await hardware_device.tare_absolute_pressure()
    assert result.frame is not None
