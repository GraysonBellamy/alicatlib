"""Cross-check ``tests/fixtures/device_matrix.yaml`` against command specs.

The matrix YAML is the empirical source of truth for observed command
availability across firmware families. This test keeps the command
catalog's ``firmware_families`` declarations honest:

- A command declared ``firmware_families={V10}`` must not have any
  non-V10 device in the matrix showing ``status: supported`` — that
  would be a coverage gap (our gate blocks a known-working family).
- A command whose matrix entries are uniformly ``silent`` / ``rejected``
  across every captured device in a family is flagged — either the
  matrix is incomplete or the spec should tighten its gate.

The validator tolerates non-monotonic commands (``gg`` works on 5v12
and 7v09 but rejects on 6v21; ``fpf`` rejects on 5v12 but works on
6v+). For those, we allow mixed results within a family.

When a new device capture lands, update the matrix first, then run
this test; a failure points at the exact (command, family, device)
triple that drifted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, cast

import pytest
import yaml  # type: ignore[import-untyped]  # types-PyYAML not in the type group

from alicatlib.commands import (
    ANALOG_OUTPUT_SOURCE,
    AUTO_TARE,
    AVERAGE_TIMING,
    BLINK_DISPLAY,
    CANCEL_VALVE_HOLD,
    DATA_FRAME_FORMAT_QUERY,
    DEADBAND_LIMIT,
    ENGINEERING_UNITS,
    FULL_SCALE_QUERY,
    GAS_LIST,
    GAS_SELECT,
    GAS_SELECT_LEGACY,
    HOLD_VALVES,
    HOLD_VALVES_CLOSED,
    LOCK_DISPLAY,
    LOOP_CONTROL_VARIABLE,
    MANUFACTURING_INFO,
    POWER_UP_TARE,
    RAMP_RATE,
    REQUEST_DATA,
    SETPOINT,
    SETPOINT_LEGACY,
    SETPOINT_SOURCE,
    STP_NTP_PRESSURE,
    STP_NTP_TEMPERATURE,
    STREAMING_RATE,
    TARE_ABSOLUTE_PRESSURE,
    TARE_FLOW,
    TARE_GAUGE_PRESSURE,
    TOTALIZER_CONFIG,
    TOTALIZER_SAVE,
    UNLOCK_DISPLAY,
    USER_DATA,
    VALVE_DRIVE,
    VE_QUERY,
    ZERO_BAND,
)
from alicatlib.commands.base import Command  # noqa: TC001 — runtime-referenced in parametrize
from alicatlib.commands.polling import POLL_DATA
from alicatlib.firmware import FirmwareFamily

MATRIX_PATH: Final[Path] = Path(__file__).parent.parent / "fixtures" / "device_matrix.yaml"


# Map from YAML matrix keys to the catalog Command spec they describe.
# Keep this in sync with the schema's `commands:` block — a missing
# entry on either side fails the matrix-key-coverage test below.
_MATRIX_KEY_TO_COMMAND: Final[dict[str, Command[Any, Any]]] = {
    "poll": POLL_DATA,
    "ve": VE_QUERY,
    "mm": MANUFACTURING_INFO,
    "dd": DATA_FRAME_FORMAT_QUERY,
    "gg": GAS_LIST,
    "gs": GAS_SELECT,
    "g_legacy_set": GAS_SELECT_LEGACY,
    "dcu": ENGINEERING_UNITS,
    "fpf": FULL_SCALE_QUERY,
    "ls": SETPOINT,
    "s": SETPOINT_LEGACY,
    "lss": SETPOINT_SOURCE,
    "lv": LOOP_CONTROL_VARIABLE,
    "t": TARE_FLOW,
    "tp": TARE_GAUGE_PRESSURE,
    "pc": TARE_ABSOLUTE_PRESSURE,
    # Additions from 2026-04-17 captures (§16.6.10):
    "dv": REQUEST_DATA,
    "ncs": STREAMING_RATE,
    "vd": VALVE_DRIVE,
    "hp": HOLD_VALVES,
    "hc": HOLD_VALVES_CLOSED,
    "c": CANCEL_VALVE_HOLD,
    "sr": RAMP_RATE,
    "lcdb": DEADBAND_LIMIT,
    "dcz": ZERO_BAND,
    "dca": AVERAGE_TIMING,
    "dcfrp": STP_NTP_PRESSURE,
    "dcfrt": STP_NTP_TEMPERATURE,
    "asocv": ANALOG_OUTPUT_SOURCE,
    "ffp": BLINK_DISPLAY,
    "l": LOCK_DISPLAY,
    "u": UNLOCK_DISPLAY,
    "ud": USER_DATA,
    "zca": AUTO_TARE,
    "zcp": POWER_UP_TARE,
    "tc": TOTALIZER_CONFIG,
    "tcr": TOTALIZER_SAVE,
}

# Matrix keys that record observed wire behavior but aren't (yet) in
# the public command catalog. Listed here so the coverage check
# doesn't flag them as typos.
_MATRIX_ONLY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "lca",  # loop-control algorithm (10v05+)
        "ncb",  # change baud rate — driven via Session.change_baud_rate, no Command spec
        "rename",  # <old>@ <new> — driven via Session.change_unit_id, no Command spec
        # streaming-mode state transition; not a single Command
        # (NCS is the rate, "stream" is the entire session)
        "stream",
    }
)

# Commands whose observed availability varies within a family — the
# "non-monotonic" bucket per the architecture review. For these, we
# allow mixed ``supported``/``rejected`` within the same family and
# don't require uniformity.
_NON_MONOTONIC_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "gg",  # works on 5v12/7v09/V8+/V10; rejects on 6v21
        "fpf",  # rejects on 5v12; works on 6v+
        "pc",  # hardware-dependent (BAROMETER-advertised doesn't imply PC)
        "t",
        "tp",  # tare — firmware-gated but per-device quirks possible
    }
)

# Statuses that count as "device actually runs this command".
_SUPPORTED_STATUSES: Final[frozenset[str]] = frozenset({"supported"})
# Statuses that explicitly mean "device doesn't implement this command".
_UNSUPPORTED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "silent",
        "rejected",
        "adc_counts",  # different command entirely; not the one we ship
    }
)


@pytest.fixture(scope="module")
def matrix() -> dict[str, Any]:
    """Load the YAML once per session — schema_version must be current."""
    raw = cast("dict[str, Any]", yaml.safe_load(MATRIX_PATH.read_text()))
    assert raw["schema_version"] == 1, (
        f"Unexpected schema_version {raw['schema_version']!r}; update "
        "this test in lockstep with any schema bump."
    )
    return raw


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------


def test_every_matrix_command_key_is_mapped(matrix: dict[str, Any]) -> None:
    """Every distinct ``commands:`` key across all devices is either a
    catalog-mapped command or an explicit ``_MATRIX_ONLY_KEYS`` entry.

    Catches typos in the YAML ("mm" vs "m_m") and drift from the command
    catalog (we drop a command → the matrix key becomes orphaned).
    """
    seen_keys: set[str] = set()
    for dev in matrix["devices"]:
        seen_keys.update(dev.get("commands", {}).keys())
    mapped = set(_MATRIX_KEY_TO_COMMAND.keys())
    accounted = mapped | _MATRIX_ONLY_KEYS
    orphans = seen_keys - accounted
    assert not orphans, (
        f"Matrix keys not mapped to a command or declared as matrix-only: "
        f"{sorted(orphans)}. Update _MATRIX_KEY_TO_COMMAND or _MATRIX_ONLY_KEYS."
    )


def test_every_device_has_a_family_and_provenance(matrix: dict[str, Any]) -> None:
    """Every device entry declares ``family`` + ``provenance.captured_at``
    so consumers can filter by family and trace captures back to their
    session without hunting in design.md."""
    valid_families = {f.name for f in FirmwareFamily}
    for i, dev in enumerate(matrix["devices"]):
        assert "family" in dev, f"device {i} ({dev.get('model')}) missing 'family'"
        assert dev["family"] in valid_families, (
            f"device {i}: family {dev['family']!r} not in {sorted(valid_families)}"
        )
        assert "captured_at" in dev, f"device {i} missing 'captured_at'"
        assert "provenance" in dev, f"device {i} missing 'provenance' block"


# ---------------------------------------------------------------------------
# Consistency — command firmware_families vs matrix
# ---------------------------------------------------------------------------


def _family_for(dev: dict[str, Any]) -> FirmwareFamily:
    return FirmwareFamily[dev["family"]]


def _devices_by_family(
    matrix: dict[str, Any],
) -> dict[FirmwareFamily, list[dict[str, Any]]]:
    buckets: dict[FirmwareFamily, list[dict[str, Any]]] = {f: [] for f in FirmwareFamily}
    for dev in matrix["devices"]:
        buckets[_family_for(dev)].append(dev)
    return buckets


@pytest.mark.parametrize(("matrix_key", "command"), sorted(_MATRIX_KEY_TO_COMMAND.items()))
def test_gate_does_not_exclude_a_supported_family(
    matrix: dict[str, Any],
    matrix_key: str,
    command: Command[Any, Any],
) -> None:
    """A command's ``firmware_families`` gate must not exclude a family
    that the matrix observes as supporting the command.

    Counter-example we'd catch: ``DCU`` declared ``firmware_families={V10}``,
    but a 6v21 capture shows ``dcu: supported``. That combination would
    mean the library blocks a working command — this test fires.
    """
    if not command.firmware_families:
        return  # empty gate = "any family" — nothing to check

    violations: list[str] = []
    for dev in matrix["devices"]:
        status = dev.get("commands", {}).get(matrix_key)
        if status not in _SUPPORTED_STATUSES:
            continue
        family = _family_for(dev)
        if family not in command.firmware_families:
            violations.append(
                f"{dev['model']} ({dev['firmware']}, family={family.name}) "
                f"shows '{status}' but {command.name}.firmware_families="
                f"{sorted(f.name for f in command.firmware_families)}"
            )
    assert not violations, (
        f"{command.name}: matrix says these devices support the command but "
        f"the spec's firmware_families gate excludes them:\n  " + "\n  ".join(violations)
    )


@pytest.mark.parametrize(("matrix_key", "command"), sorted(_MATRIX_KEY_TO_COMMAND.items()))
def test_gate_is_not_uselessly_permissive(
    matrix: dict[str, Any],
    matrix_key: str,
    command: Command[Any, Any],
) -> None:
    """If a command's ``firmware_families`` gate admits family F, and
    every captured device in F shows ``silent`` / ``rejected`` (with no
    ``untested`` covering the rest of the family), the gate is doing
    nothing — tighten the spec.

    Skipped for non-monotonic commands where per-device behavior varies
    within a family.
    """
    if matrix_key in _NON_MONOTONIC_COMMANDS:
        return
    if not command.firmware_families:
        return

    buckets = _devices_by_family(matrix)
    dead_families: list[str] = []
    for family in command.firmware_families:
        statuses = {dev.get("commands", {}).get(matrix_key, "untested") for dev in buckets[family]}
        if not statuses or "untested" in statuses:
            continue  # incomplete coverage — don't flag
        if statuses <= _UNSUPPORTED_STATUSES:
            dead_families.append(
                f"{family.name} (devices={[dev['firmware'] for dev in buckets[family]]}, "
                f"statuses={sorted(statuses)})"
            )
    assert not dead_families, (
        f"{command.name}.firmware_families admits these families even "
        "though every captured device in each rejects or silently "
        "ignores the command:\n  "
        + "\n  ".join(dead_families)
        + "\n  (Tighten the gate or remove it. If coverage is simply "
        "incomplete, add an 'untested' entry for at least one device "
        "in the family to skip this check.)"
    )


# ---------------------------------------------------------------------------
# Prefix policy consistency — GP writes vs reads
# ---------------------------------------------------------------------------


def test_gp_prefix_policy_matches_prefix_less_declarations(matrix: dict[str, Any]) -> None:
    """Every GP device in the matrix must agree on the reads-vs-writes
    prefix rule, and the ``prefix_less`` flag on each command must match.

    Design §16.6.8 established: GP reads go prefix-less, GP writes get
    ``$$``. That's now encoded via ``Command.prefix_less``. This test
    pins the matrix and code in sync.
    """
    gp_devices = [dev for dev in matrix["devices"] if _family_for(dev) is FirmwareFamily.GP]
    if not gp_devices:
        pytest.skip("no GP device captures — nothing to check")

    # All GP devices must agree on the policy.
    policies = {tuple(sorted(dev["prefix"].items())) for dev in gp_devices}
    assert len(policies) == 1, f"GP captures disagree on prefix policy: {policies!r}"
    policy = dict(gp_devices[0]["prefix"])
    assert policy["reads"] == "none", f"expected GP reads to be prefix-less, got {policy!r}"
    assert policy["writes"] == "dollar", f"expected GP writes to use $$, got {policy!r}"

    # Reads captured as ``supported`` on GP must map to ``prefix_less=True``
    # commands; writes captured as ``supported`` on GP must map to
    # ``prefix_less=False`` commands.
    read_keys = {"poll", "mm", "dd", "gg"}
    for dev in gp_devices:
        for key, status in dev.get("commands", {}).items():
            if status != "supported":
                continue
            command = _MATRIX_KEY_TO_COMMAND.get(key)
            if command is None:
                continue
            if key in read_keys:
                assert command.prefix_less, (
                    f"{command.name} is a GP-supported read ({key}) but "
                    f"prefix_less=False — the session would emit $$ and "
                    "the device would reject."
                )
            else:
                assert not command.prefix_less, (
                    f"{command.name} is a GP-supported write ({key}) but "
                    f"prefix_less=True — the session would omit $$ and "
                    "the device would go silent."
                )
