"""Hardware smoke test: setpoint + loop control + setpoint source.

Opt-in — skipped unless :envvar:`ALICATLIB_TEST_PORT` is set. The set-
path tests additionally require
:envvar:`ALICATLIB_ENABLE_STATEFUL_TESTS=1` because they drive the
valve to a new target.

Covers against a real device:

- :meth:`FlowController.setpoint` query — read-only, plain ``hardware``.
- :meth:`FlowController.setpoint` set — drives the valve to a target
  and restores the original setpoint on teardown; ``hardware_stateful``.
- :meth:`FlowController.setpoint_source` query + cache update —
  read-only, plain ``hardware``.
- :meth:`FlowController.loop_control_variable` query — read-only.

Firmware-gated dispatch between modern ``LS`` and legacy ``S`` is
transparent at the facade; legacy firmware skips the query-form tests
with a pointed message because legacy ``S`` is set-only.

Run with::

    ALICATLIB_TEST_PORT=/dev/ttyUSB0 \\
    uv run pytest -m hardware tests/integration/test_hardware_setpoint.py -v -s

    # Stateful set-path (drives the valve!):
    ALICATLIB_TEST_PORT=/dev/ttyUSB0 ALICATLIB_ENABLE_STATEFUL_TESTS=1 \\
    uv run pytest -m hardware_stateful tests/integration/test_hardware_setpoint.py -v -s
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from alicatlib.devices.flow_controller import FlowController
from alicatlib.errors import AlicatError, AlicatUnsupportedCommandError
from tests._typing import approx

if TYPE_CHECKING:
    from alicatlib.devices.base import Device


@pytest.mark.hardware
@pytest.mark.anyio
async def test_setpoint_query(hardware_device: Device) -> None:
    """``LS`` query returns a fully-typed :class:`SetpointState`.

    Design §16.6: the modern ``LS`` reply is 5 fields
    (``<uid> <current> <requested> <unit_code> <unit_label>``), so the
    facade returns ``SetpointState`` directly with ``current`` and
    ``requested`` separately on the wire and ``frame=None``. The legacy
    ``S`` path still wraps a post-op data frame, but legacy has no
    query form so query-path always exercises the modern decoder.
    """
    if not isinstance(hardware_device, FlowController):
        pytest.skip("setpoint applies to controllers only")

    try:
        state = await hardware_device.setpoint()
    except AlicatUnsupportedCommandError:
        pytest.skip(
            "firmware pre-dates LS (9v00+); legacy S is set-only, no query",
        )
    assert state.unit_id == hardware_device.unit_id
    # Modern LS decoder produces no frame (design §16.6); current and
    # requested are both populated from the 5-field wire reply.
    assert state.frame is None
    assert state.current is not None
    assert state.requested is not None
    assert state.unit_label  # non-empty

    print(  # noqa: T201
        f"\n[hardware] setpoint current={state.current} "
        f"requested={state.requested} "
        f"unit={state.unit} label={state.unit_label!r}",
    )


@pytest.mark.hardware
@pytest.mark.anyio
async def test_setpoint_source_query_updates_cache(
    hardware_device: Device,
) -> None:
    """``LSS`` query populates :attr:`Session.setpoint_source`.

    Pins the cache-invariant behaviour: the facade always updates
    the session cache on both query and set. Downstream
    :meth:`FlowController.setpoint` reads the cache pre-I/O to detect
    the ``LSS=A silently ignores serial setpoint`` failure mode.
    """
    from alicatlib.errors import AlicatFirmwareError

    if not isinstance(hardware_device, FlowController):
        pytest.skip("setpoint_source applies to controllers only")

    assert hardware_device.session.setpoint_source is None
    try:
        mode = await hardware_device.setpoint_source()
    except AlicatFirmwareError:
        pytest.skip("LSS requires V10 10v05+")
    assert mode in {"S", "A", "U"}
    assert hardware_device.session.setpoint_source == mode


@pytest.mark.hardware
@pytest.mark.anyio
async def test_loop_control_variable_query(hardware_device: Device) -> None:
    """``LV`` query returns a :class:`LoopControlState`."""
    if not isinstance(hardware_device, FlowController):
        pytest.skip("loop_control_variable applies to controllers only")

    from alicatlib.errors import AlicatFirmwareError

    try:
        state = await hardware_device.loop_control_variable()
    except AlicatFirmwareError:
        pytest.skip("LV requires 9v00+ firmware")

    assert state.unit_id == hardware_device.unit_id
    print(  # noqa: T201
        f"\n[hardware] LV variable={state.variable.name} label={state.label!r}",
    )


@pytest.mark.hardware_stateful
@pytest.mark.anyio
async def test_setpoint_set_and_restore(hardware_device: Device) -> None:
    """Drive the valve to a small target and restore.

    Uses 10% of full-scale as the target — large enough to be outside
    the typical dead-band and small enough not to stress the device.
    Restores the original setpoint on teardown even if assertions fail.
    """
    if not isinstance(hardware_device, FlowController):
        pytest.skip("setpoint set applies to controllers only")

    # When the device's loop-control setpoint source is analog (LSS=A)
    # or user-knob (LSS=U), serial setpoint writes are silently ignored
    # by the device — the post-set LS reply echoes the prior value, so
    # the write looks like a no-op (observed on MC-5SLPM-D / 10v20 on
    # 2026-04-17 with LSS=A). Skip rather than fail: there's no way to
    # test setpoint set without flipping LSS to S, which is out of scope
    # for a stateful-but-non-destructive test.
    from alicatlib.errors import AlicatFirmwareError

    try:
        source_mode = await hardware_device.setpoint_source()
    except (AlicatFirmwareError, AlicatUnsupportedCommandError):
        source_mode = None  # pre-10v05; LSS unavailable — proceed anyway
    if source_mode is not None and source_mode != "S":
        pytest.skip(
            f"device setpoint source is LSS={source_mode!r} — serial "
            "setpoint writes are silently ignored; flip to 'S' to exercise this test",
        )

    try:
        original = await hardware_device.setpoint()
    except AlicatUnsupportedCommandError:
        pytest.skip("setpoint query unsupported — legacy S path, skipping for now")

    # Pick a target: 10% of full-scale, or 10 if full_scale is unavailable.
    from alicatlib.registry import Statistic

    try:
        fs = await hardware_device.full_scale(Statistic.MASS_FLOW)
        target = fs.value * 0.10
    except AlicatError:
        # FPF may not be implemented on older firmware; 10 SCCM is a
        # conservative fallback that works for every catalog MFC.
        target = 10.0

    original_value = original.requested
    try:
        changed = await hardware_device.setpoint(target)
        assert changed.requested == approx(target, rel=0.05)
        print(  # noqa: T201
            f"\n[hardware] setpoint {original_value} -> {target} ok",
        )
    finally:
        # Restore even if the assert fails.
        await hardware_device.setpoint(original_value)
