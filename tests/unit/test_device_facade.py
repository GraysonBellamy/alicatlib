"""Tests for :class:`alicatlib.devices.base.Device` and its subclasses.

The Device façades are thin shells over :class:`Session`; these tests
pin the basics (info/unit_id pass-through, poll/execute delegation,
gas() only on FlowMeter/FlowController, context-manager close) without
re-testing the session's gating logic that ``test_session.py`` covers.
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
from alicatlib.devices.flow_controller import FlowController
from alicatlib.devices.flow_meter import FlowMeter
from alicatlib.devices.models import DeviceInfo
from alicatlib.devices.session import Session
from alicatlib.errors import (
    AlicatUnsupportedCommandError,
    AlicatValidationError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.protocol.parser import parse_optional_float
from alicatlib.registry import Gas, Statistic, Unit
from alicatlib.transport import FakeTransport
from tests._typing import approx

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _info(
    model: str = "MC-100SCCM-D",
    firmware: str = "10v05",
) -> DeviceInfo:
    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model=model,
        serial="123456",
        manufactured="2021-01-01",
        calibrated="2021-02-01",
        calibrated_by="ACS",
        software=firmware,
        firmware=FirmwareVersion.parse(firmware),
        firmware_date=date(2021, 5, 19),
        kind=DeviceKind.FLOW_CONTROLLER,
        media=Medium.GAS,
        capabilities=Capability.NONE,
    )


def _mc_frame_format() -> DataFrameFormat:
    """Minimal data-frame format matching ``dataframe_format_mc.txt``.

    Shared with legacy-gas-select facade tests — legacy G replies with
    a post-op data frame, so the session's cached format must be in
    place for the facade to round-trip.
    """

    def _text(value: str) -> float | str | None:
        return value

    def _decimal(value: str) -> float | str | None:
        return parse_optional_float(value, field="decimal")

    names = [
        ("Unit_ID", "text", _text, Statistic.NONE),
        ("Abs_Press", "decimal", _decimal, Statistic.ABS_PRESS),
        ("Flow_Temp", "decimal", _decimal, Statistic.TEMP_STREAM),
        ("Vol_Flow", "decimal", _decimal, Statistic.VOL_FLOW),
        ("Mass_Flow", "decimal", _decimal, Statistic.MASS_FLOW),
        ("Setpoint", "decimal", _decimal, Statistic.SETPT),
        ("Gas_Label", "text", _text, None),
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
    with_frame_format: bool = False,
) -> Session:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    return Session(
        client,
        unit_id="A",
        info=_info(firmware=firmware),
        data_frame_format=_mc_frame_format() if with_frame_format else None,
    )


# ---------------------------------------------------------------------------
# Device base
# ---------------------------------------------------------------------------


class TestDevice:
    @pytest.mark.anyio
    async def test_info_passthrough(self) -> None:
        session = await _make_session()
        dev = Device(session)
        assert dev.info.model == "MC-100SCCM-D"
        assert dev.unit_id == "A"

    @pytest.mark.anyio
    async def test_session_property_exposes_inner_session(self) -> None:
        session = await _make_session()
        dev = Device(session)
        assert dev.session is session

    @pytest.mark.anyio
    async def test_close_marks_session_closed(self) -> None:
        session = await _make_session()
        dev = Device(session)
        await dev.close()
        assert session.closed

    @pytest.mark.anyio
    async def test_async_context_manager(self) -> None:
        session = await _make_session()
        async with Device(session) as dev:
            assert not session.closed
            assert dev.unit_id == "A"
        assert session.closed

    @pytest.mark.anyio
    async def test_gas_on_bare_device(self) -> None:
        """``gas`` lives on :class:`Device`, not :class:`FlowMeter`.

        Pins the cycle-resolution refactor: if someone silos this
        method back onto ``FlowMeter``, instantiating a bare ``Device``
        would lose the method and this test would regress.
        """
        session = await _make_session({b"AGS\r": b"A 8 N2 Nitrogen\r"})
        dev = Device(session)
        state = await dev.gas()
        assert state.gas is Gas.N2

    @pytest.mark.anyio
    async def test_engineering_units_on_bare_device(self) -> None:
        """Same pin, for the ``engineering_units`` method."""
        session = await _make_session(
            {b"ADCU 5\r": b"A 12 SCCM\r"},
            with_frame_format=True,
        )
        dev = Device(session)
        setting = await dev.engineering_units(Statistic.MASS_FLOW)
        assert setting.unit is Unit.SCCM


# ---------------------------------------------------------------------------
# FlowMeter — empty pass-through (design §5.9)
# ---------------------------------------------------------------------------


class TestFlowMeter:
    @pytest.mark.anyio
    async def test_gas_query_form(self) -> None:
        session = await _make_session({b"AGS\r": b"A 8 N2 Nitrogen\r"})
        dev = FlowMeter(session)
        state = await dev.gas()
        assert state.gas is Gas.N2

    @pytest.mark.anyio
    async def test_gas_set_form(self) -> None:
        session = await _make_session({b"AGS 8\r": b"A 8 N2 Nitrogen\r"})
        dev = FlowMeter(session)
        state = await dev.gas(Gas.N2)
        assert state.gas is Gas.N2

    @pytest.mark.anyio
    async def test_gas_set_with_save(self) -> None:
        session = await _make_session({b"AGS 8 1\r": b"A 8 N2 Nitrogen\r"})
        dev = FlowMeter(session)
        state = await dev.gas(Gas.N2, save=True)
        assert state.gas is Gas.N2

    @pytest.mark.anyio
    async def test_gas_legacy_dispatch_on_v8(self) -> None:
        """Pre-10v05 firmware → legacy ``G`` path; facade fabricates GasState."""
        session = await _make_session(
            {b"AG 8\r": b"A +14.70 +25.0 +25.5 +25.5 +50.0 N2\r"},
            firmware="8v33",
            with_frame_format=True,
        )
        dev = FlowMeter(session)
        state = await dev.gas(Gas.N2)
        assert state.gas is Gas.N2
        assert state.code == 8
        assert state.unit_id == "A"

    @pytest.mark.anyio
    async def test_gas_legacy_query_raises(self) -> None:
        """Legacy firmware has no query form — facade rejects before I/O."""
        session = await _make_session(firmware="8v33", with_frame_format=True)
        dev = FlowMeter(session)
        with pytest.raises(AlicatUnsupportedCommandError):
            await dev.gas()

    @pytest.mark.anyio
    async def test_gas_legacy_save_true_raises(self) -> None:
        """Legacy G has no persist flag — save=True is a validation error."""
        session = await _make_session(firmware="8v33", with_frame_format=True)
        dev = FlowMeter(session)
        with pytest.raises(AlicatValidationError):
            await dev.gas(Gas.N2, save=True)

    @pytest.mark.anyio
    async def test_gas_list(self) -> None:
        reply = b"A G00      Air\rA G08       N2\rA G10       O2\r"
        session = await _make_session({b"A??G*\r": reply})
        dev = FlowMeter(session)
        listed = await dev.gas_list()
        assert listed[0] == "Air"
        assert listed[8] == "N2"
        assert listed[10] == "O2"
        assert len(listed) == 3


# ---------------------------------------------------------------------------
# Device.engineering_units + Device.full_scale
# ---------------------------------------------------------------------------


class TestEngineeringUnits:
    @pytest.mark.anyio
    async def test_query_does_not_invalidate_cache(self) -> None:
        """A DCU query returns the current unit without reshaping the frame."""
        session = await _make_session(
            {b"ADCU 5\r": b"A 12 SCCM\r"},
            with_frame_format=True,
        )
        dev = FlowMeter(session)
        setting = await dev.engineering_units(Statistic.MASS_FLOW)
        assert setting.unit is Unit.SCCM
        # Cache still populated — query does not reshape the data frame.
        assert session.data_frame_format is not None

    @pytest.mark.anyio
    async def test_set_invalidates_data_frame_cache(self) -> None:
        """Setting a unit invalidates the session's cached ??D* format.

        The next poll must re-probe ``??D*`` because unit labels (e.g.
        ``SLPM`` vs ``SCCM``) surface in the frame.
        """
        session = await _make_session(
            {b"ADCU 5 7\r": b"A 7 SLPM\r"},
            with_frame_format=True,
        )
        assert session.data_frame_format is not None
        dev = FlowMeter(session)
        await dev.engineering_units(Statistic.MASS_FLOW, Unit.SLPM)
        assert session.data_frame_format is None

    @pytest.mark.anyio
    async def test_set_with_group_flag(self) -> None:
        session = await _make_session(
            {b"ADCU 5 7 1\r": b"A 7 SLPM\r"},
            with_frame_format=True,
        )
        dev = FlowMeter(session)
        setting = await dev.engineering_units(
            Statistic.MASS_FLOW,
            Unit.SLPM,
            apply_to_group=True,
        )
        assert setting.unit is Unit.SLPM


class TestFullScale:
    @pytest.mark.anyio
    async def test_full_scale_query(self) -> None:
        session = await _make_session({b"AFPF 5\r": b"A 100.0 12 SCCM\r"})
        dev = FlowMeter(session)
        fs = await dev.full_scale(Statistic.MASS_FLOW)
        assert fs.value == 100.0
        assert fs.unit is Unit.SCCM
        assert fs.statistic is Statistic.MASS_FLOW


# ---------------------------------------------------------------------------
# Device.tare_*
# ---------------------------------------------------------------------------


class TestTareFacade:
    @pytest.mark.anyio
    async def test_tare_flow_returns_data_frame(self) -> None:
        session = await _make_session(
            {b"AT\r": b"A +14.70 +25.0 +0.0 +0.0 +50.0 N2\r"},
            with_frame_format=True,
        )
        dev = Device(session)
        result = await dev.tare_flow()
        assert result.frame.values["Mass_Flow"] == 0.0
        assert result.frame.values["Gas_Label"] == "N2"
        # DataFrame wrapping captures timing at facade level.
        assert result.frame.received_at is not None
        assert result.frame.monotonic_ns > 0

    @pytest.mark.anyio
    async def test_tare_flow_emits_precondition_info_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """INFO log names the unverifiable precondition (design §5.18 pt 6)."""
        session = await _make_session(
            {b"AT\r": b"A +14.70 +25.0 +0.0 +0.0 +50.0 N2\r"},
            with_frame_format=True,
        )
        dev = Device(session)
        with caplog.at_level("INFO", logger="alicatlib.session"):
            await dev.tare_flow()
        assert any(
            "no gas is flowing" in rec.message
            for rec in caplog.records
            if rec.name == "alicatlib.session"
        )

    @pytest.mark.anyio
    async def test_tare_gauge_pressure(self) -> None:
        session = await _make_session(
            {b"ATP\r": b"A +14.70 +25.0 +25.5 +25.5 +50.0 N2\r"},
            with_frame_format=True,
        )
        dev = Device(session)
        result = await dev.tare_gauge_pressure()
        assert result.frame.values["Abs_Press"] == 14.70

    @pytest.mark.anyio
    async def test_tare_absolute_pressure_requires_tareable_abs_pressure(self) -> None:
        """Without Capability.TAREABLE_ABSOLUTE_PRESSURE the session rejects pre-I/O.

        Note the gate switched from ``BAROMETER`` to
        ``TAREABLE_ABSOLUTE_PRESSURE`` on 2026-04-17 after
        four flow-controller devices probed ``BAROMETER`` positive yet
        rejected ``PC`` (design §16.6.7).
        """
        from alicatlib.errors import AlicatMissingHardwareError

        session = await _make_session(
            firmware="10v05",
            with_frame_format=True,
        )
        dev = Device(session)
        with pytest.raises(AlicatMissingHardwareError):
            await dev.tare_absolute_pressure()

    @pytest.mark.anyio
    async def test_tare_absolute_pressure_with_tareable_abs_pressure(self) -> None:
        """Session advertising TAREABLE_ABSOLUTE_PRESSURE → PC succeeds."""
        fake = FakeTransport(
            {b"APC\r": b"A +14.70 +25.0 +25.5 +25.5 +50.0 N2\r"},
            label="fake://test",
        )
        await fake.open()
        client = AlicatProtocolClient(
            fake,
            multiline_idle_timeout=0.01,
            default_timeout=0.1,
        )
        info = _info()
        info_with_cap = DeviceInfo(
            unit_id=info.unit_id,
            manufacturer=info.manufacturer,
            model=info.model,
            serial=info.serial,
            manufactured=info.manufactured,
            calibrated=info.calibrated,
            calibrated_by=info.calibrated_by,
            software=info.software,
            firmware=info.firmware,
            firmware_date=info.firmware_date,
            kind=info.kind,
            media=info.media,
            capabilities=Capability.TAREABLE_ABSOLUTE_PRESSURE,
        )
        session = Session(
            client,
            unit_id="A",
            info=info_with_cap,
            data_frame_format=_mc_frame_format(),
        )
        dev = Device(session)
        result = await dev.tare_absolute_pressure()
        assert result.frame.unit_id == "A"


# ---------------------------------------------------------------------------
# FlowController — inherits everything, no new surface
# ---------------------------------------------------------------------------


class TestFlowController:
    @pytest.mark.anyio
    async def test_inherits_flow_meter_gas(self) -> None:
        session = await _make_session({b"AGS 8\r": b"A 8 N2 Nitrogen\r"})
        dev = FlowController(session)
        state = await dev.gas(Gas.N2)
        assert state.gas is Gas.N2

    @pytest.mark.anyio
    async def test_isinstance_check(self) -> None:
        session = await _make_session()
        dev = FlowController(session)
        assert isinstance(dev, FlowMeter)
        assert isinstance(dev, Device)


# ---------------------------------------------------------------------------
# Setpoint facade
# ---------------------------------------------------------------------------


class TestSetpointFacade:
    @pytest.mark.anyio
    async def test_modern_set_returns_setpoint_state(self) -> None:
        """Modern LS reply is 5 fields (current + requested split on wire)."""
        session = await _make_session(
            {b"ALS 75.0\r": b"A +45.0 +75.0 12 SCCM\r"},
            with_frame_format=True,
        )
        dev = FlowController(session)
        state = await dev.setpoint(75.0)
        assert state.unit_id == "A"
        assert state.requested == 75.0
        assert state.current == 45.0
        assert state.unit is Unit.SCCM
        # Modern path carries no post-op data frame (design §16.6).
        assert state.frame is None

    @pytest.mark.anyio
    async def test_modern_query(self) -> None:
        session = await _make_session(
            {b"ALS\r": b"A +50.0 +50.0 12 SCCM\r"},
            with_frame_format=True,
        )
        dev = FlowController(session)
        state = await dev.setpoint()
        assert state.requested == 50.0
        assert state.current == 50.0

    @pytest.mark.anyio
    async def test_legacy_dispatch_on_v7(self) -> None:
        """Pre-9v00 firmware → legacy ``S`` path."""
        session = await _make_session(
            {b"AS 75.0\r": b"A +14.70 +25.0 +45.0 +45.0 +75.0 N2\r"},
            firmware="7v99",
            with_frame_format=True,
        )
        dev = FlowController(session)
        state = await dev.setpoint(75.0)
        assert state.requested == 75.0

    @pytest.mark.anyio
    async def test_legacy_dispatch_finds_setpoint_by_statistic(self) -> None:
        """Real ``??D*`` names the setpoint column after the controlled variable.

        Observed on 2026-04-17 on MCP-50SLPM-D @ 7v09 and
        MCR-500SLPM-D @ 8v30: the ``??D*`` advertisement produces
        ``"Mass_Flow_Setpt"`` (snake-cased from primer's
        ``Mass Flow Setpt``), not the literal ``"Setpoint"`` that
        test fixtures use. The legacy ``S`` decoder must find the
        setpoint value by ``*_SETPT`` statistic, not by hardcoded name,
        or every legacy-family setpoint write fails to round-trip.
        """
        fake = FakeTransport(
            {b"AS 75.0\r": b"A +14.70 +25.0 +45.0 +45.0 +75.0 N2\r"},
            label="fake://test",
        )
        await fake.open()
        client = AlicatProtocolClient(
            fake,
            multiline_idle_timeout=0.01,
            default_timeout=0.1,
        )

        # Real-hardware-shaped format: setpoint column named
        # ``Mass_Flow_Setpt`` with Statistic.MASS_FLOW_SETPT.
        def _text(v: str) -> float | str | None:
            return v

        def _decimal(v: str) -> float | str | None:
            return parse_optional_float(v, field="decimal")

        real_fmt = DataFrameFormat(
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
                for n, t, p, s in [
                    ("Unit_ID", "text", _text, Statistic.NONE),
                    ("Abs_Press", "decimal", _decimal, Statistic.ABS_PRESS),
                    ("Flow_Temp", "decimal", _decimal, Statistic.TEMP_STREAM),
                    ("Vol_Flow", "decimal", _decimal, Statistic.VOL_FLOW),
                    ("Mass_Flow", "decimal", _decimal, Statistic.MASS_FLOW),
                    ("Mass_Flow_Setpt", "decimal", _decimal, Statistic.MASS_FLOW_SETPT),
                    ("Gas", "text", _text, None),
                ]
            ),
            flavor=DataFrameFormatFlavor.DEFAULT,
        )
        session = Session(
            client,
            unit_id="A",
            info=_info(firmware="7v99"),
            data_frame_format=real_fmt,
        )
        dev = FlowController(session)
        state = await dev.setpoint(75.0)
        assert state.requested == 75.0
        assert state.current == 75.0

    @pytest.mark.anyio
    async def test_legacy_query_raises(self) -> None:
        """Legacy S has no query form — facade rejects pre-I/O."""
        session = await _make_session(firmware="7v99", with_frame_format=True)
        dev = FlowController(session)
        from alicatlib.errors import AlicatUnsupportedCommandError

        with pytest.raises(AlicatUnsupportedCommandError):
            await dev.setpoint()

    @pytest.mark.anyio
    async def test_negative_without_bidirectional_raises(self) -> None:
        session = await _make_session(with_frame_format=True)
        dev = FlowController(session)
        with pytest.raises(AlicatValidationError) as ei:
            await dev.setpoint(-25.0)
        assert "BIDIRECTIONAL" in str(ei.value)

    @pytest.mark.anyio
    async def test_negative_with_bidirectional_is_accepted(self) -> None:
        """A device advertising BIDIRECTIONAL dispatches without validation error."""
        fake = FakeTransport(
            {b"ALS -25.0\r": b"A -25.0 -25.0 12 SCCM\r"},
            label="fake://test",
        )
        await fake.open()
        client = AlicatProtocolClient(
            fake,
            multiline_idle_timeout=0.01,
            default_timeout=0.1,
        )
        info = _info()
        bidirectional_info = DeviceInfo(
            unit_id=info.unit_id,
            manufacturer=info.manufacturer,
            model=info.model,
            serial=info.serial,
            manufactured=info.manufactured,
            calibrated=info.calibrated,
            calibrated_by=info.calibrated_by,
            software=info.software,
            firmware=info.firmware,
            firmware_date=info.firmware_date,
            kind=info.kind,
            media=info.media,
            capabilities=Capability.BIDIRECTIONAL,
        )
        session = Session(
            client,
            unit_id="A",
            info=bidirectional_info,
            data_frame_format=_mc_frame_format(),
        )
        dev = FlowController(session)
        state = await dev.setpoint(-25.0)
        assert state.requested == -25.0

    @pytest.mark.anyio
    async def test_lss_cached_analog_blocks_serial_setpoint(self) -> None:
        """LSS=A cached → setpoint write raises pre-I/O (no bytes emitted)."""
        session = await _make_session(with_frame_format=True)
        session.update_setpoint_source("A")
        dev = FlowController(session)
        with pytest.raises(AlicatValidationError) as ei:
            await dev.setpoint(50.0)
        assert "analog" in str(ei.value).lower()

    @pytest.mark.anyio
    async def test_lss_cached_serial_allows_write(self) -> None:
        session = await _make_session(
            {b"ALS 50.0\r": b"A +50.0 +50.0 12 SCCM\r"},
            with_frame_format=True,
        )
        session.update_setpoint_source("S")
        dev = FlowController(session)
        state = await dev.setpoint(50.0)
        assert state.requested == 50.0


class TestSetpointSourceFacade:
    @pytest.mark.anyio
    async def test_query_updates_cache(self) -> None:
        session = await _make_session({b"ALSS\r": b"A S\r"})
        assert session.setpoint_source is None
        dev = FlowController(session)
        mode = await dev.setpoint_source()
        assert mode == "S"
        assert session.setpoint_source == "S"

    @pytest.mark.anyio
    async def test_set_updates_cache(self) -> None:
        session = await _make_session({b"ALSS A\r": b"A A\r"})
        dev = FlowController(session)
        mode = await dev.setpoint_source("A")
        assert mode == "A"
        assert session.setpoint_source == "A"


class TestLoopControlVariableFacade:
    @pytest.mark.anyio
    async def test_query(self) -> None:
        session = await _make_session({b"ALV\r": b"A 37\r"})
        dev = FlowController(session)
        state = await dev.loop_control_variable()
        from alicatlib.registry import LoopControlVariable

        assert state.variable is LoopControlVariable.MASS_FLOW_SETPT

    @pytest.mark.anyio
    async def test_set_by_enum(self) -> None:
        from alicatlib.registry import LoopControlVariable

        session = await _make_session({b"ALV 36\r": b"A 36\r"})
        dev = FlowController(session)
        state = await dev.loop_control_variable(LoopControlVariable.VOL_FLOW_SETPT)
        assert state.variable is LoopControlVariable.VOL_FLOW_SETPT

    @pytest.mark.anyio
    async def test_set_by_name_string(self) -> None:
        from alicatlib.registry import LoopControlVariable

        session = await _make_session({b"ALV 37\r": b"A 37\r"})
        dev = FlowController(session)
        state = await dev.loop_control_variable("mass_flow_setpt")
        assert state.variable is LoopControlVariable.MASS_FLOW_SETPT

    @pytest.mark.anyio
    async def test_query_updates_session_cache(self) -> None:
        """Every LV call refreshes :attr:`Session.loop_control_variable`."""
        from alicatlib.registry import LoopControlVariable

        session = await _make_session({b"ALV\r": b"A 37\r"})
        assert session.loop_control_variable is None
        dev = FlowController(session)
        await dev.loop_control_variable()
        assert session.loop_control_variable is LoopControlVariable.MASS_FLOW_SETPT

    @pytest.mark.anyio
    async def test_set_updates_session_cache(self) -> None:
        """``LV <code>`` write refreshes the cache so setpoint range-checks the new variable."""
        from alicatlib.registry import LoopControlVariable

        session = await _make_session({b"ALV 36\r": b"A 36\r"})
        dev = FlowController(session)
        await dev.loop_control_variable(LoopControlVariable.VOL_FLOW_SETPT)
        assert session.loop_control_variable is LoopControlVariable.VOL_FLOW_SETPT


# ---------------------------------------------------------------------------
# Setpoint full-scale range validation (design §5.20.2)
# ---------------------------------------------------------------------------


def _info_with_full_scale(
    *,
    stat: Statistic,
    full_scale_value: float,
    unit_label: str = "SCCM",
    capabilities: Capability = Capability.NONE,
) -> DeviceInfo:
    from types import MappingProxyType

    from alicatlib.devices.models import FullScaleValue

    base = _info()
    full_scale = MappingProxyType(
        {
            stat: FullScaleValue(
                statistic=stat,
                value=full_scale_value,
                unit=None,
                unit_label=unit_label,
            ),
        },
    )
    return DeviceInfo(
        unit_id=base.unit_id,
        manufacturer=base.manufacturer,
        model=base.model,
        serial=base.serial,
        manufactured=base.manufactured,
        calibrated=base.calibrated,
        calibrated_by=base.calibrated_by,
        software=base.software,
        firmware=base.firmware,
        firmware_date=base.firmware_date,
        kind=base.kind,
        media=base.media,
        capabilities=capabilities,
        probe_report=base.probe_report,
        full_scale=full_scale,
    )


async def _make_controller_session(
    script: Mapping[bytes, ScriptedReply] | None = None,
    *,
    info: DeviceInfo | None = None,
) -> Session:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    return Session(
        client,
        unit_id="A",
        info=info if info is not None else _info(),
        data_frame_format=_mc_frame_format(),
    )


class TestSetpointFullScaleValidation:
    """Pre-I/O range check against the FPF-probed full-scale."""

    @pytest.mark.anyio
    async def test_in_range_setpoint_dispatches(self) -> None:
        from alicatlib.registry import LoopControlVariable

        info = _info_with_full_scale(stat=Statistic.MASS_FLOW_SETPT, full_scale_value=100.0)
        session = await _make_controller_session(
            {b"ALS 50.0\r": b"A +50.0 +50.0 12 SCCM\r"},
            info=info,
        )
        session.update_loop_control_variable(LoopControlVariable.MASS_FLOW_SETPT)
        dev = FlowController(session)
        state = await dev.setpoint(50.0)
        assert state.requested == approx(50.0)

    @pytest.mark.anyio
    async def test_above_full_scale_raises_pre_io(self) -> None:
        from alicatlib.registry import LoopControlVariable

        info = _info_with_full_scale(stat=Statistic.MASS_FLOW_SETPT, full_scale_value=100.0)
        session = await _make_controller_session(info=info)
        session.update_loop_control_variable(LoopControlVariable.MASS_FLOW_SETPT)
        dev = FlowController(session)
        with pytest.raises(AlicatValidationError) as ei:
            await dev.setpoint(150.0)
        msg = str(ei.value)
        assert "full-scale" in msg
        assert "150.0" in msg
        # No wire bytes emitted — the check is pre-I/O.
        transport = session._client.transport  # pyright: ignore[reportPrivateUsage]
        assert isinstance(transport, FakeTransport)
        assert transport.writes == ()

    @pytest.mark.anyio
    async def test_below_zero_raises_on_unidirectional(self) -> None:
        """Unidirectional device: valid range is ``[0, +full_scale]``.

        The BIDIRECTIONAL gate fires first, so a negative value raises
        that error and never reaches the full-scale check. Still worth
        pinning that zero *is* in-range for unidirectional devices.
        """
        from alicatlib.registry import LoopControlVariable

        info = _info_with_full_scale(stat=Statistic.MASS_FLOW_SETPT, full_scale_value=100.0)
        session = await _make_controller_session(
            {b"ALS 0.0\r": b"A +0.0 +0.0 12 SCCM\r"},
            info=info,
        )
        session.update_loop_control_variable(LoopControlVariable.MASS_FLOW_SETPT)
        dev = FlowController(session)
        state = await dev.setpoint(0.0)
        assert state.requested == approx(0.0)

    @pytest.mark.anyio
    async def test_bidirectional_accepts_negative_in_range(self) -> None:
        from alicatlib.registry import LoopControlVariable

        info = _info_with_full_scale(
            stat=Statistic.MASS_FLOW_SETPT,
            full_scale_value=100.0,
            capabilities=Capability.BIDIRECTIONAL,
        )
        session = await _make_controller_session(
            {b"ALS -50.0\r": b"A -50.0 -50.0 12 SCCM\r"},
            info=info,
        )
        session.update_loop_control_variable(LoopControlVariable.MASS_FLOW_SETPT)
        dev = FlowController(session)
        state = await dev.setpoint(-50.0)
        assert state.requested == approx(-50.0)

    @pytest.mark.anyio
    async def test_bidirectional_rejects_negative_below_minus_full_scale(self) -> None:
        from alicatlib.registry import LoopControlVariable

        info = _info_with_full_scale(
            stat=Statistic.MASS_FLOW_SETPT,
            full_scale_value=100.0,
            capabilities=Capability.BIDIRECTIONAL,
        )
        session = await _make_controller_session(info=info)
        session.update_loop_control_variable(LoopControlVariable.MASS_FLOW_SETPT)
        dev = FlowController(session)
        with pytest.raises(AlicatValidationError) as ei:
            await dev.setpoint(-150.0)
        assert "full-scale" in str(ei.value)

    @pytest.mark.anyio
    async def test_skipped_when_no_lv_cached(self) -> None:
        """No cached LV → skip the range check (no info to pick a FullScaleValue)."""
        info = _info_with_full_scale(stat=Statistic.MASS_FLOW_SETPT, full_scale_value=100.0)
        session = await _make_controller_session(
            {b"ALS 500.0\r": b"A +500.0 +500.0 12 SCCM\r"},
            info=info,
        )
        # No update_loop_control_variable() → cache stays None.
        assert session.loop_control_variable is None
        dev = FlowController(session)
        # Would be out of range if the check fired; since it's skipped,
        # the wire write proceeds.
        state = await dev.setpoint(500.0)
        assert state.requested == approx(500.0)

    @pytest.mark.anyio
    async def test_skipped_when_full_scale_missing_for_lv(self) -> None:
        """LV points at a statistic that isn't in :attr:`full_scale` → skip the check."""
        from alicatlib.registry import LoopControlVariable

        info = _info_with_full_scale(
            stat=Statistic.VOL_FLOW_SETPT,  # full_scale only populated for VOL_FLOW_SETPT
            full_scale_value=50.0,
        )
        session = await _make_controller_session(
            {b"ALS 500.0\r": b"A +500.0 +500.0 12 SCCM\r"},
            info=info,
        )
        # LV points at MASS_FLOW_SETPT, which is NOT in full_scale.
        session.update_loop_control_variable(LoopControlVariable.MASS_FLOW_SETPT)
        dev = FlowController(session)
        state = await dev.setpoint(500.0)
        assert state.requested == approx(500.0)

    @pytest.mark.anyio
    async def test_skipped_on_query_form(self) -> None:
        """Query form (value=None) must not trigger the range check."""
        from alicatlib.registry import LoopControlVariable

        info = _info_with_full_scale(stat=Statistic.MASS_FLOW_SETPT, full_scale_value=100.0)
        session = await _make_controller_session(
            {b"ALS\r": b"A +0.0 +0.0 12 SCCM\r"},
            info=info,
        )
        session.update_loop_control_variable(LoopControlVariable.MASS_FLOW_SETPT)
        dev = FlowController(session)
        state = await dev.setpoint()
        assert state.requested == approx(0.0)
