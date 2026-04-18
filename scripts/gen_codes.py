"""Generate ``src/alicatlib/registry/_codes_gen.py`` from ``codes.json``.

Run locally whenever ``codes.json`` changes. CI re-runs with ``--check`` and
fails if the committed output is stale. See design doc §5.3.

Usage:
    python scripts/gen_codes.py            # regenerate (writes file)
    python scripts/gen_codes.py --check    # exit 1 if output is stale
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CODES_JSON = REPO_ROOT / "src" / "alicatlib" / "registry" / "data" / "codes.json"
OUTPUT = REPO_ROOT / "src" / "alicatlib" / "registry" / "_codes_gen.py"

# Character substitutions run BEFORE non-identifier stripping, so that tokens
# like ``μ`` and ``°`` become readable ASCII pieces (``u``, ``_deg_``) instead
# of being collapsed into underscores.
_CHAR_MAP = str.maketrans(
    {
        "μ": "u",
        "°": "_deg_",
        "²": "2",
        "³": "3",
        "₀": "0",
        "₁": "1",
        "₂": "2",
        "₃": "3",
        "₄": "4",
        "₅": "5",
        "₆": "6",
        "₇": "7",
        "₈": "8",
        "₉": "9",
    }
)

_IDENT_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]+")


def default_member(value: str) -> str:
    """Derive a Python enum member name from a registry value."""
    translated = value.translate(_CHAR_MAP)
    sanitized = _IDENT_SANITIZE_RE.sub("_", translated).strip("_").upper()
    if not sanitized:
        raise ValueError(f"cannot derive member name from value {value!r}")
    if sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized


def member_for(entry: dict[str, Any]) -> str:
    """Explicit ``member`` field beats auto-derivation."""
    explicit = entry.get("member")
    if explicit:
        return str(explicit)
    return default_member(str(entry["value"]))


def validate(data: dict[str, Any]) -> None:
    """Fail fast on structural problems in codes.json.

    Checked invariants:

    - Gas/Statistic: no duplicate codes, no duplicate values, no duplicate members.
    - Units: no duplicate ``(category, code)``, no duplicate values, no duplicate members.
    - Aliases: no alias collides with any canonical value or another alias (per enum).
    """
    for kind in ("statistics", "gases"):
        _validate_flat(kind, data[kind])

    _validate_units(data["units"], {c["value"] for c in data["unit_categories"]})


def _validate_flat(kind: str, entries: list[dict[str, Any]]) -> None:
    codes: dict[int, str] = {}
    values: dict[str, int] = {}
    members: dict[str, str] = {}
    alias_to_owner: dict[str, str] = {}
    for e in entries:
        code = int(e["code"])
        value = str(e["value"])
        member = member_for(e)
        if code in codes:
            raise ValueError(f"{kind}: duplicate code {code} ({value!r} vs {codes[code]!r})")
        codes[code] = value
        if value in values:
            raise ValueError(f"{kind}: duplicate value {value!r}")
        values[value] = code
        if member in members:
            raise ValueError(
                f"{kind}: duplicate member {member} ({value!r} vs {members[member]!r})",
            )
        members[member] = value
    for e in entries:
        value = str(e["value"])
        for alias in e.get("aliases", []):
            alias_s = str(alias)
            if alias_s in values and values[alias_s] != int(e["code"]):
                raise ValueError(f"{kind}: alias {alias_s!r} collides with canonical value")
            if alias_s in alias_to_owner and alias_to_owner[alias_s] != value:
                raise ValueError(
                    f"{kind}: alias {alias_s!r} claimed by both "
                    f"{alias_to_owner[alias_s]!r} and {value!r}",
                )
            alias_to_owner[alias_s] = value


def _validate_units(entries: list[dict[str, Any]], allowed_categories: set[str]) -> None:
    values: dict[str, int] = {}
    members: dict[str, str] = {}
    by_cat_code: dict[tuple[str, int], str] = {}
    for e in entries:
        code = int(e["code"])
        value = str(e["value"])
        member = member_for(e)
        categories = list(e.get("categories", []))
        if not categories:
            raise ValueError(f"units: entry {value!r} has no categories")
        for cat in categories:
            if cat not in allowed_categories:
                raise ValueError(f"units: entry {value!r} has unknown category {cat!r}")
            key = (cat, code)
            if key in by_cat_code:
                raise ValueError(
                    f"units: duplicate (category={cat}, code={code}) "
                    f"for {value!r} vs {by_cat_code[key]!r}",
                )
            by_cat_code[key] = value
        if value in values:
            raise ValueError(f"units: duplicate value {value!r}")
        values[value] = code
        if member in members:
            raise ValueError(f"units: duplicate member {member} ({value!r} vs {members[member]!r})")
        members[member] = value


_HEADER = '''"""GENERATED FILE — do not edit by hand.

Regenerated by ``scripts/gen_codes.py`` from
``src/alicatlib/registry/data/codes.json``. CI enforces that this file matches
the committed ``codes.json``.

Public types are re-exported from :mod:`alicatlib.registry`; downstream code
should import from there, not from this module directly.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Mapping


'''


def build_output(data: dict[str, Any]) -> str:
    """Render the full generated module as a string."""
    stats = data["statistics"]
    gases = data["gases"]
    units = data["units"]
    unit_cats = data["unit_categories"]

    parts: list[str] = [_HEADER]

    # --- UnitCategory enum ----------------------------------------------------
    parts.append("class UnitCategory(StrEnum):\n")
    parts.append('    """Unit category, per Primer Appendix B sections."""\n\n')
    for cat in unit_cats:
        member = default_member(cat["value"])
        parts.append(f'    {member} = "{cat["value"]}"\n')
    parts.append("\n    @property\n")
    parts.append("    def display_name(self) -> str:\n")
    parts.append('        """Human-readable name for this category."""\n')
    parts.append("        return UNIT_CATEGORY_DISPLAY_NAMES[self]\n\n\n")

    # --- Statistic enum -------------------------------------------------------
    parts.append(_render_enum("Statistic", "Device statistic code (Primer Appendix A).", stats))

    # --- Gas enum -------------------------------------------------------------
    parts.append(_render_enum("Gas", "Gas / gas mixture (Primer Appendix C).", gases))

    # --- Unit enum (with .categories) -----------------------------------------
    parts.append(
        _render_enum(
            "Unit",
            "Engineering unit (Primer Appendix B).",
            units,
            extra_properties=[
                (
                    "categories",
                    "frozenset[UnitCategory]",
                    "Primer Appendix B categories this unit belongs to.",
                    "UNIT_CATEGORIES",
                ),
            ],
        )
    )

    # --- Backing maps ---------------------------------------------------------
    parts.append(_render_unit_category_display(unit_cats))
    parts.append(_render_code_map("Statistic", stats))
    parts.append(_render_display_map("Statistic", stats))
    parts.append(_render_by_code_map("Statistic", stats))
    parts.append(_render_alias_map("Statistic", stats))

    parts.append(_render_code_map("Gas", gases))
    parts.append(_render_display_map("Gas", gases))
    parts.append(_render_by_code_map("Gas", gases))
    parts.append(_render_alias_map("Gas", gases))

    parts.append(_render_code_map("Unit", units))
    parts.append(_render_display_map("Unit", units))
    parts.append(_render_unit_categories_map(units))
    parts.append(_render_unit_by_category_code_map(units))
    parts.append(_render_alias_map("Unit", units))

    # Normalise end-of-file to exactly one trailing newline so pre-commit's
    # end-of-file-fixer agrees with the generator and codegen-check doesn't
    # flip-flop between the two hooks.
    return "".join(parts).rstrip() + "\n"


def _render_enum(
    class_name: str,
    description: str,
    entries: list[dict[str, Any]],
    *,
    extra_properties: list[tuple[str, str, str, str]] | None = None,
) -> str:
    lines: list[str] = [f"class {class_name}(StrEnum):\n"]
    lines.append(f'    """{description}"""\n\n')
    for e in entries:
        member = member_for(e)
        value = str(e["value"])
        lines.append(f"    {member} = {value!r}\n")
    lines.append("\n    @property\n")
    lines.append("    def code(self) -> int:\n")
    lines.append('        """Numeric Alicat code (see Appendix A/B/C)."""\n')
    lines.append(f"        return {class_name.upper()}_CODES[self]\n\n")
    lines.append("    @property\n")
    lines.append("    def display_name(self) -> str:\n")
    lines.append('        """Human-readable name from the primer."""\n')
    lines.append(f"        return {class_name.upper()}_DISPLAY_NAMES[self]\n")
    for name, type_, doc, map_name in extra_properties or []:
        lines.append("\n    @property\n")
        lines.append(f"    def {name}(self) -> {type_}:\n")
        lines.append(f'        """{doc}"""\n')
        lines.append(f"        return {map_name}[self]\n")
    lines.append("\n\n")
    return "".join(lines)


def _render_unit_category_display(unit_cats: list[dict[str, Any]]) -> str:
    lines = [
        "UNIT_CATEGORY_DISPLAY_NAMES: Final[Mapping[UnitCategory, str]] = {\n",
    ]
    for cat in unit_cats:
        member = default_member(cat["value"])
        lines.append(f"    UnitCategory.{member}: {cat['display_name']!r},\n")
    lines.append("}\n\n\n")
    return "".join(lines)


def _render_code_map(class_name: str, entries: list[dict[str, Any]]) -> str:
    lines = [f"{class_name.upper()}_CODES: Final[Mapping[{class_name}, int]] = {{\n"]
    for e in entries:
        member = member_for(e)
        lines.append(f"    {class_name}.{member}: {int(e['code'])},\n")
    lines.append("}\n\n\n")
    return "".join(lines)


def _render_display_map(class_name: str, entries: list[dict[str, Any]]) -> str:
    lines = [f"{class_name.upper()}_DISPLAY_NAMES: Final[Mapping[{class_name}, str]] = {{\n"]
    for e in entries:
        member = member_for(e)
        lines.append(f"    {class_name}.{member}: {str(e['display_name'])!r},\n")
    lines.append("}\n\n\n")
    return "".join(lines)


def _render_by_code_map(class_name: str, entries: list[dict[str, Any]]) -> str:
    lines = [f"{class_name.upper()}_BY_CODE: Final[Mapping[int, {class_name}]] = {{\n"]
    for e in entries:
        member = member_for(e)
        lines.append(f"    {int(e['code'])}: {class_name}.{member},\n")
    lines.append("}\n\n\n")
    return "".join(lines)


def _render_alias_map(class_name: str, entries: list[dict[str, Any]]) -> str:
    lines = [f"{class_name.upper()}_ALIASES: Final[Mapping[str, {class_name}]] = {{\n"]
    for e in entries:
        member = member_for(e)
        lines.extend(
            f"    {str(alias)!r}: {class_name}.{member},\n" for alias in e.get("aliases", [])
        )
    lines.append("}\n\n\n")
    return "".join(lines)


def _render_unit_categories_map(entries: list[dict[str, Any]]) -> str:
    lines = ["UNIT_CATEGORIES: Final[Mapping[Unit, frozenset[UnitCategory]]] = {\n"]
    for e in entries:
        member = member_for(e)
        cats = list(e["categories"])
        inner = ", ".join(f"UnitCategory.{default_member(c)}" for c in cats)
        lines.append(f"    Unit.{member}: frozenset({{{inner}}}),\n")
    lines.append("}\n\n\n")
    return "".join(lines)


def _render_unit_by_category_code_map(entries: list[dict[str, Any]]) -> str:
    # Group by category for a stable, readable layout.
    by_cat: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for e in entries:
        member = member_for(e)
        code = int(e["code"])
        for cat in e["categories"]:
            by_cat[cat].append((code, member))

    lines = [
        "UNIT_BY_CATEGORY_CODE: Final[Mapping[tuple[UnitCategory, int], Unit]] = {\n",
    ]
    for cat_name, rows in by_cat.items():
        cat_member = default_member(cat_name)
        for code, member in rows:
            lines.append(
                f"    (UnitCategory.{cat_member}, {code}): Unit.{member},\n",
            )
    lines.append("}\n")
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 1 if the committed output is stale.",
    )
    args = parser.parse_args()

    if not CODES_JSON.exists():
        print(f"codes.json not found at {CODES_JSON}", file=sys.stderr)
        return 1

    raw = CODES_JSON.read_text(encoding="utf-8")
    data = json.loads(raw)
    validate(data)
    rendered = build_output(data)

    if args.check:
        existing = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if existing != rendered:
            diff = "".join(
                difflib.unified_diff(
                    existing.splitlines(keepends=True),
                    rendered.splitlines(keepends=True),
                    fromfile=str(OUTPUT.relative_to(REPO_ROOT)),
                    tofile="<regenerated>",
                )
            )
            sys.stderr.write(
                f"{OUTPUT.relative_to(REPO_ROOT)} is stale. "
                "Run `python scripts/gen_codes.py` to regenerate.\n",
            )
            sys.stderr.write(diff)
            return 1
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
