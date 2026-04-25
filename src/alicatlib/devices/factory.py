"""Device factory — identification pipeline + ``open_device`` context manager.

The factory implements design §5.9's staged identification flow:

1. (Optional) stream recovery — passively read the transport for ~100 ms;
   if any bytes arrive, the device was left streaming by a prior process,
   so issue a stop-stream (``@@ {unit_id}``) and drain before the real
   identify begins.
2. ``VE`` — firmware version; works on every firmware family and is the
   anchor of identification.
3. ``??M*`` — 10-line manufacturing-info table, *only* when firmware is
   numeric-family and ≥ 8v28. Parsed by the protocol layer into
   :class:`ManufacturingInfo`; the factory applies a best-guess
   ``M<NN>`` → named-field mapping to synthesise :class:`DeviceInfo`.
4. Fallback — for GP / pre-8v28 devices ``??M*`` isn't available, so the
   caller must supply ``model_hint``. The factory raises
   :class:`AlicatConfigurationError` if identification reaches this
   branch without a hint.
5. Capability probing — :func:`probe_capabilities` probes the device for
   each :class:`Capability` flag, failing *closed* (default absent on
   timeout / ``?`` / parse error). Outcomes are retained in
   :attr:`DeviceInfo.probe_report` for diagnostics; gating uses only the
   flag set.
6. ``??D*`` — cached on the session as
   :attr:`Session.data_frame_format`.
7. Model-rule dispatch — :func:`device_class_for` picks the correct
   :class:`Device` subclass via the :data:`MODEL_RULES` table.

Stream recovery, capability probing, and the M-code → named-field
mapping are all marked as best-effort and will be tightened against
hardware captures.

Design reference: ``docs/design.md`` §5.9, §5.20.
"""

from __future__ import annotations

import dataclasses
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from alicatlib._logging import get_logger
from alicatlib.commands import (
    DATA_FRAME_FORMAT_QUERY,
    ENGINEERING_UNITS,
    FULL_SCALE_QUERY,
    LOOP_CONTROL_VARIABLE,
    MANUFACTURING_INFO,
    VE_QUERY,
    Capability,
    DataFrameFormatRequest,
    EngineeringUnitsRequest,
    FullScaleQueryRequest,
    LoopControlVariableRequest,
    ManufacturingInfoRequest,
    VeRequest,
)
from alicatlib.devices.base import Device
from alicatlib.devices.flow_controller import FlowController
from alicatlib.devices.flow_meter import FlowMeter
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.medium import Medium
from alicatlib.devices.models import DeviceInfo, ManufacturingInfo, ProbeOutcome
from alicatlib.devices.pressure_controller import PressureController
from alicatlib.devices.pressure_meter import PressureMeter
from alicatlib.devices.session import Session
from alicatlib.errors import (
    AlicatCommandRejectedError,
    AlicatConfigurationError,
    AlicatError,
    AlicatParseError,
    AlicatTimeoutError,
    AlicatTransportError,
    ErrorContext,
)
from alicatlib.firmware import NUMERIC_FAMILIES, FirmwareFamily, FirmwareVersion
from alicatlib.protocol.client import AlicatProtocolClient
from alicatlib.registry._codes_gen import STATISTIC_BY_CODE
from alicatlib.transport.base import SerialSettings
from alicatlib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from alicatlib.devices.data_frame import DataFrameField, DataFrameFormat
    from alicatlib.devices.models import FullScaleValue
    from alicatlib.registry import Statistic, Unit
    from alicatlib.transport.base import Transport

__all__ = [
    "MODEL_RULES",
    "ModelRule",
    "device_class_for",
    "identify_device",
    "open_device",
    "probe_capabilities",
]


_logger = get_logger("session")


# ---------------------------------------------------------------------------
# Model rules — prefix → (DeviceKind, Medium, facade class-map)
# ---------------------------------------------------------------------------


# Shortcut maps — the common case is one kind per prefix, so we reuse a
# frozen single-entry mapping rather than inlining the ``{...}`` dict
# literal on every rule.
_FC_MAP: Final[Mapping[DeviceKind, type[Device]]] = MappingProxyType(
    {DeviceKind.FLOW_CONTROLLER: FlowController},
)
_FM_MAP: Final[Mapping[DeviceKind, type[Device]]] = MappingProxyType(
    {DeviceKind.FLOW_METER: FlowMeter},
)
_PC_MAP: Final[Mapping[DeviceKind, type[Device]]] = MappingProxyType(
    {DeviceKind.PRESSURE_CONTROLLER: PressureController},
)
_PM_MAP: Final[Mapping[DeviceKind, type[Device]]] = MappingProxyType(
    {DeviceKind.PRESSURE_METER: PressureMeter},
)


@dataclass(frozen=True, slots=True)
class ModelRule:
    """One entry in the model-prefix dispatch table (design §5.9, §5.9a).

    The factory walks :data:`MODEL_RULES` in declared order and returns
    the first rule whose :attr:`prefix` matches the identified model.
    Ordering matters: longer / more-specific prefixes must come *before*
    their shorter kin (``MCDW-`` before ``MCW-`` before ``MC-``), so the
    most-specific match wins.

    Every currently-supported prefix resolves :attr:`kind` deterministically
    — the published Alicat part-number decoders for the M, MC, P, PC, L,
    LC, K-family (CODA), and B/BC (BASIS) lines all encode meter vs.
    controller as a distinct part-number field. If a future prefix turns
    out to be kind-ambiguous at the prefix level, this dataclass will
    grow a ``kind_probe`` field back at that time; omitting it now keeps
    the public shape minimal.

    Attributes:
        prefix: The model-string prefix this rule claims (e.g. ``"MC-"``).
            Alicat model strings are always uppercase; matching is
            case-sensitive.
        kind: The :class:`DeviceKind` for any model matching this prefix.
        media: The :class:`Medium` flag this prefix declares. For
            prefixes whose medium isn't determinable from the part
            number (the CODA K-family is the current example — the
            part-number decoder encodes kind but not medium, and every
            CODA unit is currently believed to handle both), this is
            the widest possible default (``Medium.GAS | Medium.LIQUID``);
            users whose device is configured for a single medium narrow
            via :func:`open_device`'s ``assume_media`` parameter.
        device_cls_map: Per-kind facade class. Lookup is
            ``device_cls_map[kind]``; when the kind isn't in the map
            the factory drops to the generic :class:`Device` (which
            still honours every session gate, so safe).
    """

    prefix: str
    kind: DeviceKind
    media: Medium
    device_cls_map: Mapping[DeviceKind, type[Device]]


#: Model-prefix → facade dispatch table. Controllers before meters
#: within each family, most-specific prefix first (``MCDW-`` before
#: ``MCW-`` before ``MC-``). Prefix matching is case-sensitive — Alicat
#: model strings are always uppercase.
#:
#: Source for the prefix → (kind, medium) mapping: Alicat Model Guide
#: (https://www.alicat.com/support/alicat-model-guide-for-all-flow-and-pressure-instruments/).
#: Verification TODOs for ambiguous entries live at design §16.1.
MODEL_RULES: Final[tuple[ModelRule, ...]] = (
    # ── Gas mass-flow controllers (thermal MFC) ──────────────────────
    # Secondary letters compose per the Alicat Part Number Guide
    # (DOC-CSR-PARTGUIDE, Apr 2018): D=dual valves, E=enclosed,
    # H=Hammerhead, P=Pneutronics, Q=high-pressure (160-320 psia),
    # R=Rolamite, S=stainless sensor, T=stream-switching (dual valves
    # for source/process switching), V=MCE+pneumatic shutoff, W=Whisper.
    # Compounds observed in the guide (MCRWD, MCDW, MCRH, MCRW, MCRD)
    # land first so the matcher picks the most specific prefix.
    ModelRule("MCRWD-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCDW-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCRH-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCRW-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCRD-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCS-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCQ-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCW-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCD-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCV-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCE-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCH-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCP-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCR-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MCT-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("MC-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    ModelRule("SFF-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    # ── BASIS (compact OEM gas flow; per the Alicat BASIS Part Number
    # Decoder, Feb 2024, family code B = Meter, BC = Controller). ─────
    # BC- is listed here among flow controllers; B- (meter) lives in
    # the meters block below. BASIS is gas-only: the part number has
    # a dedicated gas-selection field (A/M/C/E/H/N/R/T/X for Air/CH4/
    # CO2/He/H2/N2/Ar/N2O/O2) with no liquid option.
    ModelRule("BC-", DeviceKind.FLOW_CONTROLLER, Medium.GAS, _FC_MAP),
    # ── Gas mass-flow meters (thermal MFM) ───────────────────────────
    ModelRule("MWB-", DeviceKind.FLOW_METER, Medium.GAS, _FM_MAP),
    ModelRule("MBS-", DeviceKind.FLOW_METER, Medium.GAS, _FM_MAP),
    ModelRule("MS-", DeviceKind.FLOW_METER, Medium.GAS, _FM_MAP),
    ModelRule("MQ-", DeviceKind.FLOW_METER, Medium.GAS, _FM_MAP),
    ModelRule("MW-", DeviceKind.FLOW_METER, Medium.GAS, _FM_MAP),
    ModelRule("MB-", DeviceKind.FLOW_METER, Medium.GAS, _FM_MAP),
    ModelRule("M-", DeviceKind.FLOW_METER, Medium.GAS, _FM_MAP),
    # ── BASIS meter — B family code, per BASIS Part Number Decoder ───
    ModelRule("B-", DeviceKind.FLOW_METER, Medium.GAS, _FM_MAP),
    # ── Gas / gas+liquid pressure controllers ────────────────────────
    # Secondary letters compose per the 2018 Part Number Guide + the
    # PCD-Series spec sheet (DOC-SPECS-PCD Rev 11, Mar 2024):
    #   3  = remote pressure-sense port (PC only)
    #   AS = low-range all-sensor (< 1 PSIG/A/D; PC only)
    #   D  = dual valves (closed-volume PCD family)
    #   H  = Hammerhead
    #   P  = Pneutronics (large valve, flowing process;
    #        double-P ``PCPD-`` is dual Pneutronics closed volume)
    #   R  = Rolamite (high-flow, available in all controllers)
    #   S  = stainless — **context-sensitive**:
    #        * on single-valve MFC/MFM/PC (MCS/MS/PCS) = stainless
    #          sensor for corrosive gases; gas-only per the 2018 guide
    #        * on dual-valve PCD family (PCDS, PCRDS, PCRD3S) = full
    #          stainless body → gas **and liquid** compatibility per
    #          the 2024 PCD-Series spec sheet ("PCDS: Compatible with
    #          all non-corrosive gases **and liquids**, and many
    #          corrosive gases"). PCD-family S-suffix rules land at
    #          ``Medium.GAS | Medium.LIQUID``.
    # ``E`` precedes ``PC`` (EPC-/EPCD-) for enclosed-valve variants.
    # Non-dual flowing-process PC-* stays at Medium.GAS pending
    # verification of fluid-select command coverage (§16.1 TODO).
    ModelRule("PCRD3S-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS | Medium.LIQUID, _PC_MAP),
    ModelRule("PCRD3-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCRDS-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS | Medium.LIQUID, _PC_MAP),
    ModelRule("EPCD-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCD3-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCRD-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCR3-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCAS-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCPD-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCDS-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS | Medium.LIQUID, _PC_MAP),
    ModelRule("PC3-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCS-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCD-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCH-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCP-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PCR-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("EPC-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("IVC-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    ModelRule("PC-", DeviceKind.PRESSURE_CONTROLLER, Medium.GAS, _PC_MAP),
    # ── Gas pressure meters ──────────────────────────────────────────
    # Meter-side secondary letters: B=battery (PB), S=stainless (PS).
    # ``E`` precedes ``P`` (EP-) for the enclosed variant. No valve /
    # compound letters on meter prefixes. (``AS`` low-range variant is
    # controller-only per the 2018 guide — PCS/PCAS examples.)
    ModelRule("PB-", DeviceKind.PRESSURE_METER, Medium.GAS, _PM_MAP),
    ModelRule("PS-", DeviceKind.PRESSURE_METER, Medium.GAS, _PM_MAP),
    ModelRule("EP-", DeviceKind.PRESSURE_METER, Medium.GAS, _PM_MAP),
    ModelRule("P-", DeviceKind.PRESSURE_METER, Medium.GAS, _PM_MAP),
    # ── Liquid flow controllers (laminar DP) ─────────────────────────
    ModelRule("LCR-", DeviceKind.FLOW_CONTROLLER, Medium.LIQUID, _FC_MAP),
    ModelRule("LC-", DeviceKind.FLOW_CONTROLLER, Medium.LIQUID, _FC_MAP),
    # ── Liquid flow meters ───────────────────────────────────────────
    ModelRule("LB-", DeviceKind.FLOW_METER, Medium.LIQUID, _FM_MAP),
    ModelRule("L-", DeviceKind.FLOW_METER, Medium.LIQUID, _FM_MAP),
    # ── Coriolis (CODA line) — K-family, per the Alicat CODA Part
    # Number Decoder (Feb 2024). Family is the first part-number field
    # and encodes kind deterministically:
    #   K   = Meter         (current naming — post-decoder)
    #   KM  = Meter         (legacy pre-decoder naming for the single
    #                        older meter product line; preserved for
    #                        back-compat with fielded units)
    #   KC  = Controller    (valve-based)
    #   KF  = Pump controller
    #   KG  = Pump System   (pump-based controller)
    # The decoder does **not** carry a medium field; current
    # understanding is that every CODA unit handles both gas and
    # liquid, so we default to the widest media (``GAS | LIQUID``).
    # Users whose unit is configured for a single medium narrow via
    # ``assume_media`` on ``open_device``. Valve / pump detail on
    # KC/KF/KG is a facade-layer refinement that may be added later;
    # current identification stops at kind resolution. Ordering:
    # 2-letter family codes before the 1-letter ``K-`` so the bare meter
    # prefix doesn't swallow controller variants.
    ModelRule("KM-", DeviceKind.FLOW_METER, Medium.GAS | Medium.LIQUID, _FM_MAP),
    ModelRule("KC-", DeviceKind.FLOW_CONTROLLER, Medium.GAS | Medium.LIQUID, _FC_MAP),
    ModelRule("KF-", DeviceKind.FLOW_CONTROLLER, Medium.GAS | Medium.LIQUID, _FC_MAP),
    ModelRule("KG-", DeviceKind.FLOW_CONTROLLER, Medium.GAS | Medium.LIQUID, _FC_MAP),
    ModelRule("K-", DeviceKind.FLOW_METER, Medium.GAS | Medium.LIQUID, _FM_MAP),
)


def _rule_for_model(model: str) -> ModelRule | None:
    """Return the first :class:`ModelRule` whose prefix matches ``model``.

    Returns ``None`` when no rule claims the prefix — the caller (factory
    identification path) then falls back to :class:`DeviceKind.UNKNOWN`
    plus :attr:`Medium.NONE`, which forces users to supply ``model_hint``
    or accept that command gating will reject nearly everything.
    """
    for rule in MODEL_RULES:
        if model.startswith(rule.prefix):
            return rule
    return None


def _kind_for_model(model: str) -> DeviceKind:
    """Resolve the :class:`DeviceKind` for ``model`` from the prefix table.

    Returns :attr:`DeviceKind.UNKNOWN` when the prefix doesn't appear in
    :data:`MODEL_RULES`. Every currently-listed prefix resolves kind
    deterministically (CODA, BASIS, and every other family all expose
    meter vs. controller as the first part-number field), so there is
    no probe / fallback path here today — it would re-appear as a
    ``kind_probe`` field on :class:`ModelRule` when a future prefix
    turns out to be kind-ambiguous.
    """
    rule = _rule_for_model(model)
    if rule is None:
        return DeviceKind.UNKNOWN
    return rule.kind


def _media_for_model(model: str) -> Medium:
    """Resolve the default :class:`Medium` flag for ``model``.

    Returns :attr:`Medium.NONE` for prefixes not in :data:`MODEL_RULES`
    — the media gate then rejects every medium-specific command on the
    device, which is the right behaviour for an unidentified model:
    fail loud until the user steers with ``model_hint``.
    """
    rule = _rule_for_model(model)
    if rule is None:
        return Medium.NONE
    return rule.media


def device_class_for(info: DeviceInfo) -> type[Device]:
    """Return the concrete :class:`Device` subclass for ``info``.

    Routing is prefix-based via :data:`MODEL_RULES`. Every current rule
    resolves kind deterministically, so the facade class is
    ``rule.device_cls_map[rule.kind]``.

    Unknown prefixes and kinds not in the rule's map fall back to the
    generic :class:`Device` — the session's :class:`DeviceKind` /
    :class:`Medium` gates still fire, so the fallback is safe: commands
    that don't list ``UNKNOWN`` in ``device_kinds`` simply refuse to
    dispatch.
    """
    rule = _rule_for_model(info.model)
    if rule is None:
        return Device
    return rule.device_cls_map.get(rule.kind, Device)


# ---------------------------------------------------------------------------
# ??M* → DeviceInfo field mapping
# ---------------------------------------------------------------------------


# Verified against real captures from an 8v17 (V8/V9, MCR-200SLPM-D) and a
# V10 (MC-500SCCM-D, 10v20.0-R24) on 2026-04-17 — see design §16.6.
# Both devices use the same 0-indexed scheme (M00..M09) with embedded
# human-readable labels (`Model Number `, `Serial Number `, ...) between
# the code and the value. The raw payload is preserved in `by_code`; this
# mapping defines which codes carry semantic device fields and which
# label prefix (if any) to strip to recover the bare value.
#
# The label prefix may be `None` when the entire payload is the value (no
# embedded label) — matches M00..M03 (manufacturer info, contact strings).
_MFG_CODE_TO_FIELD: Final[Mapping[int, tuple[str, str | None]]] = MappingProxyType(
    {
        0: ("manufacturer", None),
        4: ("model", "Model Number"),
        5: ("serial", "Serial Number"),
        6: ("manufactured", "Date Manufactured"),
        7: ("calibrated", "Date Calibrated"),
        8: ("calibrated_by", "Calibrated By"),
        9: ("software", "Software Revision"),
    },
)


# GP firmware uses a different ``??M*`` dialect (design §16.6.8): codes
# M0..M8 (single-digit) with shorter embedded labels and no dedicated
# ``Date Calibrated`` slot. Captured on a GP07R100 (MC-100SCCM-D,
# manufactured 2012):
#   M0: Alicat Scientific Inc.    M1: (blank)     M2: Ph …    M3: Fax …
#   M4: Mdl <model>               M5: Serial No. <serial>
#   M6: Date Mfg. <MM/DD/YYYY>    M7: Calibrated By <initials>
#   M8: Software <GP…>
_MFG_CODE_TO_FIELD_GP: Final[Mapping[int, tuple[str, str | None]]] = MappingProxyType(
    {
        0: ("manufacturer", None),
        4: ("model", "Mdl"),
        5: ("serial", "Serial No."),
        6: ("manufactured", "Date Mfg."),
        7: ("calibrated_by", "Calibrated By"),
        8: ("software", "Software"),
    },
)


def _strip_label(payload: str, label: str | None) -> str:
    """Strip a known label prefix from a manufacturing-info payload.

    Embedded labels are space-padded for fixed-width alignment on the
    device LCD (`Date Calibrated   03/02/2025`). We strip the bare label
    word(s) followed by any whitespace, then return the trimmed value.
    If ``label`` is ``None`` the payload is returned unchanged. If the
    payload doesn't start with ``label`` the original payload is returned
    so unknown firmware variants still surface a value rather than ``None``.
    """
    if not label:
        return payload
    if payload.startswith(label):
        return payload[len(label) :].lstrip()
    return payload


def _is_gp_manufacturing_info(info: ManufacturingInfo) -> bool:
    """Detect the GP ``??M*`` dialect by label patterns in code 4 / 8.

    On GP the model line is ``Mdl <model>`` and the software line is
    ``Software <GP…>``; canonical firmware emits ``Model Number …`` and
    ``Software Revision …`` respectively. Either signal alone is enough;
    we use both so a partially-populated device still classifies.
    """
    model_payload = info.by_code.get(4, "")
    software_payload = info.by_code.get(8, "")
    return model_payload.startswith("Mdl") or software_payload.startswith("Software ")


def _extract_named_fields(info: ManufacturingInfo) -> dict[str, str | None]:
    """Apply the verified M-code mapping; unknown fields become ``None``.

    The GP dialect uses a different field layout — detect it and apply
    the GP mapping instead of the canonical one.
    """
    mapping = _MFG_CODE_TO_FIELD_GP if _is_gp_manufacturing_info(info) else _MFG_CODE_TO_FIELD
    # Start with every known field from BOTH mappings so callers see a
    # stable key set regardless of which dialect the device emits.
    all_fields = {field for field, _lbl in _MFG_CODE_TO_FIELD.values()}
    all_fields.update(field for field, _lbl in _MFG_CODE_TO_FIELD_GP.values())
    extracted: dict[str, str | None] = dict.fromkeys(all_fields)
    for code, (field_name, label) in mapping.items():
        payload = info.by_code.get(code)
        if payload is None or payload == "":
            continue
        extracted[field_name] = _strip_label(payload, label)
    return extracted


# ---------------------------------------------------------------------------
# Capability probing
# ---------------------------------------------------------------------------


async def probe_capabilities(
    client: AlicatProtocolClient,
    unit_id: str,
    info: DeviceInfo,
) -> tuple[Capability, Mapping[Capability, ProbeOutcome]]:
    """Probe each :class:`Capability` flag on the device.

    Real probes implemented so far:

    - :attr:`Capability.BAROMETER` — ``FPF 15`` on any numeric-family
      device. "Present" iff the reply has ``value > 0`` *and*
      ``unit_label != "---"`` (design §16.6.3). The device emits
      ``A <zero> 1 ---`` when the statistic is not supported, which
      has to be disambiguated from a real reading. Note that a
      positive ``BAROMETER`` probe on a flow controller does NOT
      imply :attr:`Capability.TAREABLE_ABSOLUTE_PRESSURE` — the two
      dissociate in practice (design §16.6.7 / Capability docstring).
    - :attr:`Capability.SECONDARY_PRESSURE` — identical rule applied
      to ``FPF 344`` (second absolute pressure). Trying ``344`` covers
      the common second-pressure-sensor configuration; future work can
      extend to ``352`` / ``360`` if devices surface those instead.

    Stubs still fail-closed:

    - :attr:`Capability.TAREABLE_ABSOLUTE_PRESSURE` — no safe probe;
      test-writing ``PC`` would re-zero the abs sensor. Users with a
      pressure meter/controller that supports ``PC`` opt in via
      ``assume_capabilities=Capability.TAREABLE_ABSOLUTE_PRESSURE``.

    - :attr:`Capability.MULTI_VALVE` / :attr:`THIRD_VALVE` —
      ``VD`` returns four columns unconditionally across meter and
      single-valve-controller devices alike (design §16.6.6), so the
      earlier column-count plan is invalidated. Left absent until a
      valve-count signal surfaces.
    - :attr:`Capability.TOTALIZER`, analog I/O flags, display, remote
      tare, bidirectional — no hardware-validated probe strategy yet.

    GP family skips every probe: we have no GP capture and the primer
    doesn't document ``FPF`` there, so assuming absence is the safe
    default. Callers can still union capabilities in via
    ``assume_capabilities=...`` on :func:`open_device`.

    Fails *closed* on every flag the probe doesn't positively confirm —
    design §5.9's "default-absent" policy: a probe that can't answer
    should never falsely claim the hardware is present.
    """
    report: dict[Capability, ProbeOutcome] = dict.fromkeys(
        _iter_single_capability_flags(),
        "absent",
    )

    if info.firmware.family is FirmwareFamily.GP:
        # No FPF on GP; leave every flag absent.
        _logger.info(
            "probe_capabilities.gp_skip",
            extra={
                "unit_id": unit_id,
                "firmware": str(info.firmware),
                "reason": "gp_family_no_fpf",
                "resolved": "NONE",
            },
        )
        return Capability.NONE, MappingProxyType(report)

    probe_session = _bootstrap_session(client, unit_id, firmware_override=info.firmware)

    baro_flag, baro_outcome = await _probe_fpf_capability(
        probe_session,
        statistic=_STAT_BARO_PRESS,
        present_flag=Capability.BAROMETER,
    )
    report[Capability.BAROMETER] = baro_outcome

    sec_flag, sec_outcome = await _probe_fpf_capability(
        probe_session,
        statistic=_STAT_ABS_PRESS_SECOND,
        present_flag=Capability.SECONDARY_PRESSURE,
    )
    report[Capability.SECONDARY_PRESSURE] = sec_outcome

    resolved = baro_flag | sec_flag
    # Structured INFO per design §5.19 / §15.2: one log event per
    # identification summarising the capability-probe outcome. Users
    # wiring up dashboards / dashboards-by-error want to see this as
    # a single row per device rather than scraping per-probe entries.
    _logger.info(
        "probe_capabilities.result",
        extra={
            "unit_id": unit_id,
            "firmware": str(info.firmware),
            "model": info.model,
            "resolved": resolved.name or str(resolved),
            "present": [c.name for c in _iter_single_capability_flags() if report[c] == "present"],
            "outcomes": {c.name: report[c] for c in _iter_single_capability_flags() if c.name},
        },
    )
    return resolved, MappingProxyType(report)


# Resolved Statistic members for the pressure probes. Resolved once at
# module-import time so a missing member surfaces as an ImportError-time
# failure (codes.json / _codes_gen.py drift) rather than a runtime one.
_STAT_BARO_PRESS: Final[Statistic] = STATISTIC_BY_CODE[15]
_STAT_ABS_PRESS_SECOND: Final[Statistic] = STATISTIC_BY_CODE[344]


async def _probe_fpf_capability(
    probe_session: Session,
    *,
    statistic: Statistic,
    present_flag: Capability,
) -> tuple[Capability, ProbeOutcome]:
    """Issue ``FPF <statistic>`` and classify the reply.

    Returns the ``(flag_or_NONE, outcome)`` pair: ``flag_or_NONE`` is
    ``present_flag`` iff the reply indicates the statistic is present
    on the device, ``Capability.NONE`` otherwise. ``outcome`` is a
    :data:`ProbeOutcome` suitable for the probe report.
    """
    try:
        result = await probe_session.execute(
            FULL_SCALE_QUERY,
            FullScaleQueryRequest(statistic=statistic),
        )
    except AlicatCommandRejectedError:
        return Capability.NONE, "rejected"
    except AlicatTimeoutError:
        return Capability.NONE, "timeout"
    except AlicatParseError:
        return Capability.NONE, "parse_error"
    except AlicatError:
        # Any other library error (validation, firmware gate, ...) —
        # default to absent so a probe failure never claims the
        # capability is present.
        return Capability.NONE, "absent"

    # Design §16.6.3: the absent-pattern reply is ``A <zero> 1 ---``
    # (value zero *and* unit_label == "---"). A real reading has a
    # non-zero value and a real label.
    if result.value > 0 and result.unit_label != "---":
        return present_flag, "present"
    return Capability.NONE, "absent"


def _iter_single_capability_flags() -> list[Capability]:
    """Every :class:`Capability` member except ``NONE`` — the probe set."""
    return [flag for flag in Capability if flag is not Capability.NONE]


# ---------------------------------------------------------------------------
# Stream recovery
# ---------------------------------------------------------------------------


# Seconds to passively listen for unsolicited bytes before issuing the
# first command. Empirically ~100 ms is enough to catch a device that was
# left streaming by a prior process (which would otherwise push frames
# into the next command's reply buffer and corrupt identification).
_STREAM_RECOVERY_WINDOW_S: Final[float] = 0.1
_STREAM_RECOVERY_DRAIN_WINDOW_S: Final[float] = 0.1
#: Hard cap on bytes the recovery sniff / drain can consume before
#: returning. A continuously-streaming device at the 50 ms default
#: NCS rate emits ~48-byte frames every 50 ms, so this cap holds
#: ~5 frames or ~250 ms of bus traffic — plenty for telemetry, and
#: small enough that ``open_device`` can't deadlock on a hot stream.
#: Hardware-validation finding, 2026-04-17: uncapped ``read_available``
#: hung the factory on a 10v20 left in streaming mode.
_STREAM_RECOVERY_MAX_BYTES: Final[int] = 256


async def _recover_from_stream(transport: Transport, unit_id: str) -> None:
    r"""Sniff the transport, then unconditionally issue stop-stream and drain.

    The stop-stream bytes are ``@@ {unit_id}\r`` — a prefix-less token
    (the factory issues raw bytes here because the client may be in
    an indeterminate state before identification completes).

    An earlier version of this function only issued the stop-stream
    when the sniff found bytes. Hardware validation on 2026-04-17 surfaced a
    state — observed on an 8v17 after a previous diag-capture session —
    where the device wasn't actively streaming during the sniff window
    but VE still hung until ``@@ <unit_id>`` was sent. Sending it
    unconditionally costs one extra wire write (~10 ms at 19200 baud)
    plus a brief drain; on a polling-mode device with a matching
    unit_id, ``@@ <id>`` is a documented no-op. Cheap insurance.

    Both the pre-stop sniff and the post-stop drain cap their reads at
    :data:`_STREAM_RECOVERY_MAX_BYTES`. Hardware validation (2026-04-17)
    found the uncapped form deadlocks the factory on a device
    streaming at its 50 ms default rate: the stream never goes idle
    for the 100 ms window ``read_available`` wants, so the loop runs
    forever. The sniff result is captured for telemetry only — the
    cap is cheap insurance that ``open_device`` can't hang on a
    continuously-streaming device.
    """
    try:
        prelude = await transport.read_available(
            idle_timeout=_STREAM_RECOVERY_WINDOW_S,
            max_bytes=_STREAM_RECOVERY_MAX_BYTES,
        )
        del prelude  # captured for telemetry only; nothing branches on it
    except AlicatTransportError:
        # If the transport can't even do a passive read, identification
        # will fail downstream with a clearer error; don't mask it here.
        return
    # Always issue stop-stream + drain — see docstring for rationale.
    await transport.write(f"@@ {unit_id}\r".encode("ascii"), timeout=0.5)
    try:
        await transport.read_available(
            idle_timeout=_STREAM_RECOVERY_DRAIN_WINDOW_S,
            max_bytes=_STREAM_RECOVERY_MAX_BYTES,
        )
    except AlicatTransportError:
        return


# ---------------------------------------------------------------------------
# identify_device
# ---------------------------------------------------------------------------


async def identify_device(
    client: AlicatProtocolClient,
    unit_id: str = "A",
    *,
    model_hint: str | None = None,
) -> DeviceInfo:
    """Run ``VE`` → (optional) ``??M*`` → classify; return :class:`DeviceInfo`.

    Capabilities are *not* populated here; call :func:`probe_capabilities`
    separately and merge via :func:`dataclasses.replace`. That split
    matches design §5.9 and keeps the two concerns testable in isolation.

    Args:
        client: A wired :class:`AlicatProtocolClient`. Identification
            issues ``VE`` and (conditionally) ``??M*``, both of which
            flow through the client's single-in-flight lock.
        unit_id: Polling unit id (``"A"``..``"Z"``).
        model_hint: Required when ``??M*`` can't be reached (GP family,
            V1_V7, or any device that rejects the command). Ignored when
            ``??M*`` succeeds. Raises :class:`AlicatConfigurationError`
            if identification reaches the fallback branch without a hint.
    """
    # The session's firmware gating references self._info.firmware, so
    # to drive VE we need a throwaway DeviceInfo first. We populate the
    # rest after parsing VE.
    ve_session = _bootstrap_session(client, unit_id)
    try:
        ve_result = await ve_session.execute(VE_QUERY, VeRequest())
    except AlicatTimeoutError:
        # GP firmware does not implement VE (design §16.6.8, confirmed on
        # a GP07R100 capture 2026-04-17). Fall through to a VE-less
        # identification path that drives ``??M*`` directly and
        # synthesises a GP :class:`FirmwareVersion` from the M8
        # ``Software`` field. If ``??M*`` also fails, the caller must
        # supply ``model_hint`` and we build a minimal GP DeviceInfo.
        return await _identify_without_ve(client, unit_id, model_hint=model_hint)
    firmware = ve_result.firmware
    firmware_date = ve_result.firmware_date

    # Try ??M* on any family the reachability gate allows. The device
    # may still reject (older numeric firmware that was never updated, or
    # a custom revision); on rejection / parse error we fall through to
    # the model_hint path. Design §16.6 — the primer's 8v28 floor was
    # observed wrong on real 8v17 hardware.
    if _manufacturing_info_reachable(firmware):
        mfg_session = _bootstrap_session(
            client,
            unit_id,
            firmware_override=firmware,
        )
        mfg_info: ManufacturingInfo | None
        try:
            mfg_info = await mfg_session.execute(
                MANUFACTURING_INFO,
                ManufacturingInfoRequest(),
            )
        except (
            AlicatCommandRejectedError,
            AlicatTimeoutError,
            AlicatParseError,
        ):
            # Device doesn't actually support ??M* despite the family
            # check. Fall through to the model_hint path below.
            mfg_info = None
        if mfg_info is not None:
            named = _extract_named_fields(mfg_info)
            model = named["model"]
            if model is None:
                if model_hint is None:
                    raise AlicatConfigurationError(
                        f"??M* returned no model field (M04) and no model_hint was supplied; "
                        f"raw by_code={dict(mfg_info.by_code)!r}",
                        context=ErrorContext(
                            command_name="??M*",
                            unit_id=unit_id,
                            raw_response=None,
                        ),
                    )
                model = model_hint
            return DeviceInfo(
                unit_id=unit_id,
                manufacturer=named["manufacturer"],
                model=model,
                serial=named["serial"],
                manufactured=named["manufactured"],
                calibrated=named["calibrated"],
                calibrated_by=named["calibrated_by"],
                software=named["software"] or str(firmware),
                firmware=firmware,
                firmware_date=firmware_date,
                kind=_kind_for_model(model),
                media=_media_for_model(model),
                capabilities=Capability.NONE,
            )

    # Fallback: ??M* unsupported / rejected, or family says skip.
    if model_hint is None:
        raise AlicatConfigurationError(
            f"Firmware {firmware} does not support ??M*; supply model_hint=... "
            f"to open_device / identify_device to synthesise DeviceInfo "
            f"(GP family / pre-8v28 devices reach this branch — see design §5.9).",
            context=ErrorContext(
                command_name="identify_device",
                unit_id=unit_id,
                firmware=firmware,
            ),
        )
    return DeviceInfo(
        unit_id=unit_id,
        manufacturer=None,
        model=model_hint,
        serial=None,
        manufactured=None,
        calibrated=None,
        calibrated_by=None,
        software=str(firmware),
        firmware=firmware,
        firmware_date=firmware_date,
        kind=_kind_for_model(model_hint),
        media=_media_for_model(model_hint),
        capabilities=Capability.NONE,
    )


async def _identify_without_ve(
    client: AlicatProtocolClient,
    unit_id: str,
    *,
    model_hint: str | None,
) -> DeviceInfo:
    """GP-specific identification: drive ``??M*`` directly, synthesise firmware.

    GP firmware doesn't implement ``VE``. This path probes ``??M*``
    (which GP *does* support, prefix-less — design §16.6.8), pulls the
    model + software string out of the reply, and synthesises a
    :class:`FirmwareVersion` whose family is :attr:`FirmwareFamily.GP`.
    If ``??M*`` also fails, we need ``model_hint`` to proceed.
    """
    gp_placeholder = FirmwareVersion(
        family=FirmwareFamily.GP,
        major=0,
        minor=0,
        raw="GP",
    )
    mfg_session = _bootstrap_session(
        client,
        unit_id,
        firmware_override=gp_placeholder,
    )
    mfg_info: ManufacturingInfo | None
    try:
        mfg_info = await mfg_session.execute(
            MANUFACTURING_INFO,
            ManufacturingInfoRequest(),
        )
    except (AlicatCommandRejectedError, AlicatTimeoutError, AlicatParseError):
        mfg_info = None

    if mfg_info is not None:
        named = _extract_named_fields(mfg_info)
        model = named["model"] or model_hint
        if model is None:
            raise AlicatConfigurationError(
                "Device did not respond to VE and ??M* returned no model "
                "(M04 line missing or unparseable); supply model_hint=... to "
                "open_device / identify_device.",
                context=ErrorContext(
                    command_name="identify_device",
                    unit_id=unit_id,
                ),
            )
        software = named["software"] or "GP"
        try:
            firmware = FirmwareVersion.parse(software)
        except AlicatParseError:
            firmware = gp_placeholder
        return DeviceInfo(
            unit_id=unit_id,
            manufacturer=named["manufacturer"],
            model=model,
            serial=named["serial"],
            manufactured=named["manufactured"],
            calibrated=named.get("calibrated"),
            calibrated_by=named["calibrated_by"],
            software=software,
            firmware=firmware,
            firmware_date=None,
            kind=_kind_for_model(model),
            media=_media_for_model(model),
            capabilities=Capability.NONE,
        )

    # VE timed out AND ??M* failed — the caller must provide a model_hint.
    if model_hint is None:
        raise AlicatConfigurationError(
            "Device did not respond to VE or ??M* (likely GP firmware with "
            "no response on either); supply model_hint=... so DeviceInfo "
            "can be synthesised from the prefix alone.",
            context=ErrorContext(
                command_name="identify_device",
                unit_id=unit_id,
            ),
        )
    return DeviceInfo(
        unit_id=unit_id,
        manufacturer=None,
        model=model_hint,
        serial=None,
        manufactured=None,
        calibrated=None,
        calibrated_by=None,
        software="GP",
        firmware=gp_placeholder,
        firmware_date=None,
        kind=_kind_for_model(model_hint),
        media=_media_for_model(model_hint),
        capabilities=Capability.NONE,
    )


def _manufacturing_info_reachable(firmware: FirmwareVersion) -> bool:
    """Whether the current firmware should be *attempted* with ``??M*``.

    Hardware validation on 2026-04-17 caught ``??M*`` working on three numeric
    devices spanning V1_V7 (5v12), V8/V9 (8v17), and V10 (10v04, 10v20)
    — *despite* the Alicat Serial Primer listing 8v28 as the minimum
    (design §16.6 / §16.6.2). The primer is wrong; every numeric-family
    device tested implements ``??M*`` with the canonical M00..M09 dialect.

    Strategy: attempt on every numeric family. Call sites wrap in
    try/except so a ``?`` rejection or timeout falls through to
    ``model_hint``. GP is the only family we still skip — the primer
    says no and we have no GP capture to test against.
    """
    return firmware.family in NUMERIC_FAMILIES


def _bootstrap_session(
    client: AlicatProtocolClient,
    unit_id: str,
    *,
    firmware_override: FirmwareVersion | None = None,
) -> Session:
    """Minimal session for the VE/??M* steps before full DeviceInfo exists.

    The :class:`Session` gates on firmware family / range / device kind /
    capability, so during identification we hand it a permissive
    ``DeviceInfo`` shell built from the VE result (or a zero-value shell
    before VE has even run — VE has no firmware gating, so that's safe).
    """
    # Before VE, we have no firmware. Session's constructor needs a
    # FirmwareVersion, but VE_QUERY has no firmware_families or
    # min_firmware, so any family/version passes its gate. We use
    # V10 10v05 as the placeholder because it reflects the "typical"
    # modern device and, importantly, satisfies MANUFACTURING_INFO's
    # min_firmware gate when we reuse this bootstrap to dispatch ??M*.
    placeholder_fw: FirmwareVersion = (
        firmware_override
        if firmware_override is not None
        else FirmwareVersion(FirmwareFamily.V10, 10, 5, "10v05")
    )
    stub = DeviceInfo(
        unit_id=unit_id,
        manufacturer=None,
        model="unknown",  # temporary — session's device-kind gate is
        #                  bypassed because both commands list all kinds
        serial=None,
        manufactured=None,
        calibrated=None,
        calibrated_by=None,
        software=str(placeholder_fw),
        firmware=placeholder_fw,
        firmware_date=None,
        kind=DeviceKind.UNKNOWN,
        # Permissive media during identification — VE/??M* are
        # medium-agnostic (default ``GAS | LIQUID`` from ``Command`` base),
        # so the bootstrap must present a device whose media intersects
        # the command's. Narrowed to the prefix-derived value as soon as
        # ``??M*`` resolves the real model.
        media=Medium.GAS | Medium.LIQUID,
        capabilities=Capability.NONE,
    )
    return Session(
        client,
        unit_id=unit_id,
        info=stub,
        port_label=getattr(client, "label", None),
    )


# ---------------------------------------------------------------------------
# open_device
# ---------------------------------------------------------------------------


@asynccontextmanager
async def open_device(
    port: str | Transport | AlicatProtocolClient,
    *,
    unit_id: str = "A",
    serial: SerialSettings | None = None,
    timeout: float = 0.5,
    recover_from_stream: bool = True,
    model_hint: str | None = None,
    assume_capabilities: Capability = Capability.NONE,
    assume_media: Medium | None = None,
) -> AsyncGenerator[Device]:
    """Open a fully-identified :class:`Device` for ``async with`` use.

    The caller's ``port`` determines the lifecycle the context manager
    takes ownership of:

    - ``str`` (``"/dev/ttyUSB0"`` etc.) — build a
      :class:`SerialTransport` from ``serial`` (or defaults), open it,
      wrap in an :class:`AlicatProtocolClient`, close both on exit.
    - :class:`Transport` — wrap in a new
      :class:`AlicatProtocolClient`; the transport's open/close is the
      caller's responsibility (we never close a transport we didn't
      open).
    - :class:`AlicatProtocolClient` — use as-is; neither transport nor
      client is closed on exit. Stream recovery is skipped because the
      factory doesn't have access to the underlying transport.

    The ``assume_capabilities`` override is union'd onto the probed set
    per design §5.9 — the factory never *subtracts* flags, because
    silently masking hardware the device reports as present is exactly
    the failure mode capability probing exists to avoid.

    The ``assume_media`` override **replaces** the prefix-derived media
    (design §5.9a). Medium answers "how is this specific unit
    configured," not "what can the hardware do" — the common correction
    is to narrow from a permissive prefix default to the single medium
    the unit was actually ordered locked to. The K-family CODA prefixes
    default to ``Medium.GAS | Medium.LIQUID`` because the part-number
    decoder encodes kind but not medium; other future order-configurable
    prefixes can adopt the same pattern. A replace policy also
    future-proofs the model: any new ambiguous prefix drops into
    :data:`MODEL_RULES` with the widest default, and users narrow at
    open time.
    """
    owns_transport = False
    owns_client = False
    transport: Transport | None = None

    if isinstance(port, AlicatProtocolClient):
        client = port
    elif isinstance(port, str):
        settings = serial if serial is not None else SerialSettings(port=port)
        transport = SerialTransport(settings)
        client = AlicatProtocolClient(transport, default_timeout=timeout)
        owns_transport = True
        owns_client = True
    else:
        # Duck-typed Transport (Protocol isn't runtime-checkable).
        transport = port
        client = AlicatProtocolClient(transport, default_timeout=timeout)
        owns_client = True

    try:
        if transport is not None and not transport.is_open:
            await transport.open()
        if recover_from_stream and transport is not None:
            await _recover_from_stream(transport, unit_id)

        info = await identify_device(client, unit_id, model_hint=model_hint)
        probed_caps, probe_report = await probe_capabilities(client, unit_id, info)
        merged_caps = probed_caps | assume_capabilities
        # Medium resolution: prefix-derived by default; ``assume_media``
        # **replaces** (not unions — design §5.9a). Rationale: the common
        # correction is narrowing a permissive prefix default
        # (``Medium.GAS | Medium.LIQUID`` for K-family CODA prefixes and similar
        # whose medium varies by order-time configuration) to the
        # single medium the unit was actually ordered locked to.
        resolved_media = info.media if assume_media is None else assume_media
        info = dataclasses.replace(
            info,
            media=resolved_media,
            capabilities=merged_caps,
            probe_report=probe_report,
        )

        data_frame_format = await _probe_data_frame_format(client, info, unit_id)

        # Per design §10.1: bind per-field engineering units
        # from ``DCU`` where ``??D*`` didn't surface a recognisable label,
        # then populate ``DeviceInfo.full_scale`` from ``FPF`` so
        # setpoint and similar facades can range-check pre-I/O (design
        # §5.20.2). Both probes iterate the data-frame fields, are
        # best-effort per statistic, and never fail the open — a device
        # that rejects one probe just leaves that slot unresolved.
        data_frame_format = await _bind_field_units(
            client,
            info,
            unit_id,
            data_frame_format,
        )
        info = await _probe_full_scales(
            client,
            info,
            unit_id,
            data_frame_format,
        )

        port_label = _resolve_port_label(port, transport)
        session = Session(
            client,
            unit_id=unit_id,
            info=info,
            data_frame_format=data_frame_format,
            port_label=port_label,
        )
        # Pre-cache the loop-control variable for controllers so the
        # first setpoint call can already range-check. Best-effort:
        # firmware without ``LV`` (V1_V7, pre-9v00 V8_V9) leaves the
        # cache ``None`` and setpoint simply skips the range check.
        await _prefetch_loop_control_variable(session, info)

        device_cls = device_class_for(info)
        device = device_cls(session)

        try:
            yield device
        finally:
            await device.close()
    finally:
        del owns_client  # not load-bearing — we never open client separately
        if owns_transport and transport is not None and transport.is_open:
            await transport.close()


async def _probe_data_frame_format(
    client: AlicatProtocolClient,
    info: DeviceInfo,
    unit_id: str,
) -> DataFrameFormat:
    """Run ``??D*`` against the identified device, return the cached format."""
    session = Session(client, unit_id=unit_id, info=info, port_label=None)
    return await session.execute(DATA_FRAME_FORMAT_QUERY, DataFrameFormatRequest())


# Non-numeric ``??D*`` type tokens that can't carry an engineering unit.
# ``??D*`` uses ``string`` for text columns (Unit ID, Gas, Status/Error
# mnemonics) and ``char`` on the V1_V7 dialect for the same role. Fields
# with these types are always skipped by DCU / FPF probes.
_NON_NUMERIC_TYPE_TOKENS: Final[frozenset[str]] = frozenset({"string", "char"})


def _is_numeric_field(field: DataFrameField) -> bool:
    """Whether ``field`` carries a numeric reading (probe-worthy)."""
    if field.statistic is None or field.conditional:
        return False
    return field.type_name.lower() not in _NON_NUMERIC_TYPE_TOKENS


async def _bind_field_units(
    client: AlicatProtocolClient,
    info: DeviceInfo,
    unit_id: str,
    fmt: DataFrameFormat,
) -> DataFrameFormat:
    """Fill in :attr:`DataFrameField.unit` via ``DCU`` where ``??D*`` left it ``None``.

    The ``??D*`` parser already binds units inline when the reply carries
    a recognisable label (design §16.6 — ``PSIA`` / ``CCM`` / ``SCCM``
    etc.). On firmware variants that advertise the legacy ``na`` unit
    token, an unrecognised label, or no trailing unit column at all,
    ``field.unit`` stays ``None``; ``DCU <stat>`` then fills the gap.

    Gating is implicit in the ``DCU`` command spec (V10 10v05+; earlier
    firmware raises :class:`AlicatFirmwareError` pre-I/O and we swallow
    it here). Each per-statistic probe is wrapped so a timeout /
    rejection / parse error on one field never blocks the rest: the
    field is left unresolved and identification continues.
    """
    if not any(_is_numeric_field(f) and f.unit is None for f in fmt.fields):
        return fmt

    session = Session(client, unit_id=unit_id, info=info, port_label=None)
    new_fields: list[DataFrameField] = []
    for field in fmt.fields:
        if field.unit is not None or not _is_numeric_field(field):
            new_fields.append(field)
            continue
        assert field.statistic is not None  # noqa: S101 — _is_numeric_field gate
        unit = await _probe_dcu_unit(session, field.statistic)
        if unit is None:
            new_fields.append(field)
            continue
        new_fields.append(dataclasses.replace(field, unit=unit))

    return dataclasses.replace(fmt, fields=tuple(new_fields))


async def _probe_dcu_unit(
    session: Session,
    statistic: Statistic,
) -> Unit | None:
    """Issue ``DCU <statistic>`` and return the resolved :class:`Unit`, or ``None``.

    Swallows the library-typed errors users would get if they invoked
    ``DCU`` directly on a device that doesn't support it:
    :class:`AlicatFirmwareError` (pre-V10 / pre-10v05),
    :class:`AlicatCommandRejectedError` (device-side ``?``),
    :class:`AlicatTimeoutError`, :class:`AlicatParseError`. Any of
    those leaves the field unresolved rather than failing identification.
    """
    try:
        result = await session.execute(
            ENGINEERING_UNITS,
            EngineeringUnitsRequest(statistic=statistic),
        )
    except (
        AlicatCommandRejectedError,
        AlicatTimeoutError,
        AlicatParseError,
        AlicatError,
    ):
        return None
    return result.unit


async def _probe_full_scales(
    client: AlicatProtocolClient,
    info: DeviceInfo,
    unit_id: str,
    fmt: DataFrameFormat,
) -> DeviceInfo:
    """Populate :attr:`DeviceInfo.full_scale` via ``FPF`` per numeric field.

    Walks the data-frame format and issues ``FPF <stat>`` for every
    field that carries a numeric reading (design §5.20.2 — setpoint
    range validation consumes this cache). Best-effort per statistic:
    a timeout / rejection / parse error on one field leaves that slot
    out of the mapping but never blocks identification.

    GP firmware is skipped (``FPF`` not supported — design §16.6.8);
    5v12 V1_V7 devices reject ``FPF`` at runtime and fall through the
    per-field error handler.
    """
    if info.firmware.family is FirmwareFamily.GP:
        return info
    numeric_fields = tuple(f for f in fmt.fields if _is_numeric_field(f))
    if not numeric_fields:
        return info

    session = Session(client, unit_id=unit_id, info=info, port_label=None)
    full_scale: dict[Statistic, FullScaleValue] = dict(info.full_scale)
    for field in numeric_fields:
        assert field.statistic is not None  # noqa: S101 — _is_numeric_field gate
        if field.statistic in full_scale:
            continue
        value = await _probe_fpf_full_scale(session, field.statistic)
        if value is None:
            continue
        full_scale[field.statistic] = value

    if not full_scale:
        return info
    return dataclasses.replace(info, full_scale=MappingProxyType(full_scale))


async def _probe_fpf_full_scale(
    session: Session,
    statistic: Statistic,
) -> FullScaleValue | None:
    """Issue ``FPF <stat>`` and return the :class:`FullScaleValue`, or ``None``.

    Filters the ``A <zero> <code> ---`` "absent-statistic" reply
    (design §16.6.3) so a meaningless slot is treated the same as an
    outright rejection. The facade is happy to leave that statistic
    out of the full-scale cache.
    """
    try:
        result = await session.execute(
            FULL_SCALE_QUERY,
            FullScaleQueryRequest(statistic=statistic),
        )
    except (
        AlicatCommandRejectedError,
        AlicatTimeoutError,
        AlicatParseError,
        AlicatError,
    ):
        return None
    if result.value <= 0 or result.unit_label == "---":
        return None
    # FPF's decoder leaves ``statistic=Statistic.NONE`` (the device doesn't
    # echo it); fill in the statistic here so callers can trust the
    # ``FullScaleValue.statistic`` identity.
    return dataclasses.replace(result, statistic=statistic)


async def _prefetch_loop_control_variable(
    session: Session,
    info: DeviceInfo,
) -> None:
    """Pre-cache the session's loop-control variable for controllers.

    The ``setpoint`` facade uses this cache to pick the right
    :class:`FullScaleValue` for pre-I/O range validation. Best-effort:
    meter kinds have no ``LV`` command and firmware gates (V1_V7, pre-9v00
    V8_V9) raise :class:`AlicatFirmwareError` — either leaves the cache
    ``None`` and setpoint simply skips the range check, rather than
    failing the open.
    """
    if info.kind not in {
        DeviceKind.FLOW_CONTROLLER,
        DeviceKind.PRESSURE_CONTROLLER,
    }:
        return
    try:
        result = await session.execute(
            LOOP_CONTROL_VARIABLE,
            LoopControlVariableRequest(),
        )
    except (
        AlicatCommandRejectedError,
        AlicatTimeoutError,
        AlicatParseError,
        AlicatError,
    ):
        return
    session.update_loop_control_variable(result.variable)


def _resolve_port_label(
    port: str | Transport | AlicatProtocolClient,
    transport: Transport | None,
) -> str | None:
    """Best-effort human-readable port label for error context."""
    if isinstance(port, str):
        return port
    if transport is not None:
        try:
            return transport.label
        except AlicatError:
            return None
    return None
