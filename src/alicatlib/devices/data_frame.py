r"""Data-frame format, parsing, and timing-wrapped result.

The Alicat ``A\r`` poll response is *core* to the polling path, yet its shape
is device-dependent — Alicat advertises it via ``??D*`` at session start.
This module models that format explicitly (so positional parsing survives
conditional ``*``-marked fields), keeps the byte-level parse pure (no
clocks), and layers timing provenance on top via :class:`DataFrame`.

The split between :class:`ParsedFrame` (pure bytes → typed values) and
:class:`DataFrame` (``ParsedFrame`` + ``received_at`` / ``monotonic_ns``) is
load-bearing: parser unit tests stay clock-free (no freeze-time mocking),
and the :class:`~alicatlib.devices.session.Session` owns the single place
that captures timing. See design doc §5.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

from alicatlib.devices.models import StatusCode
from alicatlib.errors import AlicatParseError, ErrorContext

# Note: ``decode_ascii`` is imported lazily inside :meth:`DataFrameFormat.parse`
# below to avoid an import cycle. ``alicatlib.protocol/__init__.py`` re-exports
# from ``parser.py``, which in turn imports from this module — so importing
# anything via ``alicatlib.protocol.framing`` at module load time triggers the
# package ``__init__`` and re-enters this module before its own classes are
# defined. Function-local import sidesteps the cycle without restructuring
# the public ``alicatlib.protocol`` surface (design §15.1).

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from alicatlib.registry._codes_gen import Statistic, Unit

__all__ = [
    "DataFrame",
    "DataFrameField",
    "DataFrameFormat",
    "DataFrameFormatFlavor",
    "ParsedFrame",
]

_STATUS_VALUES: frozenset[str] = frozenset(code.value for code in StatusCode)


class DataFrameFormatFlavor(Enum):
    """Wire-format generation for the ``??D*`` data-frame advertisement.

    Alicat firmware has used at least two distinct ``??D*`` layouts over
    the years (design §16.6 / §16.6.2 / §16.6.4). The flavor lives on
    :class:`~alicatlib.commands.base.DecodeContext` so the dispatching
    parser knows which line-shape to expect.

    Captured-device map (2026-04-17 hardware validation):

    - ``DEFAULT`` — canonical Alicat layout. Column header ``<uid> D00
      ID_ NAME... TYPE... WIDTH NOTES...``. Field rows carry an explicit
      stat-code column and conditional fields are marked with a leading
      ``*<name>``. **Devices observed:** 6v21, 8v17, 8v30, 10v04, 10v20.
    - ``LEGACY`` — older shape from before the dialect transition.
      Column header ``<uid>  D00 NAME... TYPE... MinVal MaxVal UNITS...``.
      No stat-code column, no ``*`` marker, ``signed`` / ``char`` types
      instead of ``s decimal`` / ``string``, units inline in a single
      column. **Devices observed:** 5v12.
      The transition happened between firmware ``5v12`` and ``6v21`` —
      so the legacy shape is *not* family-correlated (both devices are
      V1_V7-family); it correlates with firmware *version* somewhere
      around the V5→V6 cut-over. The flavor used to be called ``V1_V7``
      but that was misleading; renamed to ``LEGACY``.

    ``SIGNED`` and ``VARIABLE_V8`` are reserved for future use if a
    captured device shows a third / fourth distinct dialect; currently
    unused.
    """

    DEFAULT = 0  # canonical (6v21 + V8/V9 + V10): `ID_ NAME...` header
    SIGNED = 1  # reserved
    VARIABLE_V8 = 2  # reserved
    LEGACY = 3  # 5v12-era: `NAME... TYPE... MinVal MaxVal UNITS` header


@dataclass(frozen=True, slots=True)
class DataFrameField:
    """One column in the ``??D*``-advertised data-frame format.

    Attributes:
        name: Canonical field name, e.g. ``"Mass_Flow"``. Used as a key in
            :attr:`ParsedFrame.values` and :meth:`DataFrame.get_float`.
        raw_name: The exact name as reported by the device, preserved so a
            fixture diff can surface unexpected firmware-side renames.
        type_name: Wire type as declared in ``??D*`` (e.g. ``"decimal"``,
            ``"integer"``, ``"text"``) — retained for diagnostics, not used
            by the parser (the ``parser`` callable is authoritative).
        statistic: Linkage back to :class:`Statistic` so
            :attr:`ParsedFrame.values_by_statistic` can be built. ``None``
            for fields not modelled in the statistics registry.
        unit: Engineering :class:`Unit` active for this field at the time
            the format was cached. The data frame itself doesn't carry
            units — the session probes ``DCU`` / ``FPF`` at startup to bind
            these. ``None`` when the unit doesn't resolve against the
            registry.
        conditional: ``True`` when ``??D*`` reported this field with a
            leading ``*``. A conditional field appears in the wire frame
            only when its condition is met; the parser tail-matches
            conditionals after all required fields have been consumed.
        parser: Bytes-less (already-decoded string) parser that turns the
            raw token into the typed value. Supplied by the factory that
            builds the format — typically ``parse_float``,
            ``parse_optional_float``, ``parse_int``, or an identity
            callable for text fields.
    """

    name: str
    raw_name: str
    type_name: str
    statistic: Statistic | None
    unit: Unit | None
    conditional: bool
    parser: Callable[[str], float | str | None]


@dataclass(frozen=True, slots=True)
class ParsedFrame:
    """Byte-level parse result. Pure function of (raw, format); no timing.

    The :class:`~alicatlib.devices.session.Session` wraps this into a
    :class:`DataFrame` via :meth:`DataFrame.from_parsed`, at which point
    ``received_at`` and ``monotonic_ns`` are captured from the
    terminator-read call site. Keeping the two separate makes parser
    unit tests clock-free.
    """

    unit_id: str
    values: Mapping[str, float | str | None]
    values_by_statistic: Mapping[Statistic, float | str | None]
    status: frozenset[StatusCode]


@dataclass(frozen=True, slots=True)
class DataFrameFormat:
    """Advertised data-frame layout with a pure :meth:`parse` method.

    The format is cached on the :class:`~alicatlib.devices.session.Session`
    at startup (via ``??D*``) and exposed via
    ``session.refresh_data_frame_format()`` for the rare runtime-mutation
    cases (e.g. after ``FDF`` or ``DCU``). The format is immutable — any
    change produces a new :class:`DataFrameFormat`.
    """

    fields: tuple[DataFrameField, ...]
    flavor: DataFrameFormatFlavor

    def names(self) -> tuple[str, ...]:
        """Canonical field names, in declared order."""
        return tuple(f.name for f in self.fields)

    def parse(self, raw: bytes) -> ParsedFrame:
        """Parse a single data-frame line into a :class:`ParsedFrame`.

        Strategy (per design §5.6):

        1. Tokenise on whitespace; first token is the device's unit ID.
        2. Match required (non-conditional) fields left-to-right against
           the leading tokens — they always appear.
        3. Walk the surplus tokens. Any token matching a
           :class:`~alicatlib.devices.models.StatusCode` value collapses
           into :attr:`ParsedFrame.status`; remaining tokens are assigned
           to conditional fields in declared order.
        4. Conditional fields that never receive a token are simply absent
           from :attr:`ParsedFrame.values` — they are not ``None``. This
           matters for downstream sinks: an absent column is distinct from
           a column whose value is the ``--`` sentinel (which *does* land
           as ``None`` via :func:`parse_optional_float`).

        Raises:
            AlicatParseError: Empty frame, non-ASCII bytes, or not enough
                tokens to cover the required fields.
        """
        from alicatlib.protocol.framing import decode_ascii  # noqa: PLC0415, I001 — see top-of-module note

        text = decode_ascii(raw)
        tokens = text.split()
        if not tokens:
            raise AlicatParseError(
                "empty data frame",
                field_name="data_frame",
                expected=">=1 token",
                actual=raw,
                context=ErrorContext(command_name="poll", raw_response=raw),
            )

        required = tuple(f for f in self.fields if not f.conditional)
        conditional = tuple(f for f in self.fields if f.conditional)

        if len(tokens) < len(required):
            raise AlicatParseError(
                f"data frame truncated: expected >= {len(required)} required fields, "
                f"got {len(tokens)} tokens — {text!r}",
                field_name="data_frame",
                expected=len(required),
                actual=len(tokens),
                context=ErrorContext(command_name="poll", raw_response=raw),
            )

        values: dict[str, float | str | None] = {}
        for field_spec, token in zip(required, tokens[: len(required)], strict=True):
            values[field_spec.name] = field_spec.parser(token)

        tail = tokens[len(required) :]
        status: set[StatusCode] = set()
        conditional_tokens: list[str] = []
        for token in tail:
            if token in _STATUS_VALUES:
                status.add(StatusCode(token))
            else:
                conditional_tokens.append(token)

        for field_spec, token in zip(conditional, conditional_tokens, strict=False):
            values[field_spec.name] = field_spec.parser(token)

        values_by_statistic: dict[Statistic, float | str | None] = {
            f.statistic: values[f.name]
            for f in self.fields
            if f.statistic is not None and f.name in values
        }

        return ParsedFrame(
            unit_id=tokens[0],
            values=MappingProxyType(values),
            values_by_statistic=MappingProxyType(values_by_statistic),
            status=frozenset(status),
        )


@dataclass(frozen=True, slots=True)
class DataFrame:
    """Timing-wrapped :class:`ParsedFrame` — the public polling result.

    Built by :meth:`from_parsed`. ``monotonic_ns`` is for drift analysis
    and scheduling (never wall-clock); ``received_at`` is for data
    provenance in sinks.
    """

    unit_id: str
    format: DataFrameFormat
    values: Mapping[str, float | str | None]
    values_by_statistic: Mapping[Statistic, float | str | None]
    status: frozenset[StatusCode]
    received_at: datetime
    monotonic_ns: int

    @classmethod
    def from_parsed(
        cls,
        parsed: ParsedFrame,
        *,
        format: DataFrameFormat,  # noqa: A002 — "format" is the public kwarg per design §5.5
        received_at: datetime,
        monotonic_ns: int,
    ) -> DataFrame:
        """Wrap a :class:`ParsedFrame` with timing captured at read time."""
        return cls(
            unit_id=parsed.unit_id,
            format=format,
            values=parsed.values,
            values_by_statistic=parsed.values_by_statistic,
            status=parsed.status,
            received_at=received_at,
            monotonic_ns=monotonic_ns,
        )

    def get_float(self, name: str) -> float | None:
        """Return the float value at ``name``, or ``None`` if absent or non-numeric.

        This is the "forgiving" accessor used when a downstream consumer
        wants a numeric value and accepts absence. Text-valued fields and
        the ``--`` sentinel both yield ``None``; exceptions are never
        raised. Callers that need strict behaviour should index
        :attr:`values` directly.
        """
        value = self.values.get(name)
        return value if isinstance(value, float) else None

    def get_statistic(self, stat: Statistic) -> float | str | None:
        """Return the value keyed by :class:`Statistic`, or ``None`` if absent.

        Prefer this over :meth:`get_float` when the caller has a typed
        :class:`Statistic` — it's IDE-completable and robust to wire-name
        renames across firmware versions.
        """
        return self.values_by_statistic.get(stat)

    def as_dict(self) -> dict[str, float | str | None]:
        """Flatten to a JSON/CSV-friendly dict.

        Produces ``{field_name: value, "status": "HLD,OPL", "received_at": iso8601}``
        — status codes collapse into a single comma-joined sorted string
        (empty when no codes are active) so downstream schema is stable
        across rows. Callers that need per-code boolean columns should
        wrap this themselves; the library picks the schema-stable form.
        """
        result: dict[str, float | str | None] = dict(self.values)
        result["status"] = ",".join(sorted(code.value for code in self.status))
        result["received_at"] = self.received_at.isoformat()
        return result
