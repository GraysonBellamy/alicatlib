"""Hardware smoke test: ``??G*`` gas list round-trip.

Opt-in — skipped unless :envvar:`ALICATLIB_TEST_PORT` is set. See
``tests/integration/conftest.py`` for the full env-var contract.

Covers against a real device:

- :data:`GAS_LIST` (``??G*``) — enumerate built-in + mixture gases.
  Read-only; ships under the plain :pytest.mark:`hardware` marker.
- Firmware-gated gas dispatch: a pre-10v05 device routes through the
  legacy ``G`` path. The integration test here only validates the
  read-only listing because a real set-gas would be state-changing
  (:pytest.mark:`hardware_stateful` territory).

Run with::

    ALICATLIB_TEST_PORT=/dev/ttyUSB0 \\
    uv run pytest -m hardware tests/integration/test_hardware_gas_list.py -v -s
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from alicatlib.devices.flow_meter import FlowMeter

if TYPE_CHECKING:
    from alicatlib.devices.base import Device

pytestmark = pytest.mark.hardware


@pytest.mark.anyio
async def test_gas_list_round_trips(hardware_device: Device) -> None:
    """``??G*`` returns at least the built-in gases with non-empty labels.

    Validates the stack end-to-end against a real device:
    ``FlowMeter.gas_list()`` → session dispatch → transport →
    :func:`parse_gas_list` → typed :class:`dict[int, str]`. The assertion
    shape is deliberately permissive (every device has a different list;
    pinning counts is a hardware-fixture exercise, not a smoke test).
    """
    if not isinstance(hardware_device, FlowMeter):
        pytest.skip(
            f"gas_list applies to flow devices; attached device is "
            f"{type(hardware_device).__name__}",
        )

    listing = await hardware_device.gas_list()
    assert listing, "device reported no gases at all — expected at least Air / N2"
    for code, label in listing.items():
        assert code >= 0
        assert label, f"gas code {code} has an empty label"

    # Print for eyeballing under `pytest -s`.
    print(  # noqa: T201
        f"\n[hardware] gas_list entries={len(listing)} "
        f"codes={sorted(listing.keys())[:10]}"
        f"{'...' if len(listing) > 10 else ''}",
    )


@pytest.mark.anyio
async def test_gas_query_dispatches_and_matches(
    hardware_device: Device,
) -> None:
    """:meth:`FlowMeter.gas` query → typed :class:`GasState`.

    Read-only — firmware-aware dispatch between ``GS`` and legacy ``G``
    is exercised transparently. Legacy firmware has no query form, so
    the facade raises :class:`AlicatUnsupportedCommandError`; that's a
    pass-through to the caller, not a bug.
    """
    if not isinstance(hardware_device, FlowMeter):
        pytest.skip("gas query applies to flow devices")

    from alicatlib.errors import AlicatUnsupportedCommandError

    try:
        state = await hardware_device.gas()
    except AlicatUnsupportedCommandError:
        pytest.skip(
            "device firmware pre-dates LS/GS; legacy G has no query form "
            "(dispatch surface; expected behavior)",
        )
    assert state.unit_id == hardware_device.unit_id
    assert state.label
    assert state.long_name

    print(  # noqa: T201
        f"\n[hardware] gas={state.gas.value} (code {state.code}) long={state.long_name!r}",
    )
