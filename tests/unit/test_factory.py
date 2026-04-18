"""Tests for :mod:`alicatlib.devices.factory`.

Covers the four public functions (``identify_device``, ``probe_capabilities``,
``device_class_for``, ``open_device``) plus the ``MODEL_RULES`` dispatch
table. ``open_device`` is exercised end-to-end with a :class:`FakeTransport`
scripted to emit VE / ??M* / ??D* / poll replies so the full
identification + probe + cache + facade pipeline lives under one test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import Capability
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.base import Device
from alicatlib.devices.factory import (
    MODEL_RULES,
    device_class_for,
    identify_device,
    open_device,
    probe_capabilities,
)
from alicatlib.devices.flow_controller import FlowController
from alicatlib.devices.flow_meter import FlowMeter
from alicatlib.devices.models import DeviceInfo
from alicatlib.errors import AlicatConfigurationError, AlicatError
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.registry import Gas
from alicatlib.transport import FakeTransport
from tests._typing import approx

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mfg_lines(
    *,
    manufacturer: str = "Alicat Scientific",
    model: str = "MC-100SCCM-D",
    serial: str = "123456",
    manufactured: str = "01/01/2021",
    calibrated: str = "02/01/2021",
    calibrated_by: str = "ACS",
    software: str = "10v05",
) -> bytes:
    """Assemble a ??M* response matching the canonical Alicat dialect.

    Verified 2026-04-17 against 8v17 + V10 hardware (design §16.6):
    codes are M00..M09 (zero-indexed) with embedded human-readable
    labels between code and value.
    """
    return b"".join(
        [
            f"A M00 {manufacturer}\r".encode("ascii"),
            b"A M01 www.example.com\r",
            b"A M02 Ph   555-000-0000\r",
            b"A M03 info@example.com\r",
            f"A M04 Model Number {model}\r".encode("ascii"),
            f"A M05 Serial Number {serial}\r".encode("ascii"),
            f"A M06 Date Manufactured {manufactured}\r".encode("ascii"),
            f"A M07 Date Calibrated   {calibrated}\r".encode("ascii"),
            f"A M08 Calibrated By     {calibrated_by}\r".encode("ascii"),
            f"A M09 Software Revision {software}\r".encode("ascii"),
        ],
    )


def _df_lines() -> bytes:
    """Canonical Alicat ??D* response shape (verified 2026-04-17)."""
    return b"".join(
        [
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________\r",
            b"A D01 700 Unit ID                    string          1\r",
            b"A D02 002 Abs Press                  s decimal     7/2 010 02 PSIA\r",
            b"A D03 005 Mass Flow                  s decimal     7/2 012 02 SCCM\r",
            b"A D04 037 Mass Flow Setpt            s decimal     7/2 012 02 SCCM\r",
        ],
    )


def _happy_script(
    *,
    firmware: str = "10v05 Jan  9 2025,15:04:07",
    model: str = "MC-100SCCM-D",
) -> dict[bytes, bytes]:
    return {
        b"AVE\r": f"A   {firmware}\r".encode("ascii"),
        b"A??M*\r": _mfg_lines(model=model),
        b"A??D*\r": _df_lines(),
        b"A\r": b"A +14.62 +25.50 +050.00\r",
        b"AGS 8\r": b"A 8 N2 Nitrogen\r",
        # FPF replies for the three numeric fields advertised in
        # ``_df_lines()``. Factory populates ``DeviceInfo.full_scale``
        # from these (design §10.1 "setpoint full-scale validation").
        # Values chosen to match an MC-100SCCM-D's abs-press / mass-flow
        # ranges; unit codes are the V10 DCU/FPF wire codes.
        b"AFPF 2\r": b"A 14.7 10 PSIA\r",
        b"AFPF 5\r": b"A 100 12 SCCM\r",
        b"AFPF 37\r": b"A 100 12 SCCM\r",
        # LV prefetch — controllers get a single query round-trip at
        # open time so :meth:`FlowController.setpoint` can range-check.
        b"ALV\r": b"A 37\r",
    }


async def _make_client(
    script: Mapping[bytes, ScriptedReply] | None = None,
) -> tuple[AlicatProtocolClient, FakeTransport]:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)
    return client, fake


# ---------------------------------------------------------------------------
# MODEL_RULES / device_class_for
# ---------------------------------------------------------------------------


class TestDeviceClassFor:
    def _info(self, model: str) -> DeviceInfo:
        return DeviceInfo(
            unit_id="A",
            manufacturer=None,
            model=model,
            serial=None,
            manufactured=None,
            calibrated=None,
            calibrated_by=None,
            software="10v05",
            firmware=FirmwareVersion.parse("10v05"),
            firmware_date=None,
            kind=DeviceKind.UNKNOWN,
            media=Medium.NONE,
            capabilities=Capability.NONE,
        )

    def test_mc_dash_routes_to_flow_controller(self) -> None:
        assert device_class_for(self._info("MC-100SCCM-D")) is FlowController

    def test_mcs_dash_routes_to_flow_controller(self) -> None:
        assert device_class_for(self._info("MCS-50SCCM")) is FlowController

    def test_m_dash_routes_to_flow_meter(self) -> None:
        assert device_class_for(self._info("M-100SCCM-D")) is FlowMeter

    def test_ms_dash_routes_to_flow_meter(self) -> None:
        assert device_class_for(self._info("MS-50SCCM-D")) is FlowMeter

    def test_unknown_prefix_falls_back_to_device(self) -> None:
        """Loud fallback — UNKNOWN kind + generic Device so commands can still gate."""
        assert device_class_for(self._info("ZZ-UNKNOWN")) is Device

    def test_model_rules_controllers_before_meters(self) -> None:
        """MC- prefix must come before M- so controllers aren't mis-routed."""
        prefixes = [rule.prefix for rule in MODEL_RULES]
        for controller_prefix in ("MC-", "MCS-", "MCQ-", "MCW-"):
            # Controller prefixes must be listed (anywhere) in the table.
            assert controller_prefix in prefixes
        # M- must appear after every MC* prefix, so greedy prefix match
        # can't steer controllers into FlowMeter.
        for controller_prefix in ("MC-", "MCS-", "MCQ-", "MCW-"):
            assert prefixes.index(controller_prefix) < prefixes.index("M-")

    def test_lc_dash_routes_to_flow_controller(self) -> None:
        """Liquid controller prefix (LC-) routes to FlowController."""
        from alicatlib.devices.factory import _rule_for_model  # pyright: ignore[reportPrivateUsage]

        rule = _rule_for_model("LC-5LPM")
        assert rule is not None
        assert rule.kind is DeviceKind.FLOW_CONTROLLER
        assert rule.media is Medium.LIQUID

    def test_l_dash_routes_to_flow_meter_liquid(self) -> None:
        from alicatlib.devices.factory import _rule_for_model  # pyright: ignore[reportPrivateUsage]

        rule = _rule_for_model("L-10LPM-D")
        assert rule is not None
        assert rule.kind is DeviceKind.FLOW_METER
        assert rule.media is Medium.LIQUID

    def test_pc_dash_routes_to_pressure_controller(self) -> None:
        from alicatlib.devices.factory import _rule_for_model  # pyright: ignore[reportPrivateUsage]
        from alicatlib.devices.pressure_controller import PressureController

        rule = _rule_for_model("PC-100PSIG")
        assert rule is not None
        assert rule.kind is DeviceKind.PRESSURE_CONTROLLER
        assert rule.media is Medium.GAS
        info = self._info("PC-100PSIG")
        # Even with info.kind=UNKNOWN, the rule's deterministic kind wins.
        assert device_class_for(info) is PressureController

    def test_p_dash_routes_to_pressure_meter(self) -> None:
        from alicatlib.devices.factory import _rule_for_model  # pyright: ignore[reportPrivateUsage]
        from alicatlib.devices.pressure_meter import PressureMeter

        rule = _rule_for_model("P-30PSIG")
        assert rule is not None
        assert rule.kind is DeviceKind.PRESSURE_METER
        assert rule.media is Medium.GAS
        info = self._info("P-30PSIG")
        assert device_class_for(info) is PressureMeter

    def test_coda_k_family_prefixes_resolve_kind_deterministically(self) -> None:
        """Per the Alicat CODA Part Number Decoder (Feb 2024), family is
        the first part-number field and deterministically encodes kind:
        K = Meter, KC = Controller, KF = Pump controller, KG = Pump
        System. KM is the legacy pre-decoder meter naming. Media is
        **not** encoded in the part number; every K-family rule defaults
        to the widest (``Medium.GAS | Medium.LIQUID``), and users whose
        unit is configured for a single medium narrow via
        ``assume_media``.
        """
        from alicatlib.devices.factory import _rule_for_model  # pyright: ignore[reportPrivateUsage]

        cases = (
            # (model,            expected kind,                      prefix)
            ("K-1LPM", DeviceKind.FLOW_METER, "K-"),
            ("KM-1SLPM", DeviceKind.FLOW_METER, "KM-"),  # legacy
            ("KC-D150B-RCS", DeviceKind.FLOW_CONTROLLER, "KC-"),
            ("KF-D100A-NAS", DeviceKind.FLOW_CONTROLLER, "KF-"),  # pump ctl
            ("KG-F250A-SCS", DeviceKind.FLOW_CONTROLLER, "KG-"),  # pump sys
        )
        for model, expected_kind, expected_prefix in cases:
            rule = _rule_for_model(model)
            assert rule is not None, f"no rule matched {model!r}"
            assert rule.prefix == expected_prefix, (
                f"{model}: expected prefix {expected_prefix!r}, got {rule.prefix!r}"
            )
            assert rule.kind is expected_kind, (
                f"{model}: expected kind {expected_kind!r}, got {rule.kind!r}"
            )
            assert rule.media == (Medium.GAS | Medium.LIQUID), (
                f"{model}: media should default to GAS | LIQUID, got {rule.media!r}"
            )

    def test_k_family_longer_prefixes_win(self) -> None:
        """Ordering check: ``KM-``/``KC-``/``KF-``/``KG-`` must appear in
        MODEL_RULES before the bare ``K-`` entry so the greedy matcher
        picks the more-specific controller rules rather than routing
        every K- model to the meter rule."""
        from alicatlib.devices.factory import (
            MODEL_RULES,
            _rule_for_model,  # pyright: ignore[reportPrivateUsage]
        )

        prefixes = [rule.prefix for rule in MODEL_RULES]
        for specific in ("KM-", "KC-", "KF-", "KG-"):
            assert specific in prefixes
            assert prefixes.index(specific) < prefixes.index("K-")
        # KC-D150B-RCS (a controller) must not accidentally match K-.
        rule = _rule_for_model("KC-D150B-RCS")
        assert rule is not None
        assert rule.prefix == "KC-"
        assert rule.kind is DeviceKind.FLOW_CONTROLLER

    def test_basis_b_family_prefixes_resolve_kind_deterministically(self) -> None:
        """Per the Alicat BASIS Part Number Decoder (Feb 2024), family is
        the first part-number field and encodes kind deterministically:
        B = Meter, BC = Controller. BASIS is gas-only (the decoder has
        a dedicated gas-selection field with no liquid option)."""
        from alicatlib.devices.factory import _rule_for_model  # pyright: ignore[reportPrivateUsage]

        cases = (
            ("B-010L-N-2V-20A-00-0000", DeviceKind.FLOW_METER, "B-"),
            ("BC-010L-N-2V-20A-00-LV05", DeviceKind.FLOW_CONTROLLER, "BC-"),
        )
        for model, expected_kind, expected_prefix in cases:
            rule = _rule_for_model(model)
            assert rule is not None, f"no rule matched {model!r}"
            assert rule.prefix == expected_prefix, (
                f"{model}: expected prefix {expected_prefix!r}, got {rule.prefix!r}"
            )
            assert rule.kind is expected_kind, (
                f"{model}: expected kind {expected_kind!r}, got {rule.kind!r}"
            )
            assert rule.media is Medium.GAS, f"{model}: BASIS is gas-only, got media {rule.media!r}"

    def test_b_prefix_does_not_swallow_bc_models(self) -> None:
        """Ordering check: ``BC-`` must resolve to its own controller
        rule, not be swallowed by the shorter ``B-`` meter prefix."""
        from alicatlib.devices.factory import (
            MODEL_RULES,
            _rule_for_model,  # pyright: ignore[reportPrivateUsage]
        )

        prefixes = [rule.prefix for rule in MODEL_RULES]
        assert "BC-" in prefixes
        assert "B-" in prefixes
        assert prefixes.index("BC-") < prefixes.index("B-")
        rule = _rule_for_model("BC-010L-N-2V-20A-00-LV05")
        assert rule is not None
        assert rule.prefix == "BC-"
        assert rule.kind is DeviceKind.FLOW_CONTROLLER

    def test_part_number_guide_secondary_letter_prefixes(self) -> None:
        """Secondary-letter prefixes from the Alicat Part Number Guide
        (DOC-CSR-PARTGUIDE, Apr 2018) must route to the correct facade.

        Covers the valve/variant letters that compose onto ``MC`` / ``PC``:
        H=Hammerhead, P=Pneutronics, R=Rolamite, T=stream-switching,
        3=remote sense port, AS=low-range all-sensor, plus observed
        compounds (MCRH, MCRW, MCRD, MCRWD, PCR3, PCD3, PCRD).
        """
        from alicatlib.devices.factory import _rule_for_model  # pyright: ignore[reportPrivateUsage]

        cases = (
            # ── Mass flow controllers (MC* family) ──
            ("MCH-20SLPM", DeviceKind.FLOW_CONTROLLER, "MCH-"),
            ("MCP-100SLPM", DeviceKind.FLOW_CONTROLLER, "MCP-"),
            ("MCR-500SLPM", DeviceKind.FLOW_CONTROLLER, "MCR-"),
            ("MCT-10SLPM", DeviceKind.FLOW_CONTROLLER, "MCT-"),
            ("MCRH-1000SLPM", DeviceKind.FLOW_CONTROLLER, "MCRH-"),
            ("MCRW-500SLPM", DeviceKind.FLOW_CONTROLLER, "MCRW-"),
            ("MCRD-250SLPM", DeviceKind.FLOW_CONTROLLER, "MCRD-"),
            ("MCRWD-2000SLPM", DeviceKind.FLOW_CONTROLLER, "MCRWD-"),
            # ── Pressure controllers (PC* family) ──
            ("PCH-100PSIA", DeviceKind.PRESSURE_CONTROLLER, "PCH-"),
            ("PCP-30PSIA", DeviceKind.PRESSURE_CONTROLLER, "PCP-"),
            ("PCR-500PSIA", DeviceKind.PRESSURE_CONTROLLER, "PCR-"),
            ("PC3-100PSIA", DeviceKind.PRESSURE_CONTROLLER, "PC3-"),
            ("PCD3-100PSIA", DeviceKind.PRESSURE_CONTROLLER, "PCD3-"),
            ("PCR3-100PSIA", DeviceKind.PRESSURE_CONTROLLER, "PCR3-"),
            ("PCRD-100PSIA", DeviceKind.PRESSURE_CONTROLLER, "PCRD-"),
            ("PCAS-1PSIG", DeviceKind.PRESSURE_CONTROLLER, "PCAS-"),
        )
        for model, expected_kind, expected_prefix in cases:
            rule = _rule_for_model(model)
            assert rule is not None, f"no rule matched {model!r}"
            assert rule.prefix == expected_prefix, (
                f"{model}: expected prefix {expected_prefix!r}, got {rule.prefix!r}"
            )
            assert rule.kind is expected_kind, (
                f"{model}: expected kind {expected_kind!r}, got {rule.kind!r}"
            )
            assert rule.media is Medium.GAS, f"{model}: media should be GAS, got {rule.media!r}"

    def test_pcd_family_s_suffix_widens_media_to_gas_liquid(self) -> None:
        """The PCD-Series spec sheet (DOC-SPECS-PCD Rev 11, Mar 2024)
        establishes that the trailing-``S`` on dual-valve closed-volume
        PC prefixes indicates full stainless-body construction, which
        makes the instrument compatible with gases *and liquids* (quote:
        "PCDS: Compatible with all non-corrosive gases and liquids, and
        many corrosive gases"). Non-``S`` siblings stay gas-only. This
        test pins the pattern so a media widening on ``PCD-`` doesn't
        silently drag the ``S`` variants with it (they're already wider).
        """
        from alicatlib.devices.factory import _rule_for_model  # pyright: ignore[reportPrivateUsage]

        gas_only = (
            "PCD-100PSIG",
            "PCRD-100PSIG",
            "PCRD3-100PSIG",
            "PCD3-100PSIG",
            "PCPD-100PSIG",
            "EPCD-100PSIG",
        )
        gas_and_liquid = ("PCDS-100PSIG", "PCRDS-100PSIG", "PCRD3S-100PSIG")

        for model in gas_only:
            rule = _rule_for_model(model)
            assert rule is not None, f"no rule matched {model!r}"
            assert rule.kind is DeviceKind.PRESSURE_CONTROLLER
            assert rule.media is Medium.GAS, f"{model}: expected gas-only, got {rule.media!r}"

        for model in gas_and_liquid:
            rule = _rule_for_model(model)
            assert rule is not None, f"no rule matched {model!r}"
            assert rule.kind is DeviceKind.PRESSURE_CONTROLLER
            assert rule.media == (Medium.GAS | Medium.LIQUID), (
                f"{model}: expected GAS|LIQUID per 2024 PCD spec sheet, got {rule.media!r}"
            )

    def test_compound_prefixes_precede_bare_forms(self) -> None:
        """Compound prefixes (MCRWD/MCRW/MCRD/MCRH, PCRD/PCR3/PCD3) must
        appear before their shorter constituents in MODEL_RULES so the
        matcher picks the most specific rule — not strictly required
        because dashes separate prefixes, but enforced for readability
        and to catch accidental reordering."""
        from alicatlib.devices.factory import MODEL_RULES

        prefixes = [rule.prefix for rule in MODEL_RULES]
        # MC* compounds before MCR-/MCW-/MCD-/MCH-/MC-
        for compound, base in (
            ("MCRWD-", "MCRW-"),
            ("MCRW-", "MCR-"),
            ("MCRD-", "MCR-"),
            ("MCRH-", "MCR-"),
            ("MCR-", "MC-"),
            ("MCT-", "MC-"),
            # PC* compounds before PCR-/PCD-/PC-
            ("PCRD-", "PCR-"),
            ("PCR3-", "PCR-"),
            ("PCD3-", "PCD-"),
            ("PCR-", "PC-"),
            ("PC3-", "PC-"),
        ):
            assert compound in prefixes, f"{compound} missing from MODEL_RULES"
            assert base in prefixes, f"{base} missing from MODEL_RULES"
            assert prefixes.index(compound) < prefixes.index(base), (
                f"{compound} must precede {base} in MODEL_RULES"
            )

    def test_unknown_prefix_media_resolves_to_none(self) -> None:
        """An unrecognised prefix gives Medium.NONE — fail-loud at the media gate."""
        from alicatlib.devices.factory import (
            _media_for_model,  # pyright: ignore[reportPrivateUsage]
        )

        assert _media_for_model("ZZ-UNKNOWN") is Medium.NONE


# ---------------------------------------------------------------------------
# probe_capabilities
# ---------------------------------------------------------------------------


class TestProbeCapabilities:
    @staticmethod
    def _probe_info(firmware: str = "10v05") -> DeviceInfo:
        return DeviceInfo(
            unit_id="A",
            manufacturer=None,
            model="MC-",
            serial=None,
            manufactured=None,
            calibrated=None,
            calibrated_by=None,
            software=firmware,
            firmware=FirmwareVersion.parse(firmware),
            firmware_date=None,
            kind=DeviceKind.FLOW_CONTROLLER,
            media=Medium.GAS,
            capabilities=Capability.NONE,
        )

    @pytest.mark.anyio
    async def test_unscripted_device_fails_closed(self) -> None:
        """No scripted FPF replies → pressure probes time out, every flag
        stays absent (design §5.9 fail-closed).

        Non-FPF flags still report ``absent`` because nothing probes them
        yet (design §16.6.6 invalidated the VD column-count approach);
        FPF flags report ``timeout`` because the stub client can't answer.
        """
        client, _ = await _make_client()
        caps, report = await probe_capabilities(client, "A", self._probe_info())
        assert caps is Capability.NONE
        assert report[Capability.BAROMETER] == "timeout"
        assert report[Capability.SECONDARY_PRESSURE] == "timeout"
        for flag in Capability:
            if flag in (
                Capability.NONE,
                Capability.BAROMETER,
                Capability.SECONDARY_PRESSURE,
            ):
                continue
            assert report[flag] == "absent"

    @pytest.mark.anyio
    async def test_gp_family_skips_every_probe(self) -> None:
        """GP has no FPF — probe returns absent for every flag with no I/O.

        Pinned so a future "always probe" refactor can't silently start
        issuing FPF on GP devices (the primer and our lack of GP
        captures both point to a universal absence).
        """
        client, fake = await _make_client()
        caps, report = await probe_capabilities(client, "A", self._probe_info("GP"))
        assert caps is Capability.NONE
        assert report[Capability.BAROMETER] == "absent"
        assert fake.writes == ()

    @pytest.mark.anyio
    async def test_fpf_present_reply_resolves_barometer(self) -> None:
        """A real FPF reply (``value > 0`` AND ``unit_label != '---'``)
        resolves :attr:`Capability.BAROMETER` (design §16.6.3 rule)."""
        client, _ = await _make_client(
            {
                b"AFPF 15\r": b"A 14.696 10 PSIA\r",
                b"AFPF 344\r": b"A 0 1 ---\r",
            },
        )
        caps, report = await probe_capabilities(client, "A", self._probe_info())
        assert Capability.BAROMETER in caps
        assert Capability.SECONDARY_PRESSURE not in caps
        assert report[Capability.BAROMETER] == "present"
        assert report[Capability.SECONDARY_PRESSURE] == "absent"

    @pytest.mark.anyio
    async def test_fpf_absent_pattern_keeps_flag_absent(self) -> None:
        """``A 0 1 ---`` is the device-side "no such statistic" sentinel.

        Must NOT be misread as present even though the command didn't
        reject — the only-non-rejection check was what led to the
        over-eager classification noted in the hardware handoff
        §Outstanding-work item 4.
        """
        client, _ = await _make_client(
            {
                b"AFPF 15\r": b"A 0 1 ---\r",
                b"AFPF 344\r": b"A +0.0000 1 ---\r",
            },
        )
        caps, report = await probe_capabilities(client, "A", self._probe_info())
        assert caps is Capability.NONE
        assert report[Capability.BAROMETER] == "absent"
        assert report[Capability.SECONDARY_PRESSURE] == "absent"

    @pytest.mark.anyio
    async def test_fpf_rejection_marks_rejected(self) -> None:
        """``?`` reply surfaces as ``rejected`` in the probe report."""
        client, _ = await _make_client(
            {
                b"AFPF 15\r": b"?\r",
                b"AFPF 344\r": b"?\r",
            },
        )
        caps, report = await probe_capabilities(client, "A", self._probe_info())
        assert caps is Capability.NONE
        assert report[Capability.BAROMETER] == "rejected"
        assert report[Capability.SECONDARY_PRESSURE] == "rejected"


# ---------------------------------------------------------------------------
# identify_device — happy path, fallback paths
# ---------------------------------------------------------------------------


class TestIdentifyDevice:
    @pytest.mark.anyio
    async def test_happy_path_v10(self) -> None:
        client, _ = await _make_client(_happy_script())
        info = await identify_device(client)
        assert info.unit_id == "A"
        assert info.manufacturer == "Alicat Scientific"
        assert info.model == "MC-100SCCM-D"
        assert info.serial == "123456"
        assert info.manufactured == "01/01/2021"
        assert info.calibrated == "02/01/2021"
        assert info.calibrated_by == "ACS"
        assert info.software == "10v05"
        assert info.firmware == FirmwareVersion(FirmwareFamily.V10, 10, 5, "10v05")
        assert info.kind is DeviceKind.FLOW_CONTROLLER
        # capabilities populated by probe_capabilities, not identify_device.
        assert info.capabilities is Capability.NONE

    @pytest.mark.anyio
    async def test_gp_fallback_requires_model_hint(self) -> None:
        """GP devices can't run ??M*; model_hint is mandatory."""
        client, _ = await _make_client({b"AVE\r": b"A GP\r"})
        with pytest.raises(AlicatConfigurationError):
            await identify_device(client)

    @pytest.mark.anyio
    async def test_gp_fallback_with_model_hint_synthesises_info(self) -> None:
        client, fake = await _make_client({b"AVE\r": b"A GP\r"})
        info = await identify_device(client, model_hint="M-50SCCM-D")
        assert info.model == "M-50SCCM-D"
        assert info.firmware.family is FirmwareFamily.GP
        assert info.kind is DeviceKind.FLOW_METER  # from M- prefix
        assert info.manufacturer is None
        assert info.serial is None
        # ??M* was NOT attempted — only VE went out.
        assert fake.writes == (b"AVE\r",)

    @pytest.mark.anyio
    async def test_pre_8v28_fallback_requires_model_hint(self) -> None:
        """A V8_V9 device at 8v00 is pre-8v28 and must fall back."""
        client, _ = await _make_client({b"AVE\r": b"A 8v00 2013-01-01\r"})
        with pytest.raises(AlicatConfigurationError):
            await identify_device(client)

    @pytest.mark.anyio
    async def test_pre_8v28_with_model_hint(self) -> None:
        """Per design §16.6.2 the factory tries ??M* on every numeric family
        and falls back to ``model_hint`` on rejection. The pre-8v28 path
        shows two writes (VE then ??M*-rejected) and resolves via hint."""
        client, fake = await _make_client(
            {
                b"AVE\r": b"A 8v00 2013-01-01\r",
                b"A??M*\r": b"?\r",
            },
        )
        info = await identify_device(client, model_hint="MC-25SCCM-D")
        assert info.model == "MC-25SCCM-D"
        assert info.kind is DeviceKind.FLOW_CONTROLLER
        assert fake.writes == (b"AVE\r", b"A??M*\r")

    @pytest.mark.anyio
    async def test_v1_v7_attempts_manufacturing_info_then_falls_back(self) -> None:
        """V1_V7 attempts ??M* (5v12 + 8v17 captures showed it works on
        pre-8v28 firmware — primer's 8v28+ floor was wrong, design
        §16.6.2). When the device rejects, fall back to model_hint.
        """
        # Script: VE works, ??M* gets rejected with `?` (the "primer is
        # right" branch — some V1_V7 devices may not have ??M* even though
        # 5v12 does). The factory's try-and-recover wraps both outcomes.
        client, _ = await _make_client(
            {
                b"AVE\r": b"A 5v00 Jan  1 2010,00:00:00\r",
                b"A??M*\r": b"A?\r",  # rejection
            },
        )
        info = await identify_device(client, model_hint="M-10SLPM-D")
        assert info.firmware.family is FirmwareFamily.V1_V7
        assert info.model == "M-10SLPM-D"


# ---------------------------------------------------------------------------
# open_device — the async CM
# ---------------------------------------------------------------------------


class TestOpenDeviceWithTransport:
    """Drive open_device with a pre-built :class:`FakeTransport`.

    When the caller hands in a Transport (not a str path), the factory
    does *not* close it on exit — the caller owns that lifecycle.
    """

    @pytest.mark.anyio
    async def test_happy_path_yields_flow_controller(self) -> None:
        fake = FakeTransport(_happy_script())
        async with open_device(fake) as dev:
            assert isinstance(dev, FlowController)
            assert dev.info.model == "MC-100SCCM-D"
            assert dev.info.kind is DeviceKind.FLOW_CONTROLLER
            assert dev.unit_id == "A"

    @pytest.mark.anyio
    async def test_poll_works_end_to_end(self) -> None:
        fake = FakeTransport(_happy_script())
        async with open_device(fake) as dev:
            frame = await dev.poll()
            assert frame.unit_id == "A"
            assert frame.values["Mass_Flow"] == approx(25.5)

    @pytest.mark.anyio
    async def test_gas_method_round_trips(self) -> None:
        fake = FakeTransport(_happy_script())
        async with open_device(fake) as dev:
            assert isinstance(dev, FlowMeter)  # FlowController is-a FlowMeter
            state = await dev.gas(Gas.N2)
            assert state.gas is Gas.N2

    @pytest.mark.anyio
    async def test_caller_owned_transport_stays_open(self) -> None:
        """Factory must not close a transport it didn't open."""
        fake = FakeTransport(_happy_script())
        async with open_device(fake):
            pass
        assert fake.is_open is True

    @pytest.mark.anyio
    async def test_model_hint_honoured_for_gp_device(self) -> None:
        # GP devices use ``$$`` only for writes (design §16.6.8); read
        # queries (``??M*`` / ``??D*`` / ``??G*`` / poll) go prefix-less.
        # This test exercises the legacy code-path where a device claims
        # ``GP`` via VE — not a real-hardware shape (real GP firmware
        # doesn't implement VE), but a useful regression pin for the
        # ``family is GP → skip ??M*`` branch of ``identify_device``.
        fake = FakeTransport(
            {
                b"AVE\r": b"A GP\r",
                b"A??D*\r": _df_lines(),
            },
        )
        async with open_device(fake, model_hint="M-100SCCM-D") as dev:
            assert isinstance(dev, FlowMeter)
            assert not isinstance(dev, FlowController)
            assert dev.info.firmware.family is FirmwareFamily.GP
            assert dev.info.model == "M-100SCCM-D"

    @pytest.mark.anyio
    async def test_assume_capabilities_unions(self) -> None:
        """User-supplied capabilities union onto the (empty) probed set."""
        fake = FakeTransport(_happy_script())
        async with open_device(
            fake,
            assume_capabilities=Capability.BAROMETER | Capability.MULTI_VALVE,
        ) as dev:
            assert Capability.BAROMETER in dev.info.capabilities
            assert Capability.MULTI_VALVE in dev.info.capabilities

    @pytest.mark.anyio
    async def test_assume_media_replaces_prefix_default(self) -> None:
        """``assume_media`` **replaces** the prefix-derived media (design §5.9a).

        Contrast with ``assume_capabilities`` (unions). Rationale: medium
        describes how the specific unit is configured, not what the
        hardware can do — the common override is narrowing a dual-medium
        default down to what the unit was actually ordered locked to.
        """
        fake = FakeTransport(_happy_script())
        # The scripted device identifies as MC-*, whose prefix-derived
        # medium is Medium.GAS. Passing ``assume_media=Medium.LIQUID``
        # must replace that (not union): the resolved media should be
        # LIQUID alone, not GAS | LIQUID.
        async with open_device(fake, assume_media=Medium.LIQUID) as dev:
            assert dev.info.media is Medium.LIQUID
            # Gas bit must not be set — `&` is non-empty only if intersecting.
            assert not (dev.info.media & Medium.GAS)

    @pytest.mark.anyio
    async def test_assume_media_none_preserves_prefix_default(self) -> None:
        """Omitting ``assume_media`` keeps the prefix-derived value."""
        fake = FakeTransport(_happy_script())
        async with open_device(fake) as dev:
            # MC-* → Medium.GAS per MODEL_RULES.
            assert dev.info.media is Medium.GAS


class TestOpenDeviceWithClient:
    @pytest.mark.anyio
    async def test_caller_owned_client_stays_usable(self) -> None:
        client, fake = await _make_client(_happy_script())
        async with open_device(client) as dev:
            assert isinstance(dev, FlowController)
        assert fake.is_open is True


class TestOpenDeviceStreamRecovery:
    @pytest.mark.anyio
    async def test_recovers_when_transport_has_buffered_bytes(self) -> None:
        """A device left streaming by a prior process pushes bytes into the buffer.

        Stream recovery should see them, issue a stop-stream command,
        drain, and then proceed with normal identification.
        """
        fake = FakeTransport(_happy_script())
        await fake.open()
        # Simulate unsolicited streaming data queued on the transport.
        fake.feed(b"A 14.7 25.5 50.0\r")
        # Add the stop-stream byte form to the script — recovery issues
        # it raw (no reply expected; the factory just drains after).
        fake.add_script(b"@@ A\r", b"")

        async with open_device(fake) as dev:
            assert dev.info.model == "MC-100SCCM-D"

        # First write must be the stop-stream bytes — recovery happened
        # before identification began.
        assert fake.writes[0] == b"@@ A\r"

    @pytest.mark.anyio
    async def test_stop_stream_emitted_unconditionally(self) -> None:
        """Clean bus: factory still emits ``@@ <uid>`` before identification.

        Per design §16.6, the stop-stream is sent unconditionally
        because a device may be in a half-streaming state
        that the passive sniff misses. Cheap insurance — one extra write
        at ~10 ms. Asserts the stop-stream precedes ``VE``.
        """
        fake = FakeTransport(_happy_script())
        async with open_device(fake):
            pass
        # Writes begin with the unconditional stop-stream, then VE.
        assert fake.writes[0] == b"@@ A\r"
        assert fake.writes[1] == b"AVE\r"

    @pytest.mark.anyio
    async def test_recovery_can_be_disabled(self) -> None:
        """recover_from_stream=False skips the passive read entirely.

        With recovery disabled and streaming bytes in the buffer, the
        first real command (VE) reads those leftover bytes as its
        reply. Identification fails somewhere downstream; we don't pin
        *where* (depends on how the leftover bytes happen to parse),
        only that the factory emitted *no* stop-stream command.
        """
        # Use a payload that won't accidentally parse as firmware — all
        # letters, no v-pattern digits. Ensures downstream failure.
        fake = FakeTransport(_happy_script())
        await fake.open()
        fake.feed(b"garbage nonfirmware output\r")

        with pytest.raises(AlicatError):
            async with open_device(fake, recover_from_stream=False):
                pass
        assert b"@@ A\r" not in fake.writes


class TestOpenDeviceLifecycle:
    @pytest.mark.anyio
    async def test_device_close_fires_on_context_exit(self) -> None:
        fake = FakeTransport(_happy_script())
        async with open_device(fake) as dev:
            assert not dev.session.closed
        assert dev.session.closed


# ---------------------------------------------------------------------------
# Post-identification DCU / FPF / LV binding
# ---------------------------------------------------------------------------


def _df_lines_no_units() -> bytes:
    """A ``??D*`` reply whose decimal rows omit the trailing unit label.

    Mirrors a legacy firmware variant where the notes column stops at
    the width token — the ??D* parser leaves ``field.unit = None`` for
    such rows so the factory's ``DCU`` probe has to fill the gap
    (design §10.1 "automatic DataFrameField.unit binding").
    """
    return b"".join(
        [
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________\r",
            b"A D01 700 Unit ID                    string          1\r",
            b"A D02 002 Abs Press                  s decimal     7/2\r",
            b"A D03 005 Mass Flow                  s decimal     7/2\r",
            b"A D04 037 Mass Flow Setpt            s decimal     7/2\r",
        ],
    )


class TestOpenDeviceFullScaleProbe:
    """Factory populates :attr:`DeviceInfo.full_scale` from FPF per field."""

    @pytest.mark.anyio
    async def test_full_scale_populated_for_every_numeric_field(self) -> None:
        fake = FakeTransport(_happy_script())
        async with open_device(fake) as dev:
            # Three numeric fields in _df_lines(): stat codes 2, 5, 37.
            # Every one should have a FullScaleValue entry.
            full_scale = dev.info.full_scale
            from alicatlib.registry._codes_gen import STATISTIC_BY_CODE

            for code in (2, 5, 37):
                stat = STATISTIC_BY_CODE[code]
                assert stat in full_scale, f"missing full-scale for {stat}"
            # The statistic field must be hydrated by the factory (the
            # FPF decoder leaves it as Statistic.NONE — design §5.5).
            fs_mass = full_scale[STATISTIC_BY_CODE[5]]
            assert fs_mass.statistic is STATISTIC_BY_CODE[5]
            assert fs_mass.value == approx(100.0)
            assert fs_mass.unit_label == "SCCM"

    @pytest.mark.anyio
    async def test_full_scale_skipped_for_gp_family(self) -> None:
        """GP firmware lacks FPF — full_scale must stay empty."""
        fake = FakeTransport(
            {
                b"AVE\r": b"A GP\r",
                b"A??D*\r": _df_lines(),
                b"ALV\r": b"A 37\r",
            },
        )
        async with open_device(fake, model_hint="MC-100SCCM-D") as dev:
            assert dev.info.firmware.family is FirmwareFamily.GP
            assert dict(dev.info.full_scale) == {}

    @pytest.mark.anyio
    async def test_full_scale_skips_absent_sentinel_reply(self) -> None:
        """``A <zero> <code> ---`` is the device's "statistic absent" sentinel.

        Design §16.6.3: the sentinel must be treated identically to a
        rejection — that slot stays out of the full-scale mapping.
        """
        script = _happy_script()
        # Replace the abs-press FPF with the absent-statistic sentinel.
        script[b"AFPF 2\r"] = b"A 0 1 ---\r"
        fake = FakeTransport(script)
        async with open_device(fake) as dev:
            from alicatlib.registry._codes_gen import STATISTIC_BY_CODE

            assert STATISTIC_BY_CODE[2] not in dev.info.full_scale
            # Other fields still populate — one bad slot doesn't poison the rest.
            assert STATISTIC_BY_CODE[5] in dev.info.full_scale

    @pytest.mark.anyio
    async def test_full_scale_survives_single_field_timeout(self) -> None:
        """A timeout on one FPF leaves the other slots populated."""
        script = _happy_script()
        # Drop the mass-flow FPF script entry — FakeTransport then times
        # out on that probe; the other two still return normally.
        del script[b"AFPF 5\r"]
        fake = FakeTransport(script)
        # Reduce client timeout so the test doesn't wait the full
        # default ``open_device`` budget.
        async with open_device(fake, timeout=0.02) as dev:
            from alicatlib.registry._codes_gen import STATISTIC_BY_CODE

            assert STATISTIC_BY_CODE[5] not in dev.info.full_scale
            assert STATISTIC_BY_CODE[2] in dev.info.full_scale
            assert STATISTIC_BY_CODE[37] in dev.info.full_scale


class TestOpenDeviceDcuUnitBinding:
    """Factory fills in :attr:`DataFrameField.unit` via ``DCU`` when ``??D*`` omits it."""

    @pytest.mark.anyio
    async def test_dcu_binds_unit_when_df_reply_omits_label(self) -> None:
        script = _happy_script()
        script[b"A??D*\r"] = _df_lines_no_units()
        # DCU replies: <uid> <unit_code> <unit_label>.
        script[b"ADCU 2\r"] = b"A 10 PSIA\r"
        script[b"ADCU 5\r"] = b"A 12 SCCM\r"
        script[b"ADCU 37\r"] = b"A 12 SCCM\r"
        fake = FakeTransport(script)
        async with open_device(fake) as dev:
            fmt = dev.session.data_frame_format
            assert fmt is not None
            fields_by_name = {f.name: f for f in fmt.fields}
            mass_flow = fields_by_name["Mass_Flow"]
            assert mass_flow.unit is not None
            assert mass_flow.unit.value == "SCCM"

    @pytest.mark.anyio
    async def test_dcu_probe_not_issued_when_df_already_bound_unit(self) -> None:
        """``??D*`` provided resolvable units; DCU probe should not fire."""
        fake = FakeTransport(_happy_script())
        async with open_device(fake):
            pass
        dcu_writes = [w for w in fake.writes if w.startswith(b"ADCU ")]
        assert dcu_writes == []

    @pytest.mark.anyio
    async def test_dcu_timeout_leaves_field_unresolved(self) -> None:
        """DCU probe failure must not prevent opening the device."""
        script = _happy_script()
        script[b"A??D*\r"] = _df_lines_no_units()
        # No DCU entries — every probe times out.
        fake = FakeTransport(script)
        async with open_device(fake, timeout=0.02) as dev:
            fmt = dev.session.data_frame_format
            assert fmt is not None
            # Fields remain unresolved, but the device opened cleanly.
            for field in fmt.fields:
                if field.name in {"Abs_Press", "Mass_Flow", "Mass_Flow_Setpt"}:
                    assert field.unit is None


class TestOpenDeviceLoopControlPrefetch:
    """Factory pre-caches the loop-control variable for controllers."""

    @pytest.mark.anyio
    async def test_controller_lv_cached_at_open(self) -> None:
        fake = FakeTransport(_happy_script())
        async with open_device(fake) as dev:
            from alicatlib.registry import LoopControlVariable

            assert dev.session.loop_control_variable is LoopControlVariable.MASS_FLOW_SETPT

    @pytest.mark.anyio
    async def test_meter_does_not_probe_lv(self) -> None:
        """Meter kinds have no LV command — probe must be skipped entirely."""
        script = _happy_script(model="M-100SCCM-D")
        script[b"A??M*\r"] = _mfg_lines(model="M-100SCCM-D")
        fake = FakeTransport(script)
        async with open_device(fake) as dev:
            assert isinstance(dev, FlowMeter)
            assert not isinstance(dev, FlowController)
            assert dev.session.loop_control_variable is None
            assert b"ALV\r" not in fake.writes

    @pytest.mark.anyio
    async def test_lv_timeout_leaves_cache_none(self) -> None:
        """LV-unsupported firmware leaves the cache ``None`` (setpoint skips range check)."""
        script = _happy_script()
        del script[b"ALV\r"]  # force a timeout on the LV probe
        fake = FakeTransport(script)
        async with open_device(fake, timeout=0.02) as dev:
            assert dev.session.loop_control_variable is None
