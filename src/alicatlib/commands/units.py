"""Engineering-units + full-scale commands.

Two Tier-1 commands (design §9):

- :data:`ENGINEERING_UNITS` (``DCU``) — query or set the engineering unit
  for a given :class:`Statistic`. Both the statistic-level and
  group-level forms share one spec; the request's ``apply_to_group``
  flag switches between them. Setting units on a session-cached
  data-frame format invalidates the format so the next poll re-probes
  ``??D*`` (design §5.6 final paragraph) — that lifecycle concern lives
  at the facade, not the command.
- :data:`FULL_SCALE_QUERY` (``FPF``) — query the full-scale value for a
  given statistic, returning a :class:`FullScaleValue`. Consumed
  downstream by setpoint range validation (design §5.20.2).

Wire shapes assumed by the encoders / decoders here are primer-derived
and synthetic-fixture-backed; a real hardware capture may refine
spacing or trailing tokens. Per design §15.3 ("best-guess mappings,
hardware-correctable"), any refinement is a one-line regex change +
fixture refresh.
"""

from __future__ import annotations

from dataclasses import dataclass

from alicatlib.commands._firmware_cutoffs import MIN_FIRMWARE_DCU
from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import FullScaleValue, UnitSetting
from alicatlib.errors import AlicatValidationError, ErrorContext, UnknownUnitError
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_fields, parse_float, parse_int
from alicatlib.registry import (
    Statistic,
    Unit,
    statistic_registry,
    unit_registry,
)
from alicatlib.registry._codes_gen import STATISTIC_BY_CODE, UNIT_BY_CATEGORY_CODE

__all__ = [
    "ENGINEERING_UNITS",
    "FULL_SCALE_QUERY",
    "EngineeringUnits",
    "EngineeringUnitsRequest",
    "FullScaleQuery",
    "FullScaleQueryRequest",
]


# Device kinds that emit a statistic → unit mapping. ``DCU`` / ``FPF`` make
# sense on any kind that reports measurable statistics; the full
# :class:`DeviceKind` set is fine because UNKNOWN-kind devices still
# surface a valid statistic table.
_ALL_DEVICE_KINDS: frozenset[DeviceKind] = frozenset(DeviceKind)


def _resolve_statistic_code(value: Statistic | str) -> tuple[Statistic, int]:
    """Coerce ``value`` and return ``(statistic, code)``.

    Reverse-lookup through :data:`STATISTIC_BY_CODE`. Codegen guarantees
    every Statistic member has a code; an absence means ``codes.json``
    and ``_codes_gen.py`` drifted — surfaced loudly as
    :class:`AlicatValidationError` rather than silently guessing.
    """
    stat = statistic_registry.coerce(value)
    for code, member in STATISTIC_BY_CODE.items():
        if member is stat:
            return stat, code
    raise AlicatValidationError(
        f"{stat!r} has no numeric code — codes.json / _codes_gen.py drift?",
    )


def _resolve_unit_code(unit: Unit | int | str) -> int:
    """Resolve ``unit`` to the numeric code ``DCU`` expects on the wire.

    Precedence:

    - ``int`` — used verbatim (no category disambiguation needed).
    - ``str`` — coerced to :class:`Unit` via :func:`unit_registry.coerce`
      (accepts canonical values, aliases, case-insensitive).
    - :class:`Unit` — looked up in :data:`UNIT_BY_CATEGORY_CODE`. If the
      unit maps to exactly one ``(category, code)`` pair the code is
      unambiguous; otherwise a :class:`AlicatValidationError` directs
      the caller to pass the raw ``int`` code.

    This mirrors the primer's DCU semantics: the device interprets the
    code relative to the statistic's native category, so we only need
    to emit a number. Ambiguous Unit enum members (``DEFAULT``,
    ``UNKNOWN``, ``COUNT``, ``PERCENT`` — any unit that appears in
    multiple categories with the same code) are safe when the code is
    identical across categories; otherwise the caller must disambiguate.
    """
    if isinstance(unit, bool):  # bool is an int subclass — reject explicitly.
        raise TypeError("unit must be Unit | int | str, not bool")
    if isinstance(unit, int):
        return unit
    typed = unit if isinstance(unit, Unit) else unit_registry.coerce(unit)

    # Find every (category, code) pair for this Unit. Collapse to a
    # unique set of codes; if there's exactly one, use it. If multiple
    # codes map to the same Unit across categories (unusual — the
    # unit-code scheme is category-scoped), fail loud.
    codes = {code for (_cat, code), u in UNIT_BY_CATEGORY_CODE.items() if u is typed}
    if len(codes) == 1:
        return next(iter(codes))
    if not codes:
        raise UnknownUnitError(typed.value, suggestions=())
    raise AlicatValidationError(
        f"{typed!r} maps to multiple codes across categories ({sorted(codes)}); "
        "pass the raw int code explicitly to disambiguate",
        context=ErrorContext(extra={"unit": typed.value, "codes": sorted(codes)}),
    )


def _resolve_response_unit(label: str, code: int) -> Unit | None:
    """Best-effort resolve the ``Unit`` member from a DCU / FPF reply.

    Tries the human label first (``"SLPM"``, ``"bar"``, …) — that path
    is category-agnostic. Falls back to raw-code lookup by scanning
    every category; if multiple hits collapse to one :class:`Unit`,
    use it. Anything else returns ``None``; the raw label is always
    preserved by the caller for diagnostics.
    """
    try:
        return unit_registry.coerce(label)
    except UnknownUnitError:
        pass
    matches = {u for (_cat, c), u in UNIT_BY_CATEGORY_CODE.items() if c == code}
    if len(matches) == 1:
        return next(iter(matches))
    return None


# ---------------------------------------------------------------------------
# ENGINEERING_UNITS (``DCU``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineeringUnitsRequest:
    """Arguments for :data:`ENGINEERING_UNITS`.

    Attributes:
        statistic: Statistic to query or set units on. Accepts the
            :class:`Statistic` member or any registered alias / value
            string.
        unit: ``None`` issues the query form. An :class:`Unit` member,
            numeric code, or registered alias sets the unit.
        apply_to_group: When setting, broadcast the change to the
            statistic's group instead of the single statistic.
        override_special_rules: When setting, override any
            device-specific restrictions on the statistic / unit pair.
            Only meaningful on set; ignored in query form.
    """

    statistic: Statistic | str
    unit: Unit | int | str | None = None
    apply_to_group: bool = False
    override_special_rules: bool = False


@dataclass(frozen=True, slots=True)
class EngineeringUnits(Command[EngineeringUnitsRequest, UnitSetting]):
    """``DCU`` — engineering-units query / set.

    Wire shape (primer-derived; hardware-correctable):

    - Query:   ``<uid><prefix>DCU <stat>``
    - Set:     ``<uid><prefix>DCU <stat> <unit>``
    - Group:   ``<uid><prefix>DCU <stat> <unit> 1``
    - Special: ``<uid><prefix>DCU <stat> <unit> 1 1``

    Response shape: ``<uid> <stat_code> <unit_code> <unit_label>``.
    """

    name: str = "engineering_units"
    token: str = "DCU"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = MIN_FIRMWARE_DCU
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V10},
    )

    def encode(
        self,
        ctx: DecodeContext,
        request: EngineeringUnitsRequest,
    ) -> bytes:
        """Emit the DCU query / set bytes."""
        _, stat_code = _resolve_statistic_code(request.statistic)
        prefix = ctx.command_prefix.decode("ascii")
        head = f"{ctx.unit_id}{prefix}{self.token} {stat_code}"

        if request.unit is None:
            # Query form. ``apply_to_group`` / ``override_special_rules``
            # are set-only semantics; silently ignored here so a caller
            # that constructs the request with default flags for both
            # query and set doesn't have to reset them.
            return (head + "\r").encode("ascii")

        unit_code = _resolve_unit_code(request.unit)
        tokens = [head, str(unit_code)]
        if request.apply_to_group or request.override_special_rules:
            tokens.append("1" if request.apply_to_group else "0")
            if request.override_special_rules:
                tokens.append("1")
        return (" ".join(tokens) + "\r").encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> UnitSetting:
        """Parse ``<uid> <unit_code> <unit_label>`` into :class:`UnitSetting`.

        Verified against a V10 capture on 2026-04-17 (design §16.6) — the
        device does *not* echo the requested statistic in the reply, so
        ``statistic`` is left as :attr:`Statistic.NONE` and the facade
        fills it from the request via :func:`dataclasses.replace`.
        """
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=3)
        unit_id, unit_code_s, label = fields
        unit_code = parse_int(unit_code_s, field="unit_code")

        return UnitSetting(
            unit_id=unit_id,
            statistic=Statistic.NONE,  # facade replaces with request.statistic
            unit=_resolve_response_unit(label, unit_code),
            label=label,
        )


ENGINEERING_UNITS: EngineeringUnits = EngineeringUnits()


# ---------------------------------------------------------------------------
# FULL_SCALE_QUERY (``FPF``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FullScaleQueryRequest:
    """Arguments for :data:`FULL_SCALE_QUERY`.

    Attributes:
        statistic: Statistic whose full-scale value to query.
    """

    statistic: Statistic | str


@dataclass(frozen=True, slots=True)
class FullScaleQuery(Command[FullScaleQueryRequest, FullScaleValue]):
    r"""``FPF`` — full-scale query for a single statistic.

    Wire shape: ``<uid><prefix>FPF <stat>\r``.

    Response (primer-derived): ``<uid> <stat_code> <value> <unit_code> <unit_label>``.
    """

    name: str = "full_scale_query"
    token: str = "FPF"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    # Primer lists FPF as 6v00+ (numeric families only). GP firmware
    # does not implement it (design §16.6.8). Runtime rejection on
    # 5v12-era V1_V7 devices is still possible; the family gate only
    # blocks GP pre-I/O.
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V1_V7, FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(
        self,
        ctx: DecodeContext,
        request: FullScaleQueryRequest,
    ) -> bytes:
        r"""Emit ``<unit_id><prefix>FPF <stat_code>\r``."""
        _, stat_code = _resolve_statistic_code(request.statistic)
        prefix = ctx.command_prefix.decode("ascii")
        return f"{ctx.unit_id}{prefix}{self.token} {stat_code}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> FullScaleValue:
        """Parse ``<uid> <value> <unit_code> <unit_label>`` into :class:`FullScaleValue`.

        Verified against a V10 capture on 2026-04-17 (design §16.6) — the
        device does *not* echo the requested statistic in the reply, so
        ``statistic`` is left as :attr:`Statistic.NONE` and the facade
        fills it from the request via :func:`dataclasses.replace`.
        """
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=4)
        _unit_id, value_s, unit_code_s, label = fields
        value = parse_float(value_s, field="full_scale_value")
        unit_code = parse_int(unit_code_s, field="unit_code")

        return FullScaleValue(
            statistic=Statistic.NONE,  # facade replaces with request.statistic
            value=value,
            unit=_resolve_response_unit(label, unit_code),
            unit_label=label,
        )


FULL_SCALE_QUERY: FullScaleQuery = FullScaleQuery()
