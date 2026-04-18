"""Hardware smoke test: ``DCU`` engineering-units + ``FPF`` full-scale.

Opt-in — skipped unless :envvar:`ALICATLIB_TEST_PORT` is set.

Covers against a real device:

- :data:`ENGINEERING_UNITS` (``DCU``) query — read the current unit
  binding per statistic. Read-only; plain :pytest.mark:`hardware`.
- :data:`ENGINEERING_UNITS` set — changes device state (unit binding
  + data-frame format); :pytest.mark:`hardware_stateful` with a
  restore-on-teardown dance so the run leaves the device as it found it.
- :data:`FULL_SCALE_QUERY` (``FPF``) — read the full-scale value for
  a statistic. Read-only; plain :pytest.mark:`hardware`.

Run with::

    ALICATLIB_TEST_PORT=/dev/ttyUSB0 \\
    uv run pytest -m hardware tests/integration/test_hardware_units.py -v -s

    # Stateful variant — changes unit binding + restores:
    ALICATLIB_TEST_PORT=/dev/ttyUSB0 ALICATLIB_ENABLE_STATEFUL_TESTS=1 \\
    uv run pytest -m hardware_stateful tests/integration/test_hardware_units.py -v -s
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from alicatlib.devices.flow_meter import FlowMeter
from alicatlib.registry import Statistic

if TYPE_CHECKING:
    from alicatlib.devices.base import Device


@pytest.mark.hardware
@pytest.mark.anyio
async def test_engineering_units_query(hardware_device: Device) -> None:
    """``DCU`` returns a :class:`UnitSetting` for the active mass-flow unit."""
    from alicatlib.errors import AlicatFirmwareError

    if not isinstance(hardware_device, FlowMeter):
        pytest.skip("engineering_units applies to flow / pressure devices")

    try:
        setting = await hardware_device.engineering_units(Statistic.MASS_FLOW)
    except AlicatFirmwareError:
        pytest.skip("DCU unit-query requires V10 10v05+")
    assert setting.unit_id == hardware_device.unit_id
    assert setting.statistic is Statistic.MASS_FLOW
    assert setting.label  # device always reports a non-empty label

    print(  # noqa: T201
        f"\n[hardware] DCU mass_flow unit={setting.unit} label={setting.label!r}",
    )


@pytest.mark.hardware
@pytest.mark.anyio
async def test_full_scale_query(hardware_device: Device) -> None:
    """``FPF`` returns a :class:`FullScaleValue` for the mass-flow stat."""
    from alicatlib.errors import AlicatCommandRejectedError, AlicatFirmwareError

    if not isinstance(hardware_device, FlowMeter):
        pytest.skip("full_scale applies to flow / pressure devices")

    try:
        fs = await hardware_device.full_scale(Statistic.MASS_FLOW)
    except AlicatFirmwareError:
        pytest.skip("FPF requires numeric firmware (6v00+); GP unsupported")
    except AlicatCommandRejectedError:
        pytest.skip(
            "device rejected FPF despite family gate — "
            "per-device behavior recorded in tests/fixtures/device_matrix.yaml "
            "(e.g. 5v12 rejects; see design §16.6.2)",
        )
    assert fs.statistic is Statistic.MASS_FLOW
    assert fs.value > 0, "full-scale mass flow should be positive"
    assert fs.unit_label

    print(  # noqa: T201
        f"\n[hardware] FPF mass_flow value={fs.value} unit={fs.unit} label={fs.unit_label!r}",
    )


@pytest.mark.hardware_stateful
@pytest.mark.anyio
async def test_engineering_units_set_and_restore(
    hardware_device: Device,
) -> None:
    """Set DCU to a known alternate unit, verify, restore.

    State-changing so tagged :pytest.mark:`hardware_stateful`. The
    restore step uses the same facade: the pre-flight query captures
    the original :class:`Unit` / raw code, and the teardown block
    writes it back via the raw integer code so the session-level
    invalidation path also survives the round-trip.
    """
    from alicatlib.errors import AlicatFirmwareError

    if not isinstance(hardware_device, FlowMeter):
        pytest.skip("engineering_units applies to flow / pressure devices")

    try:
        original = await hardware_device.engineering_units(Statistic.MASS_FLOW)
    except AlicatFirmwareError:
        pytest.skip("DCU unit-query requires V10 10v05+")

    # Pick a different unit in the same category. Try SLPM → SCCM, or
    # vice versa — one of them is always "the other one". If the device
    # is already set to something exotic we skip with a pointed note.
    from alicatlib.registry import Unit

    candidates: tuple[Unit, ...] = (Unit.SLPM, Unit.SCCM)
    target: Unit | None = next(
        (c for c in candidates if c is not original.unit),
        None,
    )
    if target is None:
        pytest.skip(
            f"device mass-flow unit {original.unit!r} isn't in the "
            "standard candidate list; hand-write a one-shot test",
        )

    try:
        changed = await hardware_device.engineering_units(
            Statistic.MASS_FLOW,
            target,
        )
        assert changed.unit is target

        # Read-back confirms persistence within the session.
        read_back = await hardware_device.engineering_units(Statistic.MASS_FLOW)
        assert read_back.unit is target

        print(  # noqa: T201
            f"\n[hardware] DCU set mass_flow {original.unit!r} -> {target!r} ok",
        )
    finally:
        # Restore — best-effort; teardown failures should not mask a
        # primary assertion failure in the test body above.
        if original.unit is not None and original.unit is not target:
            await hardware_device.engineering_units(
                Statistic.MASS_FLOW,
                original.unit,
            )
