"""Tests for :mod:`alicatlib.devices.models`.

These models are data carriers — the tests pin value stability (status
codes appear in telemetry and logs, so renaming them would be a breaking
change), frozen-ness (the session caches these), and default shapes.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime

import pytest

from alicatlib.commands.base import Capability
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.models import (
    DeviceInfo,
    FullScaleValue,
    MeasurementSet,
    StatusCode,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.registry._codes_gen import Statistic, Unit


class TestStatusCode:
    def test_values_are_three_letter_codes(self) -> None:
        """Pin the exact wire values — these appear in data frames and telemetry."""
        assert StatusCode.ADC.value == "ADC"
        assert StatusCode.EXH.value == "EXH"
        assert StatusCode.HLD.value == "HLD"
        assert StatusCode.LCK.value == "LCK"
        assert StatusCode.MOV.value == "MOV"
        assert StatusCode.OPL.value == "OPL"
        assert StatusCode.OVR.value == "OVR"
        assert StatusCode.POV.value == "POV"
        assert StatusCode.TMF.value == "TMF"
        assert StatusCode.TOV.value == "TOV"
        assert StatusCode.VOV.value == "VOV"

    def test_all_values_are_three_letters(self) -> None:
        """Guard against accidental members of the wrong shape."""
        for code in StatusCode:
            assert len(code.value) == 3
            assert code.value.isupper()
            assert code.value.isalpha()

    def test_count(self) -> None:
        """Eleven codes per Alicat primer; pin the count."""
        assert len(list(StatusCode)) == 11

    def test_is_str(self) -> None:
        """StrEnum means `StatusCode.HLD == "HLD"` — useful for wire matching."""
        assert StatusCode.HLD == "HLD"


class TestFullScaleValue:
    def test_construct(self) -> None:
        fsv = FullScaleValue(
            statistic=Statistic.MASS_FLOW,
            value=100.0,
            unit=Unit.SCCM,
            unit_label="SCCM",
        )
        assert fsv.statistic is Statistic.MASS_FLOW
        assert fsv.value == 100.0

    def test_unit_can_be_none(self) -> None:
        """Unknown unit label still round-trips; `unit_label` preserves the raw string."""
        fsv = FullScaleValue(
            statistic=Statistic.MASS_FLOW,
            value=10.0,
            unit=None,
            unit_label="mysterious",
        )
        assert fsv.unit is None
        assert fsv.unit_label == "mysterious"

    def test_is_frozen(self) -> None:
        fsv = FullScaleValue(statistic=Statistic.MASS_FLOW, value=1.0, unit=None, unit_label="x")
        with pytest.raises(FrozenInstanceError):
            fsv.value = 2.0  # type: ignore[misc]


class TestMeasurementSet:
    def test_construct(self) -> None:
        ms = MeasurementSet(
            unit_id="A",
            values={Statistic.MASS_FLOW: 1.5, Statistic.ABS_PRESS: None},
            averaging_ms=10,
            received_at=datetime(2026, 4, 16, tzinfo=UTC),
        )
        assert ms.values[Statistic.MASS_FLOW] == 1.5
        assert ms.values[Statistic.ABS_PRESS] is None

    def test_none_distinct_from_zero(self) -> None:
        """`--` → None is semantically distinct from 0.0; both must round-trip."""
        ms = MeasurementSet(
            unit_id="A",
            values={Statistic.MASS_FLOW: 0.0, Statistic.ABS_PRESS: None},
            averaging_ms=1,
            received_at=datetime.now(UTC),
        )
        assert ms.values[Statistic.MASS_FLOW] == 0.0
        assert ms.values[Statistic.ABS_PRESS] is None


class TestDeviceInfo:
    def _minimal(self) -> DeviceInfo:
        return DeviceInfo(
            unit_id="A",
            manufacturer="Alicat Scientific",
            model="MC-100SCCM-D",
            serial="123456",
            manufactured="2021-01-01",
            calibrated="2021-02-01",
            calibrated_by="ACS",
            software="10v05",
            firmware=FirmwareVersion.parse("10v05"),
            firmware_date=date(2021, 5, 19),
            kind=DeviceKind.FLOW_CONTROLLER,
            media=Medium.GAS,
            capabilities=Capability.BAROMETER | Capability.MULTI_VALVE,
        )

    def test_construct(self) -> None:
        info = self._minimal()
        assert info.model == "MC-100SCCM-D"
        assert Capability.BAROMETER in info.capabilities
        assert Capability.MULTI_VALVE in info.capabilities

    def test_defaults_empty_probe_and_full_scale(self) -> None:
        """Probe report and full-scale cache default empty — session fills in."""
        info = self._minimal()
        assert info.probe_report == {}
        assert info.full_scale == {}

    def test_defaults_are_not_shared_across_instances(self) -> None:
        """`field(default_factory=dict)` — regression guard for mutable-default bug."""
        a = self._minimal()
        b = self._minimal()
        assert a.probe_report is not b.probe_report
        assert a.full_scale is not b.full_scale

    def test_gp_fallback_shape(self) -> None:
        """Pre-8v28 / GP devices synthesise DeviceInfo with only model known."""
        info = DeviceInfo(
            unit_id="A",
            manufacturer=None,
            model="MC-100SCCM-D",  # from caller-supplied model_hint
            serial=None,
            manufactured=None,
            calibrated=None,
            calibrated_by=None,
            software="GP",
            firmware=FirmwareVersion.parse("GP"),
            firmware_date=None,
            kind=DeviceKind.FLOW_CONTROLLER,
            media=Medium.GAS,
            capabilities=Capability.NONE,
        )
        assert info.serial is None
        assert info.firmware_date is None

    def test_is_frozen(self) -> None:
        info = self._minimal()
        with pytest.raises(FrozenInstanceError):
            info.unit_id = "B"  # type: ignore[misc]
