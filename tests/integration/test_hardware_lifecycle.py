"""Hardware smoke test: session lifecycle changes.

Covers :meth:`Session.change_unit_id` and :meth:`Session.change_baud_rate`
against real hardware. Both are mutating operations with specific
safety tiers (design §5.15 / §5.20):

- :meth:`Session.change_unit_id` is :pytest.mark:`hardware_stateful` —
  the test changes the device's unit id, verifies via ``VE`` at the
  new id, and restores the original id on teardown.
- :meth:`Session.change_baud_rate` is :pytest.mark:`hardware_destructive`
  — a failed baud change can leave the adapter unable to reach the
  device until the user manually reopens the port. Guarded hard
  (opt-in env var) and uses conservative baudrates (19200 ↔ 38400
  round-trip) with teardown restoration.

Run only the restorable test::

    ALICATLIB_TEST_PORT=/dev/ttyUSB0 ALICATLIB_ENABLE_STATEFUL_TESTS=1 \\
    uv run pytest -m hardware_stateful tests/integration/test_hardware_lifecycle.py -v -s

Opt in to the destructive baud-change test (understand the risk first)::

    ALICATLIB_TEST_PORT=/dev/ttyUSB0 \\
    ALICATLIB_ENABLE_DESTRUCTIVE_TESTS=1 \\
    uv run pytest -m hardware_destructive tests/integration/test_hardware_lifecycle.py -v -s
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from alicatlib.devices.session import SessionState

if TYPE_CHECKING:
    from alicatlib.devices.base import Device


@pytest.mark.hardware_stateful
@pytest.mark.anyio
async def test_change_unit_id_round_trip(hardware_device: Device) -> None:
    """Rename to a free letter, verify, restore on teardown.

    Picks a target unit id by scanning A..Z for the first letter that
    isn't the current one. On a bus with multiple devices this could
    collide; the design leaves that to the caller since we can't safely
    probe every letter without I/O.
    """
    session = hardware_device.session
    original_unit_id = session.unit_id
    target = next(
        (letter for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if letter != original_unit_id),
        None,
    )
    assert target is not None  # A..Z includes something other than the current id

    from alicatlib.errors import AlicatTimeoutError

    try:
        await session.change_unit_id(target, confirm=True)
    except AlicatTimeoutError:
        # Some GP-era firmware (observed on a GP07R100, design §16.6.8)
        # silently ignores ``<old>@ <new>\r`` even though the primer
        # lists the command as working on "All" firmware. Treat the
        # verify timeout as a device-specific skip rather than a
        # library bug.
        pytest.skip(
            "device did not accept <old>@ <new> rename — likely GP-era "
            "firmware that predates the command",
        )
    try:
        assert session.unit_id == target
        assert session.info.unit_id == target
        print(  # noqa: T201
            f"\n[hardware] change_unit_id {original_unit_id!r} -> {target!r} ok",
        )
    finally:
        # Restore — best-effort; teardown failure shouldn't mask a
        # primary assertion failure above. If this fails, the caller
        # sees both errors in the pytest report.
        if session.unit_id == target:
            await session.change_unit_id(original_unit_id, confirm=True)


@pytest.mark.hardware_destructive
@pytest.mark.anyio
async def test_change_baud_rate_round_trip(hardware_device: Device) -> None:
    """19200 → 38400 → 19200, verifying ``VE`` at each step.

    ``hardware_destructive`` because a failed reopen leaves the adapter
    on the old baud while the device is on the new baud — user has to
    close the port and reopen at the new baud manually to recover. The
    test itself never leaves the device on a non-19200 baud (teardown
    restores), so a clean run returns you to where you started.

    If the session transitions to :attr:`SessionState.BROKEN` at any
    step, the test fails and the tear-down branch is skipped — the
    device may be stranded at 38400 and the user must recover manually.
    """
    session = hardware_device.session
    # Only run when the current port is at 19200. The session doesn't
    # expose its current baud, so we proxy via a hardware property: if
    # the user started at a non-default baud, they've opted in to
    # different behaviour and this test shouldn't assume otherwise.
    # TODO: once Session / Transport expose the cached baud publicly,
    #       tighten this guard from a comment to a runtime check.
    original = 19200
    target = 38400

    await session.change_baud_rate(target, confirm=True)
    assert session.state is SessionState.OPERATIONAL
    try:
        # Verify dispatch works at the new baud — a poll is the cheapest
        # round-trip and exercises the full session dispatch path.
        await hardware_device.poll()
        print(  # noqa: T201
            f"\n[hardware] change_baud_rate {original} -> {target} ok",
        )
    finally:
        if session.state is SessionState.OPERATIONAL:
            await session.change_baud_rate(original, confirm=True)
