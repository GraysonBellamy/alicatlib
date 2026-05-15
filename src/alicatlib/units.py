"""``to_pint`` — map Alicat :class:`Unit` to pint-compatible unit strings.

Cross-lib spec §K: every sibling library ships a ``to_pint`` helper so
downstream consumers (capa, dashboards) can hand off engineering units
to :mod:`pint` without each consumer maintaining its own mapping table.
``pint`` is **not** added as a runtime dependency — :func:`to_pint`
returns plain strings.

**Lossy by design.** PSIA / PSIG / PSID all collapse to ``"psi"`` —
``pint`` has no built-in gauge/absolute distinction, and consumers that
need it have larger problems than this helper can solve. Don't add an
``is_gauge`` argument; if a future caller genuinely needs the
distinction, expose it via a separate helper.

The table is exhaustive over every :class:`~alicatlib.registry.Unit`
member as of the current ``codes.json`` snapshot. New members landing
in the generator must be added here too; CI's import-symmetry test
verifies the helper is callable, and a dedicated unit test asserts
every :class:`Unit` member round-trips.
"""

from __future__ import annotations

from alicatlib.registry import Unit

__all__ = ["to_pint"]


_ALICAT_UNIT_TO_PINT: dict[Unit, str | None] = {
    # ----- sentinels / non-physical
    Unit.DEFAULT: None,
    Unit.UNKNOWN: None,
    Unit.COUNT: None,
    Unit.PERCENT: "percent",
    Unit.V: "volt",
    # ----- standard volumetric flow (Primer §B-1)
    Unit.SUL_M: "uL/min",  # standard µL/min — lossy: pint has no std/normal
    Unit.SML_S: "mL/s",
    Unit.SML_M: "mL/min",
    Unit.SML_H: "mL/hour",
    Unit.SL_S: "L/s",
    Unit.SLPM: "L/min",
    Unit.SL_H: "L/hour",
    Unit.SCCS: "cm**3/s",
    Unit.SCCM: "cm**3/min",
    Unit.SCM3_H: "cm**3/hour",
    Unit.SM3_M: "m**3/min",
    Unit.SM3_H: "m**3/hour",
    Unit.SM3_D: "m**3/day",
    Unit.SIN3_M: "in**3/min",
    Unit.SCFM: "ft**3/min",
    Unit.SCFH: "ft**3/hour",
    Unit.KSCFM: "kft**3/min",
    Unit.SCFD: "ft**3/day",
    # ----- normal volumetric flow (Primer §B-1)
    Unit.NUL_M: "uL/min",
    Unit.NML_S: "mL/s",
    Unit.NML_M: "mL/min",
    Unit.NML_H: "mL/hour",
    Unit.NL_S: "L/s",
    Unit.NLPM: "L/min",
    Unit.NL_H: "L/hour",
    Unit.NCCS: "cm**3/s",
    Unit.NCCM: "cm**3/min",
    Unit.NCM3_H: "cm**3/hour",
    Unit.NM3_M: "m**3/min",
    Unit.NM3_H: "m**3/hour",
    Unit.NM3_D: "m**3/day",
    # ----- true mass flow (Primer §B-2)
    Unit.MG_S: "mg/s",
    Unit.MG_M: "mg/min",
    Unit.G_S: "g/s",
    Unit.G_M: "g/min",
    Unit.G_H: "g/hour",
    Unit.KG_M: "kg/min",
    Unit.KG_H: "kg/hour",
    Unit.OZ_S: "oz/s",
    Unit.OZ_M: "oz/min",
    Unit.LB_M: "lb/min",
    Unit.LB_H: "lb/hour",
    # ----- totalised standard / normal volume (Primer §B-3)
    Unit.SUL: "uL",
    Unit.SML: "mL",
    Unit.SL: "L",
    Unit.SCM3: "cm**3",
    Unit.SM3: "m**3",
    Unit.SIN3: "in**3",
    Unit.SFT3: "ft**3",
    Unit.KSFT3: "kft**3",
    Unit.NUL: "uL",
    Unit.NML: "mL",
    Unit.NL: "L",
    Unit.NCM3: "cm**3",
    Unit.NM3: "m**3",
    # ----- volumetric flow (Primer §B-4)
    Unit.UL_M: "uL/min",
    Unit.ML_S: "mL/s",
    Unit.ML_M: "mL/min",
    Unit.ML_H: "mL/hour",
    Unit.L_S: "L/s",
    Unit.LPM: "L/min",
    Unit.L_H: "L/hour",
    Unit.US_GPM: "gallon/min",
    Unit.US_GPH: "gallon/hour",
    Unit.CCS: "cm**3/s",
    Unit.CCM: "cm**3/min",
    Unit.CM3_H: "cm**3/hour",
    Unit.M3_M: "m**3/min",
    Unit.M3_H: "m**3/hour",
    Unit.M3_D: "m**3/day",
    Unit.IN3_M: "in**3/min",
    Unit.CFM: "ft**3/min",
    Unit.CFH: "ft**3/hour",
    Unit.CFD: "ft**3/day",
    # ----- totalised volume (Primer §B-5)
    Unit.UL: "uL",
    Unit.ML: "mL",
    Unit.L: "L",
    Unit.US_GAL: "gallon",
    Unit.CM3: "cm**3",
    Unit.M3: "m**3",
    Unit.IN3: "in**3",
    Unit.FT3: "ft**3",
    # ----- pressure (Primer §B-6)
    Unit.UP: "micropoise",
    Unit.PA: "Pa",
    Unit.HPA: "hPa",
    Unit.KPA: "kPa",
    Unit.MPA: "MPa",
    Unit.MBAR: "mbar",
    Unit.BAR: "bar",
    Unit.G_CM2: "g_force/cm**2",
    Unit.KG_CM2: "kg_force/cm**2",
    Unit.PSI: "psi",  # lossy: PSIA/PSIG/PSID all map to psi (see §K)
    Unit.PSF: "lbf/ft**2",
    Unit.MTORR: "millitorr",
    Unit.TORR: "torr",
    Unit.MMHG: "mmHg",
    Unit.INHG: "inHg",
    Unit.MMH2O: "mmH2O",
    Unit.MMH2O_60F: "mmH2O",  # 60 °F reference collapses to plain mmH2O
    Unit.CMH2O: "cmH2O",
    Unit.CMH2O_60F: "cmH2O",
    Unit.INH2O: "inH2O",
    Unit.INH2O_60F: "inH2O",
    Unit.ATM: "atm",
    # ----- temperature (Primer §B-7)
    Unit.DEG_C: "degC",
    Unit.DEG_F: "degF",
    Unit.DEG_K: "kelvin",
    Unit.DEG_RA: "degR",
    # ----- time interval (Primer §B-8)
    Unit.H_M_S: None,  # composite display format; no pint equivalent
    Unit.MS: "ms",
    Unit.S: "s",
    Unit.M: "min",
    Unit.HOUR: "hour",
    Unit.DAY: "day",
}


# Sanity guard: a member added to the codes generator that's missing here
# would silently return ``None`` and look like "no pint mapping". Fail
# loudly at import time so the gap shows up in CI rather than in the
# field. Cheap once-per-process check.
_missing = set(Unit) - set(_ALICAT_UNIT_TO_PINT)
if _missing:  # pragma: no cover — defensive guard
    raise RuntimeError(
        f"alicatlib.units.to_pint: missing pint mapping for {sorted(u.name for u in _missing)} "
        "— update _ALICAT_UNIT_TO_PINT to cover the new Unit member(s).",
    )
del _missing


def to_pint(unit: Unit | str | None) -> str | None:
    """Return a pint-compatible unit string, or ``None`` if unmapped.

    Accepts a :class:`Unit` member, the raw Alicat label string
    (case-insensitive fallback), or ``None``. PSIA / PSIG / PSID
    collapse to ``"psi"`` — lossy by design (spec §K).
    """
    if unit is None:
        return None
    if isinstance(unit, Unit):
        return _ALICAT_UNIT_TO_PINT.get(unit)
    stripped = unit.strip()
    try:
        return _ALICAT_UNIT_TO_PINT[Unit(stripped)]
    except (ValueError, KeyError):
        pass
    target = stripped.casefold()
    for member, pint_str in _ALICAT_UNIT_TO_PINT.items():
        if member.value.casefold() == target:
            return pint_str
    return None
