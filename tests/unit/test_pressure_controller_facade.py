"""Tests for :class:`alicatlib.devices.pressure_controller.PressureController`.

``setpoint`` / ``setpoint_source`` / ``loop_control_variable`` are
hoisted onto a shared
:class:`~alicatlib.devices._controller._ControllerMixin`. The
flow-controller versions are covered by ``test_device_facade.py``; this
file pins the *pressure-controller* parity:

- Same three methods are present and dispatch identically.
- Inheritance chain: ``PressureController → PressureMeter → Device``
  with the mixin inserted; ``isinstance`` checks still work both ways.
- LSS cache and BIDIRECTIONAL capability gates fire the same way on a
  pressure-controller instance (the mixin has one implementation).
- Legacy ``S`` dispatch works on V1_V7 firmware (vacuum / absolute
  pressure controllers on older hardware).

Routing (factory model rules) is covered by ``test_factory.py``; this
file stops at the facade layer.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import Capability
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.base import Device
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.devices.models import DeviceInfo
from alicatlib.devices.pressure_controller import PressureController
from alicatlib.devices.pressure_meter import PressureMeter
from alicatlib.devices.session import Session
from alicatlib.errors import (
    AlicatUnsupportedCommandError,
    AlicatValidationError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import LoopControlVariable, Statistic
from alicatlib.transport import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pressure_info(
    firmware: str = "10v05",
    capabilities: Capability = Capability.NONE,
) -> DeviceInfo:
    """Pressure-controller info — typical PC- / EPC- shape."""
    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model="PC-100PSIA-D",
        serial="200001",
        manufactured="2021-03-01",
        calibrated="2021-04-01",
        calibrated_by="ACS",
        software=firmware,
        firmware=FirmwareVersion.parse(firmware),
        firmware_date=date(2021, 5, 19),
        kind=DeviceKind.PRESSURE_CONTROLLER,
        media=Medium.GAS,
        capabilities=capabilities,
    )


def _pc_frame_format() -> DataFrameFormat:
    """Pressure-controller data-frame format — minimal shape for legacy S tests.

    Real PC devices report ``Abs_Press`` / ``Gauge_Press`` / ``Setpoint``.
    Only the ``Setpoint`` column is load-bearing for the facade
    (legacy ``S`` wraps the post-op frame into a ``SetpointState`` via
    the shared ``_build_setpoint_state`` helper).
    """

    def _text(value: str) -> float | str | None:
        return value

    def _decimal(value: str) -> float | str | None:
        return parse_optional_float(value, field="decimal")

    names = [
        ("Unit_ID", "text", _text, Statistic.NONE),
        ("Abs_Press", "decimal", _decimal, Statistic.ABS_PRESS),
        ("Setpoint", "decimal", _decimal, Statistic.SETPT),
    ]
    return DataFrameFormat(
        fields=tuple(
            DataFrameField(
                name=n,
                raw_name=n,
                type_name=t,
                statistic=s,
                unit=None,
                conditional=False,
                parser=p,
            )
            for n, t, p, s in names
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


async def _make_session(
    script: Mapping[bytes, ScriptedReply] | None = None,
    *,
    firmware: str = "10v05",
    capabilities: Capability = Capability.NONE,
    with_frame_format: bool = True,
) -> Session:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    return Session(
        client,
        unit_id="A",
        info=_pressure_info(firmware=firmware, capabilities=capabilities),
        data_frame_format=_pc_frame_format() if with_frame_format else None,
    )


# ---------------------------------------------------------------------------
# Inheritance / isinstance contract
# ---------------------------------------------------------------------------


class TestInheritance:
    @pytest.mark.anyio
    async def test_is_pressure_meter_and_device(self) -> None:
        """Diamond MRO must preserve the meter + base-device branches."""
        session = await _make_session()
        dev = PressureController(session)
        assert isinstance(dev, PressureMeter)
        assert isinstance(dev, Device)

    @pytest.mark.anyio
    async def test_exposes_controller_methods(self) -> None:
        """The shared mixin methods must be visible on the class."""
        assert callable(PressureController.setpoint)
        assert callable(PressureController.setpoint_source)
        assert callable(PressureController.loop_control_variable)

    @pytest.mark.anyio
    async def test_single_device_init(self) -> None:
        """Diamond shouldn't double-initialise ``Device`` state."""
        session = await _make_session()
        dev = PressureController(session)
        assert dev.session is session
        assert dev.unit_id == "A"


# ---------------------------------------------------------------------------
# Setpoint dispatch — modern and legacy paths
# ---------------------------------------------------------------------------


class TestSetpointFacade:
    @pytest.mark.anyio
    async def test_modern_set_returns_setpoint_state(self) -> None:
        """Modern ``LS`` reply is 5 fields; same decoder as flow-controller path."""
        session = await _make_session(
            {b"ALS 50.0\r": b"A +49.9 +50.0 2 PSIA\r"},
        )
        dev = PressureController(session)
        state = await dev.setpoint(50.0)
        assert state.unit_id == "A"
        assert state.requested == 50.0
        assert state.current == 49.9
        assert state.unit_label == "PSIA"
        assert state.frame is None  # modern path carries no post-op frame

    @pytest.mark.anyio
    async def test_modern_query(self) -> None:
        session = await _make_session(
            {b"ALS\r": b"A +30.0 +30.0 2 PSIA\r"},
        )
        dev = PressureController(session)
        state = await dev.setpoint()
        assert state.requested == 30.0

    @pytest.mark.anyio
    async def test_legacy_dispatch_on_v7(self) -> None:
        """Pre-9v00 firmware → legacy ``S`` path on pressure controllers too."""
        session = await _make_session(
            {b"AS 30.0\r": b"A +30.0 +30.0\r"},
            firmware="7v99",
        )
        dev = PressureController(session)
        state = await dev.setpoint(30.0)
        # Legacy wraps the post-op frame into a SetpointState using the
        # shared ``_build_setpoint_state`` helper — ``current`` and
        # ``requested`` both come from the ``Setpoint`` column.
        assert state.requested == 30.0
        assert state.frame is not None

    @pytest.mark.anyio
    async def test_legacy_query_raises(self) -> None:
        session = await _make_session(firmware="7v99")
        dev = PressureController(session)
        with pytest.raises(AlicatUnsupportedCommandError):
            await dev.setpoint()

    @pytest.mark.anyio
    async def test_negative_without_bidirectional_raises(self) -> None:
        """Vacuum targets on a unidirectional pressure controller fail pre-I/O."""
        session = await _make_session()
        dev = PressureController(session)
        with pytest.raises(AlicatValidationError) as ei:
            await dev.setpoint(-5.0)
        assert "BIDIRECTIONAL" in str(ei.value)

    @pytest.mark.anyio
    async def test_negative_with_bidirectional_is_accepted(self) -> None:
        """A vacuum PC advertising BIDIRECTIONAL dispatches negative setpoints."""
        session = await _make_session(
            {b"ALS -5.0\r": b"A -5.0 -5.0 2 PSIG\r"},
            capabilities=Capability.BIDIRECTIONAL,
        )
        dev = PressureController(session)
        state = await dev.setpoint(-5.0)
        assert state.requested == -5.0

    @pytest.mark.anyio
    async def test_lss_cached_analog_blocks_serial_setpoint(self) -> None:
        """LSS=A cached → setpoint write refuses pre-I/O (no wire bytes)."""
        session = await _make_session()
        session.update_setpoint_source("A")
        dev = PressureController(session)
        with pytest.raises(AlicatValidationError) as ei:
            await dev.setpoint(30.0)
        assert "analog" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# setpoint_source — cache update parity with FlowController
# ---------------------------------------------------------------------------


class TestSetpointSourceFacade:
    @pytest.mark.anyio
    async def test_query_updates_cache(self) -> None:
        session = await _make_session({b"ALSS\r": b"A S\r"})
        assert session.setpoint_source is None
        dev = PressureController(session)
        mode = await dev.setpoint_source()
        assert mode == "S"
        assert session.setpoint_source == "S"

    @pytest.mark.anyio
    async def test_set_updates_cache(self) -> None:
        session = await _make_session({b"ALSS A\r": b"A A\r"})
        dev = PressureController(session)
        mode = await dev.setpoint_source("A")
        assert mode == "A"
        assert session.setpoint_source == "A"


# ---------------------------------------------------------------------------
# loop_control_variable — cache update parity with FlowController
# ---------------------------------------------------------------------------


class TestLoopControlVariableFacade:
    @pytest.mark.anyio
    async def test_query_updates_cache(self) -> None:
        """Pressure-controller LV typically reports ABS_PRESS_SETPT or GAUGE_PRESS_SETPT."""
        session = await _make_session({b"ALV\r": b"A 37\r"})
        assert session.loop_control_variable is None
        dev = PressureController(session)
        state = await dev.loop_control_variable()
        # Shared decoder — 37 is MASS_FLOW_SETPT; the scope of this test
        # is facade plumbing, not what a real PC advertises. LV eligibility
        # by statistic is covered in test_loop_control_command.py.
        assert state.variable is LoopControlVariable.MASS_FLOW_SETPT
        assert session.loop_control_variable is LoopControlVariable.MASS_FLOW_SETPT

    @pytest.mark.anyio
    async def test_set_updates_cache(self) -> None:
        session = await _make_session({b"ALV 37\r": b"A 37\r"})
        dev = PressureController(session)
        state = await dev.loop_control_variable(LoopControlVariable.MASS_FLOW_SETPT)
        assert state.variable is LoopControlVariable.MASS_FLOW_SETPT
        assert session.loop_control_variable is LoopControlVariable.MASS_FLOW_SETPT
