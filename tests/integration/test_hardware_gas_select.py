"""Hardware smoke test: read the active gas from a real Alicat device.

**Opt-in.** Skipped unless ``ALICATLIB_TEST_PORT`` is set in the environment.
See ``tests/integration/conftest.py`` for the full env-var contract.

Run with::

    ALICATLIB_TEST_PORT=/dev/ttyUSB0 \\
    ALICATLIB_TEST_FIRMWARE=10v05 \\
    uv run pytest -m hardware tests/integration/test_hardware_gas_select.py -v -s

The ``-s`` is worth passing — the test prints the active gas so you can
eyeball that the round-trip matches what the device's LCD shows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import GAS_SELECT, DecodeContext, GasSelectRequest
from alicatlib.firmware import FirmwareFamily, FirmwareVersion

if TYPE_CHECKING:
    from alicatlib.protocol import AlicatProtocolClient

pytestmark = pytest.mark.hardware


@pytest.mark.anyio
async def test_gas_query_round_trips(
    hardware_client: AlicatProtocolClient,
    hardware_unit_id: str,
    hardware_firmware: FirmwareVersion,
) -> None:
    """The device echoes its active gas.

    Validates the full stack against real hardware:
    ``SerialTransport → AlicatProtocolClient → GAS_SELECT.encode/decode``.
    """
    # GS only exists on the V10 family (10v05+). V8/V9 and GP devices use
    # the legacy ``G`` command path. Cross-family firmware comparisons
    # are rejected by design (§11 risk mitigation), so gate on family — V8/V9
    # is by definition pre-10v05 since that's the V10 family.
    if hardware_firmware.family is not FirmwareFamily.V10:
        pytest.skip(
            f"GS is V10-family only (10v05+); device reports family "
            f"{hardware_firmware.family.value!r} — legacy G path covers it.",
        )
    # Pre-10v05 V10 firmware rejects GS with `?`. The test bypasses the
    # session (uses hardware_client directly) so the ``min_firmware`` gate
    # doesn't fire — enforce it here explicitly.
    if hardware_firmware < FirmwareVersion(
        family=FirmwareFamily.V10,
        major=10,
        minor=5,
        raw="10v05",
    ):
        pytest.skip(
            f"GS requires 10v05+; device reports {hardware_firmware.raw}",
        )

    ctx = DecodeContext(unit_id=hardware_unit_id, firmware=hardware_firmware)
    cmd = GAS_SELECT.encode(ctx, GasSelectRequest())
    raw = await hardware_client.query_line(cmd)
    state = GAS_SELECT.decode(raw, ctx)

    assert state.unit_id == hardware_unit_id
    assert state.code >= 0
    assert state.label  # non-empty
    assert state.long_name

    # Print for easy eyeballing under `pytest -s`.
    print(  # noqa: T201
        f"\n[hardware] unit={state.unit_id} "
        f"gas={state.gas.value} (code {state.code}) {state.long_name!r}",
    )
