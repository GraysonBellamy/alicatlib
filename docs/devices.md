# Devices

Every Alicat device routes through one of five facades based on its
model prefix and firmware:
[`Device`](../src/alicatlib/devices/base.py), [`FlowMeter`](../src/alicatlib/devices/flow_meter.py),
[`FlowController`](../src/alicatlib/devices/flow_controller.py),
[`PressureMeter`](../src/alicatlib/devices/pressure_meter.py), and
[`PressureController`](../src/alicatlib/devices/pressure_controller.py).
The factory picks the right one from the `MODEL_RULES` table in
[devices/factory.py](../src/alicatlib/devices/factory.py); callers never
instantiate facades directly.

See [Design](design.md) §5.9 for the class tree and §5.9a for the
orthogonal `Medium` model.

## DeviceKind and Medium

`DeviceKind` ([devices/kind.py](../src/alicatlib/devices/kind.py)) is a
`StrEnum` with five values — `FLOW_METER`, `FLOW_CONTROLLER`,
`PRESSURE_METER`, `PRESSURE_CONTROLLER`, and `UNKNOWN`. Every command
declares a `device_kinds: frozenset[DeviceKind]`; the session rejects
commands whose kind set excludes the identified kind.

`Medium` ([devices/medium.py](../src/alicatlib/devices/medium.py)) is a
`Flag` — `GAS`, `LIQUID`, or both (`GAS | LIQUID`). Modelling medium as
a flag rather than an enum lets the `K-` family CODA devices and the
`PCDS-`/`PCRDS-`/`PCRD3S-` stainless dual-valve controllers carry both
bits, while single-medium devices like `M-` (gas) and `LC-` (liquid)
carry one.

Kind × Medium is orthogonal: a flow controller can be gas, liquid, or
both; a CODA Coriolis meter covers both with a single facade. Per-command
gating is a bitwise intersection of the command's `media` against the
device's configured `media` — pre-I/O, from
[devices/session.py](../src/alicatlib/devices/session.py).

## Model prefix matrix

Prefix matching is case-sensitive (Alicat models are uppercase) and
most-specific first (`MCDW-` before `MCW-` before `MC-`). The full
table lives at [devices/factory.py:179](../src/alicatlib/devices/factory.py#L179);
this summary covers the headline prefixes.

### Gas mass-flow

| Prefix | Kind | Medium | Notes |
| --- | --- | --- | --- |
| `M-` / `MB-` / `MS-` / `MW-` / `MQ-` / `MBS-` / `MWB-` | `FLOW_METER` | `GAS` | Thermal MFM |
| `MC-` / `MCD-` / `MCH-` / `MCP-` / `MCR-` / `MCS-` / `MCT-` / `MCV-` / `MCW-` / `MCQ-` / `MCE-` / `MCDW-` / `MCRD-` / `MCRW-` / `MCRH-` / `MCRWD-` | `FLOW_CONTROLLER` | `GAS` | Thermal MFC; secondary letters compose per the 2018 Part Number Guide |
| `SFF-` | `FLOW_CONTROLLER` | `GAS` | Stream-switching / full-flow |
| `B-` / `BC-` | `FLOW_METER` / `FLOW_CONTROLLER` | `GAS` | BASIS OEM gas line; gas-only by part-number decoder |

### Gas pressure

| Prefix | Kind | Medium | Notes |
| --- | --- | --- | --- |
| `P-` / `PB-` / `PS-` / `EP-` | `PRESSURE_METER` | `GAS` | Meter-side, no valve compound letters |
| `PC-` / `PC3-` / `PCH-` / `PCP-` / `PCR-` / `PCS-` / `PCAS-` / `PCR3-` / `EPC-` / `IVC-` | `PRESSURE_CONTROLLER` | `GAS` | Single-valve flowing-process |
| `PCD-` / `PCD3-` / `PCPD-` / `PCRD-` / `PCRD3-` / `EPCD-` | `PRESSURE_CONTROLLER` | `GAS` | Dual-valve closed-volume |
| `PCDS-` / `PCRDS-` / `PCRD3S-` | `PRESSURE_CONTROLLER` | `GAS \| LIQUID` | Dual-valve stainless — gas and liquid per PCD-Series spec Rev 11 |

### Liquid

| Prefix | Kind | Medium | Notes |
| --- | --- | --- | --- |
| `L-` / `LB-` | `FLOW_METER` | `LIQUID` | Laminar DP |
| `LC-` / `LCR-` | `FLOW_CONTROLLER` | `LIQUID` | Laminar DP |

### CODA (K-family Coriolis)

| Prefix | Kind | Medium | Notes |
| --- | --- | --- | --- |
| `K-` / `KM-` | `FLOW_METER` | `GAS \| LIQUID` | Meter; legacy `KM-` preserved for fielded units |
| `KC-` | `FLOW_CONTROLLER` | `GAS \| LIQUID` | Valve-based controller |
| `KF-` / `KG-` | `FLOW_CONTROLLER` | `GAS \| LIQUID` | Pump-based controller (`KG-` is the pump-system variant) |

The CODA part-number decoder does not carry a medium field; the library
defaults to the widest media. Users whose unit is configured for a
single medium should narrow via `assume_media=` on
[`open_device`](../src/alicatlib/devices/factory.py#L988) (see below).

## Firmware families

`FirmwareVersion` ([firmware.py](../src/alicatlib/firmware.py)) sorts
into four families — `GP`, `V1_V7`, `V8_V9`, `V10` — parsed from the
`VE` reply. Cross-family ordering raises `TypeError`; the session gate
catches that and surfaces `AlicatFirmwareError` with
`reason="family_not_supported"`. Silent cross-family comparison would
be a worse failure mode than a typed crash.

| Family | Major range | Prefix behaviour | Notes |
| --- | --- | --- | --- |
| `GP` | n/a | Requires `$$` prefix on every command | Oldest lineage (`GP07R100`-era); no `Nv` number. Has its own `??M*` dialect (M0–M8 labels with `\x08` padding) — detection is by header, not by family |
| `V1_V7` | 1–7 | Bare prefix | No `LSS` / no `LS` modern setpoint — use the caveat in [safety.md](safety.md) |
| `V8_V9` | 8–9 | Bare prefix | `LS` / `LSS` land at firmware `9v00` within the family; use firmware gates on the command spec |
| `V10` | 10+ | Bare prefix | Current lineage. `NCS` streaming-rate config at `10v05`+ |

Every command spec declares either a firmware-version range
(`min_firmware` / `max_firmware`) or a family set
(`firmware_families`), or both. Mixing the two is fine — the
version-range check only runs within a matching family because
cross-family comparison is forbidden.

## Identification pipeline

[`open_device(port, ...)`](../src/alicatlib/devices/factory.py#L988) is
the public entry point. It runs the staged identification flow from
design §5.9:

1. **Stream recovery.** Optional (`recover_from_stream=True` by default).
   Passively reads the transport for ~100 ms; if bytes arrive the
   device was left streaming by a prior process, so the factory writes
   `@@ {unit_id}\r` and drains before identification begins. The
   passive sniff is capped at 256 bytes to avoid deadlocking against a
   device continuously streaming at its 50 ms default rate.
2. **`VE`.** Firmware version. Works on every family and anchors
   identification.
3. **`??M*`.** Manufacturing-info table (numeric families `>= 8v28`
   only). Parsed into `ManufacturingInfo`; the factory applies a best-guess
   `M<NN>` → named-field mapping to synthesise `DeviceInfo`. GP uses its
   own dialect (M0–M8 labels with `\x08` padding); detection is by
   header.
4. **Fallback identification.** For GP and pre-8v28 devices the caller
   must supply `model_hint="MC-100SCCM-D"` (or similar). Reaching this
   branch without a hint raises `AlicatConfigurationError`.
5. **Capability probing.** `FPF` probes `BAROMETER` and
   `SECONDARY_PRESSURE`; other bits currently fail-closed. Probe
   outcomes are retained on `DeviceInfo.probe_report` for diagnostics;
   gating reads only the flag set. Fail-closed means a hardware-missing
   command raises `AlicatMissingHardwareError` pre-I/O rather than
   silently hitting the device.
6. **`??D*`.** Data-frame format cached on the `Session`. `DCU` and
   `FPF` sweeps bind units and full-scale ranges per numeric field
   (see [Data frames](data-frames.md)).
7. **Model-rule dispatch.** Factory picks the correct facade subclass.

## Escape hatches

Three kwargs on [`open_device`](../src/alicatlib/devices/factory.py#L988)
let callers override identification when the device's self-report isn't
enough:

### `model_hint="..."`

Supplies the model string directly when `??M*` isn't available (GP
devices, pre-8v28 numeric firmware). The factory still runs `VE` and
the capability probes, but dispatches on the hinted model instead of a
parsed `??M*`.

### `assume_media=Medium.GAS | Medium.LIQUID`

**Replaces** the prefix-derived media bits. Intentional: "union with"
would make it impossible to narrow a widest-default CODA device to a
single configured medium. Use when:

- You have a `K-*` or `PCDS-*` device configured for one specific
  medium and want gas-only / liquid-only commands to fast-fail the
  other medium pre-I/O.
- Your prefix isn't in `MODEL_RULES` and you've supplied `model_hint`
  without media context.

### `assume_capabilities=Capability.TAREABLE_ABSOLUTE_PRESSURE`

**Union** with probed capabilities. The factory never *subtracts*
flags — silently masking hardware the device reports as present is
exactly the failure mode capability probing exists to avoid. Use for
capabilities where no safe probe exists (notably
`TAREABLE_ABSOLUTE_PRESSURE`, where probing would tare the device).

Hardware day 2026-04-17 established that `BAROMETER` does **not**
imply `TAREABLE_ABSOLUTE_PRESSURE` — four flow controllers probed
`BAROMETER` positive via `FPF 15` yet rejected or silently ignored
`PC` (`tare_absolute_pressure`). See design §16.6.7 for the narrative.

## Unknown devices

A prefix that doesn't match any `MODEL_RULES` entry falls through to
`DeviceKind.UNKNOWN` + `Medium.NONE`. The factory still returns a
generic `Device` facade — `poll()` and `execute()` work — but
kind-gated commands reject, and media-gated commands fail pre-I/O
because an empty `Medium` intersects nothing. This is the "loud
silence" path: better to tell users "unknown model, supply
`model_hint`" than to silently classify a new MFC as a pressure
controller.

## Capability probes

`Capability` ([commands/base.py:70](../src/alicatlib/commands/base.py#L70))
is a `Flag`: `BAROMETER`, `TAREABLE_ABSOLUTE_PRESSURE`,
`SECONDARY_PRESSURE`, `ANALOG_INPUT`, `ANALOG_OUTPUT`,
`SECONDARY_ANALOG_OUTPUT`, `REMOTE_TARE_PIN`, `MULTI_VALVE`,
`THIRD_VALVE`, `BIDIRECTIONAL`, `TOTALIZER`, `DISPLAY`.

Every command declares `required_capabilities`; the session gate
checks the device's flag set pre-I/O and raises
`AlicatMissingHardwareError` with the missing-flag name if the device
lacks the requirement. The error type is distinct from
`AlicatCommandRejectedError` (the `?` marker) so callers can
disambiguate "device doesn't have this hardware" from "device has it
but rejected the request".

The full probe report (per-flag `"present"` / `"absent"` / `"timeout"`
/ `"rejected"` / `"parse_error"`) is available on
`DeviceInfo.probe_report`; the gate uses only the flag set. A timeout
and a genuine absent look identical at the gate but different in the
report, which is the diagnostic signal.
