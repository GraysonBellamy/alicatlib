"""Tests for the observability fills (design §5.19 / §15.2).

Covers the four log sites:

- Setpoint-change INFO on :meth:`FlowController.setpoint` (both
  modern LS and legacy S paths).
- LSS set-event INFO on :meth:`FlowController.setpoint_source`.
- LV set-event INFO on :meth:`FlowController.loop_control_variable`.
- Capability-probe outcome INFO from :func:`probe_capabilities`.

Also pins the firmware-raw preservation fix so the
``.0-R<NN>`` revision suffix reaches sinks and dashboards.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import Capability
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.devices.factory import probe_capabilities
from alicatlib.devices.flow_controller import FlowController
from alicatlib.devices.models import DeviceInfo
from alicatlib.devices.session import Session
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.registry import LoopControlVariable, Statistic
from alicatlib.transport import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _info(
    *,
    firmware: str = "10v20",
    family: FirmwareFamily = FirmwareFamily.V10,
    capabilities: Capability = Capability.NONE,
) -> DeviceInfo:
    # ``FirmwareVersion.parse`` will derive the family from the digits;
    # the ``family`` arg here only matters when a caller wants the raw
    # string to explicitly encode a different family (e.g. GP tests).
    del family
    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model="MC-100SCCM-D",
        serial="1",
        manufactured="2024-01-01",
        calibrated="2024-02-01",
        calibrated_by="ACS",
        software=firmware,
        firmware=FirmwareVersion.parse(firmware),
        firmware_date=date(2024, 1, 1),
        kind=DeviceKind.FLOW_CONTROLLER,
        media=Medium.GAS,
        capabilities=capabilities,
    )


def _identity(v: str) -> float | str | None:
    return v


def _format() -> DataFrameFormat:
    def _decimal(v: str) -> float | str | None:
        return float(v)

    return DataFrameFormat(
        fields=(
            DataFrameField(
                name="Unit_ID",
                raw_name="Unit_ID",
                type_name="text",
                statistic=None,
                unit=None,
                conditional=False,
                parser=_identity,
            ),
            DataFrameField(
                name="Setpoint",
                raw_name="Setpoint",
                type_name="decimal",
                statistic=Statistic.MASS_FLOW_SETPT,
                unit=None,
                conditional=False,
                parser=_decimal,
            ),
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


async def _make_session(
    script: Mapping[bytes, ScriptedReply],
    *,
    firmware: str = "10v20",
    capabilities: Capability = Capability.NONE,
) -> Session:
    fake = FakeTransport(script, label="fake://obs-test")
    await fake.open()
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    return Session(
        client,
        unit_id="A",
        info=_info(firmware=firmware, capabilities=capabilities),
        data_frame_format=_format(),
    )


# ---------------------------------------------------------------------------
# Setpoint-change INFO
# ---------------------------------------------------------------------------


class TestSetpointChangeLog:
    @pytest.mark.anyio
    async def test_modern_setpoint_set_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        # Modern LS reply: 5 fields — uid, current, requested, unit_code, unit_label.
        script = {b"ALS 50.0\r": b"A +50.00 +50.00 12 SCCM\r"}
        session = await _make_session(script)
        dev = FlowController(session)
        with caplog.at_level(logging.INFO, logger="alicatlib.session"):
            await dev.setpoint(50.0)
        change_records = [r for r in caplog.records if r.message == "setpoint_change"]
        assert len(change_records) == 1
        rec = change_records[0]
        assert rec.levelno == logging.INFO
        assert rec.unit_id == "A"  # type: ignore[attr-defined]
        assert rec.value == 50.0  # type: ignore[attr-defined]
        assert rec.path == "modern"  # type: ignore[attr-defined]

    @pytest.mark.anyio
    async def test_query_form_does_not_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """A read-only query shouldn't emit the change event."""
        script = {b"ALS\r": b"A +50.00 +50.00 12 SCCM\r"}
        session = await _make_session(script)
        dev = FlowController(session)
        with caplog.at_level(logging.INFO, logger="alicatlib.session"):
            await dev.setpoint()
        change_records = [r for r in caplog.records if r.message == "setpoint_change"]
        assert not change_records


# ---------------------------------------------------------------------------
# LSS set-event INFO
# ---------------------------------------------------------------------------


class TestSetpointSourceLog:
    @pytest.mark.anyio
    async def test_lss_set_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        script = {b"ALSS S\r": b"A S\r"}
        session = await _make_session(script)
        dev = FlowController(session)
        with caplog.at_level(logging.INFO, logger="alicatlib.session"):
            await dev.setpoint_source("S")
        change_records = [r for r in caplog.records if r.message == "setpoint_source_change"]
        assert len(change_records) == 1
        rec = change_records[0]
        assert rec.requested_mode == "S"  # type: ignore[attr-defined]

    @pytest.mark.anyio
    async def test_lss_query_does_not_log(self, caplog: pytest.LogCaptureFixture) -> None:
        script = {b"ALSS\r": b"A S\r"}
        session = await _make_session(script)
        dev = FlowController(session)
        with caplog.at_level(logging.INFO, logger="alicatlib.session"):
            await dev.setpoint_source()
        change_records = [r for r in caplog.records if r.message == "setpoint_source_change"]
        assert not change_records


# ---------------------------------------------------------------------------
# LV set-event INFO
# ---------------------------------------------------------------------------


class TestLoopControlVariableLog:
    @pytest.mark.anyio
    async def test_lv_set_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        # LV set wire: ``ALV <code>`` → reply echoes ``<uid> <code>``.
        script = {b"ALV 37\r": b"A 37\r"}
        session = await _make_session(script)
        dev = FlowController(session)
        with caplog.at_level(logging.INFO, logger="alicatlib.session"):
            await dev.loop_control_variable(LoopControlVariable.MASS_FLOW_SETPT)
        change_records = [r for r in caplog.records if r.message == "loop_control_variable_change"]
        assert len(change_records) == 1
        rec = change_records[0]
        assert rec.unit_id == "A"  # type: ignore[attr-defined]
        assert "MASS_FLOW_SETPT" in rec.requested_variable  # type: ignore[attr-defined]

    @pytest.mark.anyio
    async def test_lv_query_does_not_log(self, caplog: pytest.LogCaptureFixture) -> None:
        script = {b"ALV\r": b"A 37\r"}
        session = await _make_session(script)
        dev = FlowController(session)
        with caplog.at_level(logging.INFO, logger="alicatlib.session"):
            await dev.loop_control_variable()
        change_records = [r for r in caplog.records if r.message == "loop_control_variable_change"]
        assert not change_records


# ---------------------------------------------------------------------------
# Capability-probe outcome INFO
# ---------------------------------------------------------------------------


class TestCapabilityProbeLog:
    @pytest.mark.anyio
    async def test_probe_emits_summary_info(self, caplog: pytest.LogCaptureFixture) -> None:
        # Empty script → every FPF probe times out → fail-closed "absent".
        # That's fine for the log assertion — we just want the summary event.
        fake = FakeTransport({}, label="fake://probe")
        await fake.open()
        client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.02)
        with caplog.at_level(logging.INFO, logger="alicatlib.session"):
            await probe_capabilities(client, "A", _info())
        summary_records = [r for r in caplog.records if r.message == "probe_capabilities.result"]
        assert len(summary_records) == 1
        rec = summary_records[0]
        assert rec.unit_id == "A"  # type: ignore[attr-defined]
        assert isinstance(rec.outcomes, dict)  # type: ignore[attr-defined]
        # Every outcome should be one of the ProbeOutcome literals.
        valid_outcomes = {"present", "absent", "timeout", "rejected", "parse_error"}
        for outcome in rec.outcomes.values():  # type: ignore[attr-defined]
            assert outcome in valid_outcomes

    @pytest.mark.anyio
    async def test_gp_probe_emits_skip_event(self, caplog: pytest.LogCaptureFixture) -> None:
        """GP family short-circuits — logs the skip reason explicitly."""
        fake = FakeTransport({}, label="fake://gp-probe")
        await fake.open()
        client = AlicatProtocolClient(fake)
        gp_info = DeviceInfo(
            unit_id="A",
            manufacturer="Alicat",
            model="MC-100SCCM-D",
            serial="1",
            manufactured="2020-01-01",
            calibrated=None,
            calibrated_by=None,
            software="GP",
            firmware=FirmwareVersion.parse("GP"),
            firmware_date=None,
            kind=DeviceKind.FLOW_CONTROLLER,
            media=Medium.GAS,
            capabilities=Capability.NONE,
        )
        with caplog.at_level(logging.INFO, logger="alicatlib.session"):
            caps, report = await probe_capabilities(client, "A", gp_info)
        assert caps is Capability.NONE
        skip_records = [r for r in caplog.records if r.message == "probe_capabilities.gp_skip"]
        assert len(skip_records) == 1
        rec = skip_records[0]
        assert rec.reason == "gp_family_no_fpf"  # type: ignore[attr-defined]
        # Report is still populated (everything "absent"), not empty.
        assert all(v == "absent" for v in report.values())


# ---------------------------------------------------------------------------
# Firmware raw preservation
# ---------------------------------------------------------------------------


class TestFirmwareRawPreservation:
    def test_v10_r_suffix_preserved(self) -> None:
        fw = FirmwareVersion.parse("10v20.0-R24")
        assert fw.raw == "10v20.0-R24"
        assert (fw.major, fw.minor) == (10, 20)

    def test_v8_v9_r_suffix_preserved(self) -> None:
        fw = FirmwareVersion.parse("8v17.0-R23")
        assert fw.raw == "8v17.0-R23"

    def test_bare_version_stays_unchanged(self) -> None:
        """Devices without the ``.N-RNN`` suffix still parse cleanly."""
        fw = FirmwareVersion.parse("10v05")
        assert fw.raw == "10v05"
