"""Shared response parsing helpers.

Covers the full parser surface per design §5.11: primitive decoders
(`parse_ascii`, `parse_fields`, `parse_int`, `parse_float`,
`parse_optional_float`, `parse_bool_code`, `parse_enum_code`), the
all-firmware `VE` decoder (`parse_ve_response`), the status-code helper
(`parse_status_codes`), and the table / frame parsers used by
identification and polling (`parse_manufacturing_info`,
`parse_data_frame_table`, `parse_data_frame`).

Every helper raises :class:`alicatlib.errors.AlicatParseError` with the raw
response preserved in :class:`alicatlib.errors.ErrorContext` so debugging a
bad reply never requires adding print statements.
"""

from __future__ import annotations

import contextlib
import re
from datetime import date
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from alicatlib.devices.data_frame import (
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
    ParsedFrame,
)
from alicatlib.devices.models import ManufacturingInfo, StatusCode
from alicatlib.errors import (
    AlicatParseError,
    AlicatUnitIdMismatchError,
    ErrorContext,
    UnknownGasError,
    UnknownStatisticError,
    UnknownUnitError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol.framing import decode_ascii
from alicatlib.registry.statistics import statistic_registry
from alicatlib.registry.units import unit_registry

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from alicatlib.registry._codes_gen import Gas, Statistic, Unit
    from alicatlib.registry.aliases import AliasRegistry

__all__ = [
    "parse_ascii",
    "parse_bool_code",
    "parse_data_frame",
    "parse_data_frame_table",
    "parse_enum_code",
    "parse_fields",
    "parse_float",
    "parse_gas_list",
    "parse_int",
    "parse_manufacturing_info",
    "parse_optional_float",
    "parse_status_codes",
    "parse_ve_response",
]

_DEFAULT_BOOL_MAPPING: Mapping[str, bool] = {"1": True, "0": False}
_ABSENT_TOKEN = "--"  # noqa: S105 — Alicat wire sentinel for "field unavailable", not a credential


#: Re-export of :func:`alicatlib.protocol.framing.decode_ascii` under the
#: parser-layer name users already know. The implementation lives in
#: :mod:`alicatlib.protocol.framing` to break a would-be cycle with
#: :mod:`alicatlib.devices.data_frame`.
parse_ascii = decode_ascii


def parse_fields(
    raw: str,
    *,
    command: str,
    expected_count: int | None = None,
) -> list[str]:
    """Split a whitespace-delimited response into fields.

    Args:
        raw: Response text (already ASCII-decoded).
        command: Command name, for the error message.
        expected_count: If given, enforce exactly this many fields.

    Returns:
        The list of non-empty fields.

    Raises:
        AlicatParseError: If ``expected_count`` is set and the actual count
            differs.
    """
    fields = raw.split()
    if expected_count is not None and len(fields) != expected_count:
        raise AlicatParseError(
            f"{command}: expected {expected_count} fields, got {len(fields)} — {raw!r}",
            field_name="fields",
            expected=expected_count,
            actual=len(fields),
            context=ErrorContext(command_name=command, raw_response=raw.encode("ascii", "replace")),
        )
    return fields


def parse_int(value: str, *, field: str) -> int:
    """Parse ``value`` as a base-10 integer, raising on failure."""
    try:
        return int(value)
    except ValueError as exc:
        raise AlicatParseError(
            f"could not parse integer from {value!r} (field={field})",
            field_name=field,
            expected="integer",
            actual=value,
        ) from exc


def parse_float(value: str, *, field: str) -> float:
    """Parse ``value`` as a float, raising on failure."""
    try:
        return float(value)
    except ValueError as exc:
        raise AlicatParseError(
            f"could not parse float from {value!r} (field={field})",
            field_name=field,
            expected="float",
            actual=value,
        ) from exc


def parse_optional_float(value: str, *, field: str) -> float | None:
    """Parse ``value`` as a float, returning ``None`` for the ``"--"`` sentinel.

    Alicat emits ``--`` in a data frame when a field is unavailable on the
    current device or in the current mode (e.g. setpoint on a flow meter).
    Callers that want a strict parse should use :func:`parse_float` directly.
    """
    if value == _ABSENT_TOKEN:
        return None
    return parse_float(value, field=field)


def parse_bool_code(
    value: str,
    *,
    field: str,
    mapping: Mapping[str, bool] = _DEFAULT_BOOL_MAPPING,
) -> bool:
    """Parse a boolean-coded field (default: ``"1"`` → True, ``"0"`` → False).

    Args:
        value: The wire-level field.
        field: Field name, for the error message.
        mapping: Accepted string-to-bool pairs. Override for commands that use
            non-standard codes (e.g. some commands use ``"Y"`` / ``"N"``).

    Raises:
        AlicatParseError: If ``value`` is not a key in ``mapping``.
    """
    try:
        return mapping[value]
    except KeyError as exc:
        accepted = ", ".join(repr(k) for k in mapping)
        raise AlicatParseError(
            f"could not parse bool from {value!r} (field={field}, accepted={accepted})",
            field_name=field,
            expected=tuple(mapping),
            actual=value,
        ) from exc


def parse_enum_code[E: "Gas | Statistic | Unit"](
    value: str,
    *,
    field: str,
    registry: AliasRegistry[E],
) -> E:
    """Parse a numeric code and resolve it against ``registry``.

    Wraps :meth:`alicatlib.registry.aliases.AliasRegistry.by_code` so that
    unknown codes coming from the device surface as :class:`AlicatParseError`
    (a protocol-layer problem) rather than :class:`UnknownGasError` /
    :class:`UnknownStatisticError` (config-layer problems from user input).
    The original registry error is preserved as ``__cause__``.

    Only usable with :class:`AliasRegistry` (unique-code enums: Gas, Statistic).
    Unit lookups require a :class:`UnitCategory` disambiguator and are handled
    at the data-frame-field parsing layer where the category is known.
    """
    code = parse_int(value, field=field)
    try:
        return registry.by_code(code)
    except (UnknownGasError, UnknownStatisticError, UnknownUnitError) as exc:
        raise AlicatParseError(
            f"unknown code {code} for {field}",
            field_name=field,
            expected="known registry code",
            actual=code,
        ) from exc


_ISO_DATE_RE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b")

# V8/V9 firmware (e.g. ``8v17.0-R23``) emits dates like
# ``Nov 27 2019,15:28:45`` rather than ISO. Captured during 8v17 hardware
# validation (design §16.1 #4) — extend rather than replace because newer
# firmware still emits ISO and other devices may yet emit other formats.
_MONTH_NAME_DATE_RE = re.compile(
    r"\b(?P<m>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<d>\d{1,2})\s+"
    r"(?P<y>\d{4})\b",
    re.IGNORECASE,
)
_MONTH_TO_NUM: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
# Captures the full firmware string including the ``.N-RNN`` revision
# suffix when present. Hardware validation on 2026-04-17 (design §16.6) confirmed
# the suffix appears uniformly across families (5v12.0-R22, 8v17.0-R23,
# 10v04.0-R24, 10v20.0-R24). :class:`FirmwareVersion.parse` keeps it on
# ``.raw`` for diagnostics; the narrower ``major`` / ``minor`` fields
# still drive gating logic.
_FIRMWARE_TOKEN_RE = re.compile(
    r"(?:\bGP\b|\d+\s*[vV.]\s*\d+(?:\.\d+(?:-R\d+)?)?)",
)


def parse_ve_response(raw: bytes) -> tuple[FirmwareVersion, date | None]:
    """Parse a ``VE`` (firmware version) response.

    ``VE`` is the one identification command that works on every firmware
    family — it is the anchor of the identification pipeline (design §5.9).
    The response shape varies across families: at minimum it contains a
    firmware token (``GP``, ``GP-10v05``, ``10v05``, ``1v00``, ...); some
    devices additionally report a firmware date in ISO ``YYYY-MM-DD`` form.

    This parser is deliberately tolerant: it scans the decoded response for
    (a) the first firmware-shaped token and (b) the first ISO-date token,
    regardless of their relative position or surrounding text.

    Returns:
        A ``(FirmwareVersion, date | None)`` pair. The date is ``None`` when
        the device's firmware does not include one.

    Raises:
        AlicatParseError: If no firmware token can be found. A malformed date
            (present but unparseable) also raises, rather than silently
            dropping the field — a garbled date is a sign of line corruption
            that the caller should see.
    """
    text = parse_ascii(raw)
    fw_match = _FIRMWARE_TOKEN_RE.search(text)
    if fw_match is None:
        raise AlicatParseError(
            f"VE: no firmware token in response {text!r}",
            field_name="firmware",
            expected="GP or <major>v<minor>",
            actual=text,
            context=ErrorContext(command_name="VE", raw_response=raw),
        )

    fw = FirmwareVersion.parse(fw_match.group(0))

    iso_match = _ISO_DATE_RE.search(text)
    if iso_match is not None:
        try:
            fw_date = date(
                int(iso_match.group("y")),
                int(iso_match.group("m")),
                int(iso_match.group("d")),
            )
        except ValueError as exc:
            raise AlicatParseError(
                f"VE: malformed date {iso_match.group(0)!r} in response {text!r}",
                field_name="firmware_date",
                expected="YYYY-MM-DD",
                actual=iso_match.group(0),
                context=ErrorContext(command_name="VE", raw_response=raw),
            ) from exc
        return fw, fw_date

    name_match = _MONTH_NAME_DATE_RE.search(text)
    if name_match is not None:
        try:
            fw_date = date(
                int(name_match.group("y")),
                _MONTH_TO_NUM[name_match.group("m").lower()],
                int(name_match.group("d")),
            )
        except ValueError as exc:
            raise AlicatParseError(
                f"VE: malformed date {name_match.group(0)!r} in response {text!r}",
                field_name="firmware_date",
                expected="<Mon> <DD> <YYYY>",
                actual=name_match.group(0),
                context=ErrorContext(command_name="VE", raw_response=raw),
            ) from exc
        return fw, fw_date

    return fw, None


_STATUS_VALUES: frozenset[str] = frozenset(code.value for code in StatusCode)


def parse_status_codes(tokens: Sequence[str]) -> frozenset[StatusCode]:
    """Collect :class:`StatusCode` members from a token sequence.

    Any token whose value is not a known status code is silently skipped —
    callers that want "status-only" semantics should pre-slice the tail of
    their token stream, since status codes are the trailing run of a data
    frame per primer convention.

    Order on the wire is not preserved (returned as a :class:`frozenset`);
    the primer does not specify a canonical ordering for multi-code runs.
    """
    return frozenset(StatusCode(t) for t in tokens if t in _STATUS_VALUES)


# Each line: "<unit_id> M<NN> <payload>". Payload may be empty on some
# firmware for codes the device doesn't populate; we keep the empty string
# to preserve the fact that the code was emitted.
_MFG_LINE_RE = re.compile(r"^(?P<uid>\S+)\s+M(?P<code>\d{1,2})(?:\s+(?P<payload>.*))?$")

# GP firmware wraps ``??M*`` / ``??D*`` payload values in ``\x08`` (backspace)
# control characters used by the device's LCD display loop. They aren't part
# of the data; strip them at the parser layer so downstream consumers see a
# clean value. See design §16.6.8 for the capture showing the pattern.
_GP_BACKSPACE: Final[str] = "\x08"


def _strip_gp_padding(text: str) -> str:
    """Remove GP firmware's backspace padding from a payload string."""
    return text.replace(_GP_BACKSPACE, "").strip()


def parse_manufacturing_info(lines: Sequence[bytes]) -> ManufacturingInfo:
    """Parse a ``??M*`` response into :class:`ManufacturingInfo`.

    Expected shape (per primer — verified against hardware fixtures as
    they're captured): a series of ``<unit_id> M<NN> <payload>`` lines,
    one per code. The parser does not pin a line count (firmware versions
    have varied) but does enforce:

    - A consistent ``unit_id`` across every line — a mismatch is an
      :class:`AlicatUnitIdMismatchError` (a sign the bus has bled frames
      from another device).
    - No duplicate ``M<NN>`` codes within the response — a duplicate is
      an :class:`AlicatParseError` rather than a silent overwrite.
    - Every non-empty line matches the ``<uid> M<NN> <payload>`` shape;
      a malformed line raises rather than being skipped, so firmware-side
      format drift surfaces instead of being swallowed.

    The *semantic* mapping (``M04`` → model, ``M05`` → serial, etc.) is
    intentionally not applied here — it belongs in the factory
    (:mod:`alicatlib.devices.factory`) where it can be firmware-version
    aware and validated against real captures.
    """
    if not lines:
        raise AlicatParseError(
            "??M*: empty response",
            field_name="manufacturing_info",
            expected=">=1 line",
            actual=0,
            context=ErrorContext(command_name="??M*"),
        )

    unit_id: str | None = None
    by_code: dict[int, str] = {}

    for raw_line in lines:
        # Strip GP backspace-padding before regex match — see _strip_gp_padding.
        text = _strip_gp_padding(parse_ascii(raw_line).rstrip("\r\n"))
        if not text:
            continue
        m = _MFG_LINE_RE.match(text)
        if m is None:
            raise AlicatParseError(
                f"??M*: malformed line {text!r}",
                field_name="manufacturing_info_line",
                expected="<unit_id> M<NN> <payload>",
                actual=text,
                context=ErrorContext(command_name="??M*", raw_response=raw_line),
            )

        line_uid = m.group("uid")
        code = int(m.group("code"))
        payload = (m.group("payload") or "").strip()

        if unit_id is None:
            unit_id = line_uid
        elif unit_id != line_uid:
            raise AlicatUnitIdMismatchError(
                f"??M*: unit_id {line_uid!r} on M{code:02d} line does not match {unit_id!r}",
                context=ErrorContext(
                    command_name="??M*",
                    unit_id=unit_id,
                    raw_response=raw_line,
                ),
            )

        if code in by_code:
            raise AlicatParseError(
                f"??M*: duplicate M{code:02d} line — {text!r}",
                field_name="manufacturing_info_code",
                expected="unique M-codes",
                actual=code,
                context=ErrorContext(command_name="??M*", raw_response=raw_line),
            )

        by_code[code] = payload

    if unit_id is None:
        # All lines were blank — treat as empty response.
        raise AlicatParseError(
            "??M*: response contained only blank lines",
            field_name="manufacturing_info",
            expected=">=1 non-blank line",
            actual=0,
            context=ErrorContext(command_name="??M*"),
        )

    return ManufacturingInfo(unit_id=unit_id, by_code=MappingProxyType(by_code))


# Canonical ??D* dialect captured on real hardware (8v17 + V10 10v20.0-R24
# both emit the same shape — see design §16.6).
#
# Each line: `<uid> D<NN> <stat_code> <name with internal spaces> <type> <width> [<notes...>]`
#
# - The leading line `<uid> D00 ID_ NAME...... TYPE...... WIDTH NOTES.....`
#   (column placeholders padded with underscores) is a header and is
#   skipped — recognised by `ID_` in the stat-code position.
# - Field rows carry a 3-digit (sometimes 2-digit) statistic code right after
#   `D<NN>`; we record it but prefer the registry lookup by canonical name.
# - The name column is right-padded with spaces and may contain internal
#   spaces (`Mass Flow Setpt`); we recover it by joining tokens until we
#   hit a known type marker.
# - The type column is `string` (alone) or `s decimal` (signed) or `decimal`
#   (unsigned). Width follows (`<n>` or `<n>/<n>`).
# - The NOTES column carries either:
#     - For decimal fields: `<unit_code> <decimal_precision> <unit_label>`
#       (3 tokens; unit_code is 3-digit zero-padded, label like `PSIA`)
#     - For string fields: typically blank (Unit ID, Gas)
#     - For conditional flag rows (`*Error`, `*Status`): a single
#       per-flag mnemonic like `OPL`, `ADC`, `LCK`.

# Type tokens that mark the end of the (possibly multi-word) name column.
# A `s` token immediately followed by one of these marks a signed variant.
_DF_TYPE_TOKENS: frozenset[str] = frozenset(
    {"string", "decimal", "integer", "int", "uint", "float", "double"},
)

# V1_V7 dialect type tokens — observed on the 5v12 capture. `signed` is the
# numeric type marker; `char` is single-character; `string` is multi-char.
_DF_TYPE_TOKENS_V1_V7: frozenset[str] = frozenset({"string", "char", "signed", "unsigned"})

# Header-line markers. The V8+ header has `ID_` as the third token (i.e.
# in the stat-code position); the V1_V7 header has `NAME_______` as the
# second token (after `<uid>  D00`). Checking these markers avoids the
# false-positive risk of pinning on D-code values which may differ across
# firmware versions.
_DF_HEADER_MARKER: str = "ID_"  # V8+ dialect
_DF_V1_V7_HEADER_MARKER_PREFIX: str = "NAME"  # V1_V7 dialect

# V1_V7 conditional field names — the dialect lacks the `*<name>` marker
# the V8+ format uses, so we recognise these by name. `Status` rows
# typically appear once per status flag (with the per-flag mnemonic in
# the MaxVal column); `Error` appears once with the per-error mnemonic.
_DF_V1_V7_CONDITIONAL_NAMES: frozenset[str] = frozenset({"Error", "Status"})

# V1_V7 "no unit" sentinel in the UNITS column.
_DF_V1_V7_UNIT_NA: str = "na"

# Minimum token counts for the various ??D* row shapes. Pulled out of the
# parser bodies so the magic-number lint doesn't have to noqa each.
_DF_HEADER_MIN_TOKENS_FOR_SNIFF: int = 3  # uid + Dnn + first column header
_DF_CODE_MIN_LEN: int = 2  # 'D' + at least one digit
_DF_DEFAULT_ROW_MIN_TOKENS: int = 5  # uid + Dnn + stat_code + name + type
_DF_V1_V7_ROW_MIN_TOKENS: int = 7  # uid + Dnn + name + type + min + max + units


def _df_parser_for_type(type_name: str) -> Callable[[str], float | str | None]:
    """Pick the per-field parser callable for a ``??D*``-declared type.

    The type name is the joined value as observed on the wire — `string`,
    `decimal`, `s decimal` (signed), `integer`, etc. The leading `s` is a
    sign marker; both signed and unsigned integers / decimals share the
    same parser entry-point because :func:`parse_optional_float` /
    :func:`parse_int` already accept signed input.
    """

    def _decimal(value: str) -> float | str | None:
        return parse_optional_float(value, field=type_name)

    def _integer(value: str) -> float | str | None:
        if value == _ABSENT_TOKEN:
            return None
        return parse_int(value, field=type_name)

    def _text(value: str) -> float | str | None:
        return value

    # Strip leading "s " (signed marker) so "s decimal" maps the same as "decimal".
    lowered = type_name.lower()
    lowered = lowered.removeprefix("s ")
    if lowered in {"decimal", "float", "double", "signed", "unsigned"}:
        return _decimal
    if lowered in {"integer", "int", "int8", "int16", "int32", "uint"}:
        return _integer
    return _text


def _df_split_name_and_type(tail: list[str]) -> tuple[str, str, list[str]]:
    """Split the post-stat-code tokens into ``(name, type, remainder)``.

    Walks ``tail`` from the left looking for the type token boundary.
    A standalone type token (`string`, `decimal`, ...) marks the start of
    the type column; a `s` token immediately followed by a type token marks
    a signed variant (joined as `s decimal` etc.).
    """
    for i, tok in enumerate(tail):
        lowered = tok.lower()
        if lowered in _DF_TYPE_TOKENS:
            name = " ".join(tail[:i])
            return name, tok, tail[i + 1 :]
        if lowered == "s" and i + 1 < len(tail) and tail[i + 1].lower() in _DF_TYPE_TOKENS:
            name = " ".join(tail[:i])
            type_name = f"{tok} {tail[i + 1]}"
            return name, type_name, tail[i + 2 :]
    return "", "", []


def _df_canonicalise_name(name: str) -> str:
    """Map an Alicat-emitted field name to its canonical (underscored) form.

    Internal spaces in the wire name (`Mass Flow Setpt`) become underscores
    so the canonical form (`Mass_Flow_Setpt`) round-trips against the
    statistic registry's alias table. Returns the input unchanged if the
    mapping is a no-op.
    """
    return name.strip().replace(" ", "_")


def _df_resolve_statistic(*candidates: str) -> Statistic | None:
    """Try each candidate name through the statistic registry; first hit wins."""
    for candidate in candidates:
        try:
            return statistic_registry.coerce(candidate)
        except UnknownStatisticError:
            continue
    return None


def _df_detect_flavor(lines: Sequence[bytes]) -> DataFrameFormatFlavor:
    """Sniff the column-header row to pick a dialect.

    The V8+ header line has ``ID_`` as its third token (in the stat-code
    column position), e.g. ``A D00 ID_ NAME...``. The V1_V7 header has
    ``NAME...`` as its third token (no stat-code column), e.g.
    ``A  D00 NAME_______ TYPE_____ ...``. Devices that don't emit a
    recognisable header default to the V8+ DEFAULT dialect — which makes
    the parser fail fast with ``AlicatParseError`` rather than silently
    misparse an unfamiliar shape.
    """
    for raw_line in lines:
        text = _strip_gp_padding(parse_ascii(raw_line).rstrip("\r\n"))
        if not text.strip():
            continue
        tokens = text.split()
        if len(tokens) < _DF_HEADER_MIN_TOKENS_FOR_SNIFF:
            continue
        # All header rows begin `<uid> D<NN>`; skip lines that don't.
        code = tokens[1]
        if len(code) < _DF_CODE_MIN_LEN or code[0] not in {"D", "d"} or not code[1:].isdigit():
            continue
        third = tokens[2]
        if third == _DF_HEADER_MARKER:
            return DataFrameFormatFlavor.DEFAULT
        if third.startswith(_DF_V1_V7_HEADER_MARKER_PREFIX):
            return DataFrameFormatFlavor.LEGACY
        # Header row that doesn't match either marker — assume DEFAULT
        # (V8+) and let the row-shape parser fail loudly on a bad line
        # rather than guessing here.
        return DataFrameFormatFlavor.DEFAULT
    # No identifiable header at all — DEFAULT will fail-fast on the empty
    # field list, which is the right surface.
    return DataFrameFormatFlavor.DEFAULT


def _parse_data_frame_table_default(lines: Sequence[bytes]) -> DataFrameFormat:
    """V8+ ``??D*`` dialect parser. See :func:`parse_data_frame_table`."""
    fields: list[DataFrameField] = []
    for raw_line in lines:
        text = _strip_gp_padding(parse_ascii(raw_line).rstrip("\r\n"))
        if not text.strip():
            continue
        tokens = text.split()
        # Need at least <uid> D<NN> <stat_code> <name> <type> — 5 tokens minimum.
        if len(tokens) < _DF_DEFAULT_ROW_MIN_TOKENS:
            continue
        uid, code, stat_code, *tail = tokens
        # Reject rows that don't look like field rows.
        if len(code) < _DF_CODE_MIN_LEN or code[0] not in {"D", "d"} or not code[1:].isdigit():
            continue
        # Skip the column header row.
        if stat_code == _DF_HEADER_MARKER:
            continue

        name, type_name, remainder = _df_split_name_and_type(tail)
        if not name or not type_name:
            # Couldn't find a type-token boundary — not a field row.
            continue
        del uid

        conditional = name.startswith("*")
        if conditional:
            name = name[1:].strip()
        canonical_name = _df_canonicalise_name(name)

        # The remainder starts with the width column (e.g. `7/2` or `1`),
        # then optionally `<unit_code> <precision> <unit_label>` (decimal
        # rows) or `<flag_mnemonic>` (status / error rows). We bind the
        # unit by label when we can recognise a 3-token "<digits> <digits>
        # <label>" trailer, since unit codes collide across categories
        # (the registry needs a category for code-based lookup; label
        # lookup is unambiguous via aliases / canonical values).
        unit: Unit | None = None
        if (
            len(remainder) >= 4  # noqa: PLR2004 — width + 3 NOTES tokens
            and remainder[1].isdigit()
            and remainder[2].isdigit()
        ):
            label = remainder[3]
            with contextlib.suppress(UnknownUnitError):
                unit = unit_registry.coerce(label)

        fields.append(
            DataFrameField(
                name=canonical_name,
                raw_name=name,
                type_name=type_name,
                statistic=_df_resolve_statistic(canonical_name, name),
                unit=unit,
                conditional=conditional,
                parser=_df_parser_for_type(type_name),
            )
        )

    if not fields:
        raise AlicatParseError(
            "??D*: no field lines recognised in response (V8+ dialect)",
            field_name="data_frame_table",
            expected=">=1 field line",
            actual=0,
            context=ErrorContext(command_name="??D*"),
        )

    return DataFrameFormat(fields=tuple(fields), flavor=DataFrameFormatFlavor.DEFAULT)


def _df_split_name_and_type_v1_v7(
    tail: list[str],
) -> tuple[str, str, list[str]]:
    """Split V1_V7 ``<name (multi-word)> <type> <min> <max> <units>`` tokens."""
    for i, tok in enumerate(tail):
        if tok.lower() in _DF_TYPE_TOKENS_V1_V7:
            name = " ".join(tail[:i])
            return name, tok, tail[i + 1 :]
    return "", "", []


def _parse_data_frame_table_v1_v7(lines: Sequence[bytes]) -> DataFrameFormat:
    """V1_V7 ``??D*`` dialect parser (5v12-era).

    Wire shape (verified on a 5v12 capture, design §16.6.2)::

        <uid>  D00 NAME_______ TYPE_____ MinVal_  MaxVal_  UNITS__
        <uid>  D<NN> <name (multi-word)> <type> <min> <max> <units>

    Differences from the V8+ dialect handled here:

    - No statistic-code column; the canonical statistic is looked up
      from the field name alone.
    - Type tokens are ``signed`` / ``char`` / ``string`` (rather than
      ``s decimal`` / ``string`` etc.).
    - Min / max range tokens occupy two columns where V8+ has just a
      width tuple; we capture the trailing UNITS token and ignore the
      range columns (they're not used by the poll parser).
    - Engineering unit is the trailing single token (``PSIA`` / ``CCM``
      / ``SCCM`` / ``C`` / ``na``); we resolve via the registry as
      with the V8+ dialect.
    - Conditional fields lack the V8+ ``*<name>`` marker; we recognise
      ``Error`` and ``Status`` (the two conventional Alicat conditional
      names) by name.
    """
    fields: list[DataFrameField] = []
    for raw_line in lines:
        text = _strip_gp_padding(parse_ascii(raw_line).rstrip("\r\n"))
        if not text.strip():
            continue
        tokens = text.split()
        # Minimum row: <uid> D<NN> <name> <type> <min> <max> <units> = 7 tokens.
        if len(tokens) < _DF_V1_V7_ROW_MIN_TOKENS:
            continue
        uid, code, *tail = tokens
        if len(code) < _DF_CODE_MIN_LEN or code[0] not in {"D", "d"} or not code[1:].isdigit():
            continue
        # Skip the column header row (third token starts with `NAME`).
        if tail and tail[0].startswith(_DF_V1_V7_HEADER_MARKER_PREFIX):
            continue

        name, type_name, remainder = _df_split_name_and_type_v1_v7(tail)
        if not name or not type_name:
            continue
        del uid

        canonical_name = _df_canonicalise_name(name)
        conditional = name in _DF_V1_V7_CONDITIONAL_NAMES

        # remainder = [min, max, units]. Trailing token is the unit label.
        unit: Unit | None = None
        if remainder:
            label = remainder[-1]
            if label != _DF_V1_V7_UNIT_NA:
                with contextlib.suppress(UnknownUnitError):
                    unit = unit_registry.coerce(label)

        fields.append(
            DataFrameField(
                name=canonical_name,
                raw_name=name,
                type_name=type_name,
                statistic=_df_resolve_statistic(canonical_name, name),
                unit=unit,
                conditional=conditional,
                parser=_df_parser_for_type(type_name),
            )
        )

    if not fields:
        raise AlicatParseError(
            "??D*: no field lines recognised in response (V1_V7 dialect)",
            field_name="data_frame_table",
            expected=">=1 field line",
            actual=0,
            context=ErrorContext(command_name="??D*"),
        )

    return DataFrameFormat(fields=tuple(fields), flavor=DataFrameFormatFlavor.LEGACY)


def parse_data_frame_table(lines: Sequence[bytes]) -> DataFrameFormat:
    """Parse a ``??D*`` response into a :class:`DataFrameFormat`.

    Auto-detects the dialect by sniffing the column-header row, then
    dispatches to the appropriate per-dialect parser:

    - **V8+ dialect (DEFAULT)** — V8/V9 + V10 captures (design §16.6).
      Header: ``<uid> D00 ID_ NAME... TYPE... WIDTH NOTES...``.
      Field rows carry a stat-code column and conditional rows are
      marked with a leading ``*`` on the name.
    - **V1_V7 dialect** — 5v12 capture (design §16.6.2). Header:
      ``<uid>  D00 NAME... TYPE... MinVal MaxVal UNITS...``. No
      stat-code column, no ``*`` marker; engineering units sit in the
      trailing column.

    Per-field :attr:`DataFrameField.unit` is bound inline when the
    dialect carries a recognisable unit label.

    Raises:
        AlicatParseError: Non-ASCII bytes, or no field lines were
            recognised in either dialect.
    """
    flavor = _df_detect_flavor(lines)
    if flavor is DataFrameFormatFlavor.LEGACY:
        return _parse_data_frame_table_v1_v7(lines)
    return _parse_data_frame_table_default(lines)


def parse_data_frame(raw: bytes, fmt: DataFrameFormat) -> ParsedFrame:
    """Parse ``raw`` against ``fmt`` into a :class:`ParsedFrame`.

    Thin delegator to :meth:`DataFrameFormat.parse`. Provided as a
    free-function alias so all low-level parsers share one import site
    (:mod:`alicatlib.protocol.parser`), matching the rest of design §5.11.
    Pure — no clocks; the session captures ``received_at`` /
    ``monotonic_ns`` and wraps via :meth:`DataFrame.from_parsed`.
    """
    return fmt.parse(raw)


# Gas-list line shape. We match "<uid> G<NN> <code> <label...>", where
# G<NN> is the row index (1..n) and <code> is the primer's gas code
# (used as the dict key). The trailing label may be one token (short
# name) or two (short + long) — we preserve the full trailing run
# Real V10 ??G* format (verified 2026-04-17 on MC-500SCCM-D, design §16.6):
#
#   <uid> G<NN>      <short_name>
#
# where NN is the gas's per-device code (zero-padded to 2-3 digits) and the
# short name is right-aligned in a fixed-width column. There is no separate
# numeric code token — the G<NN> row index IS the code. There is no count
# header on real hardware. There is no long-name column on the wire.
_GAS_LIST_LINE_RE = re.compile(
    r"^(?P<uid>\S+)\s+G(?P<code>\d{1,3})\s+(?P<label>\S.*?)\s*$",
)

# GP firmware emits the entire ``??G*`` list on a single line with no ``\r``
# separating entries. Each entry is ``<uid> G<NN>     <short_name>``; gas
# short-names don't contain whitespace (``Air``, ``C-25``, ``n-C4H10``), so
# a ``\S+`` token captures them. Hardware validation on 2026-04-17 §16.6.8.
_GAS_LIST_INLINE_ENTRY_RE = re.compile(r"(?P<uid>\S+)\s+G(?P<code>\d{1,3})\s+(?P<label>\S+)")


def _maybe_split_gp_inline_gas_list(lines: Sequence[bytes]) -> Sequence[bytes]:
    """Split a single-line, multi-entry ``??G*`` reply into per-entry lines.

    Runs only when ``lines`` is a single element that contains more than
    one entry — so the typical multi-line path is unaffected. Returns
    ``lines`` unchanged when no split applies.
    """
    if len(lines) != 1:
        return lines
    text = _strip_gp_padding(parse_ascii(lines[0]).rstrip("\r\n"))
    matches = list(_GAS_LIST_INLINE_ENTRY_RE.finditer(text))
    if len(matches) <= 1:
        return lines
    return tuple(m.group(0).encode("ascii") for m in matches)


def parse_gas_list(lines: Sequence[bytes]) -> dict[int, str]:
    """Parse a ``??G*`` response into ``{gas_code: short_name}``.

    Real V10 wire shape (verified 2026-04-17 on a MC-500SCCM-D, design
    §16.6)::

        <unit_id> G<NN>      <short_name>

    The integer in the ``G<NN>`` row label is the device-side gas code
    (which coincides with the canonical Appendix-C code for the built-in
    gases G00..G29 and continues with mixture/specialty slots beyond).
    The short name (e.g. ``Air``, ``CH4``, ``N2``) is right-aligned in a
    fixed-width column; we collapse the leading whitespace.

    Invariants:

    - A consistent ``unit_id`` across every parsed line — mismatch
      raises :class:`AlicatUnitIdMismatchError`.
    - Duplicate gas codes raise :class:`AlicatParseError` rather than
      silently overwriting — a duplicate would mask firmware oddities.
    - Empty responses or responses with no recognisable gas lines raise
      :class:`AlicatParseError`; the ``??G*`` command always has at
      least one built-in gas on any supported device.

    Returns:
        Mapping from Alicat gas code (per-device, often matching primer
        Appendix C for built-ins) to the wire short name.
        :meth:`gas_registry.coerce` resolves the short name to the typed
        :class:`~alicatlib.registry.Gas` member.
    """
    if not lines:
        raise AlicatParseError(
            "??G*: empty response",
            field_name="gas_list",
            expected=">=1 line",
            actual=0,
            context=ErrorContext(command_name="??G*"),
        )

    # GP firmware returns the full list on one line; split it before the
    # per-line loop so every downstream invariant stays the same.
    lines = _maybe_split_gp_inline_gas_list(lines)

    unit_id: str | None = None
    by_code: dict[int, str] = {}

    for raw_line in lines:
        text = _strip_gp_padding(parse_ascii(raw_line).rstrip("\r\n"))
        if not text.strip():
            continue
        m = _GAS_LIST_LINE_RE.match(text)
        if m is None:
            # Lines that don't match the row shape (preamble, blank, etc.)
            # are silently skipped.
            continue

        label = m.group("label").strip()
        if not label:
            continue

        line_uid = m.group("uid")
        code = int(m.group("code"))

        if unit_id is None:
            unit_id = line_uid
        elif unit_id != line_uid:
            raise AlicatUnitIdMismatchError(
                f"??G*: unit_id {line_uid!r} on G{m.group('code')} line does not match {unit_id!r}",
                context=ErrorContext(
                    command_name="??G*",
                    unit_id=unit_id,
                    raw_response=raw_line,
                ),
            )

        if code in by_code:
            raise AlicatParseError(
                f"??G*: duplicate gas code {code} — {text!r}",
                field_name="gas_list_code",
                expected="unique gas codes",
                actual=code,
                context=ErrorContext(command_name="??G*", raw_response=raw_line),
            )

        by_code[code] = label

    if not by_code:
        raise AlicatParseError(
            "??G*: no gas entries recognised in response",
            field_name="gas_list",
            expected=">=1 gas entry",
            actual=0,
            context=ErrorContext(command_name="??G*"),
        )

    return by_code
