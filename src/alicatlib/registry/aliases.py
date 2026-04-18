"""Alias-aware lookup layer on top of the generated enums.

The generated :mod:`alicatlib.registry._codes_gen` defines typed enums
(:class:`Gas`, :class:`Statistic`, :class:`Unit`) plus backing mappings. This
module wraps those mappings in registries that accept either enum members or
loose strings (short names, long names, legacy aliases) and coerce them to the
typed enum — with informative errors on misses.

See design doc §5.3.
"""

from __future__ import annotations

import difflib
from typing import cast

from alicatlib.errors import (
    UnknownGasError,
    UnknownStatisticError,
    UnknownUnitError,
)
from alicatlib.registry._codes_gen import (
    Gas,
    Statistic,
    Unit,
    UnitCategory,
)

# Any of the three concrete "unknown X" errors — they share the
# ``__init__(value: str | int, *, suggestions: tuple[str, ...])`` shape that
# the registry's miss path depends on.
_LookupErrorCls = type[UnknownGasError] | type[UnknownStatisticError] | type[UnknownUnitError]


class _BaseAliasRegistry[E: Gas | Statistic | Unit]:
    """Shared coerce/suggest/aliases logic.

    Subclasses add ``by_code`` with an enum-appropriate signature. Kept internal
    so that downstream code always works with one of the concrete registries
    (:class:`AliasRegistry` or :class:`UnitRegistry`).
    """

    def __init__(
        self,
        enum_cls: type[E],
        *,
        aliases: dict[str, E],
        error_cls: _LookupErrorCls,
    ) -> None:
        self._enum_cls = enum_cls
        self._aliases: dict[str, E] = dict(aliases)
        self._aliases_ci: dict[str, E] = {k.lower(): v for k, v in aliases.items()}
        # Iterating the enum class yields its members; cast because mypy can't
        # infer that ``type[E]`` iteration narrows to ``E`` for str-based enums.
        members: list[E] = [cast("E", m) for m in enum_cls]  # pyright: ignore[reportUnnecessaryCast]
        self._values_ci: dict[str, E] = {m.value.lower(): m for m in members}
        self._error_cls = error_cls
        # Suggestion pool: canonical values + every alias key.
        self._suggestion_pool: tuple[str, ...] = tuple(
            sorted({m.value for m in members} | set(aliases.keys())),
        )

    def coerce(self, value: E | str) -> E:
        """Resolve an enum member, canonical value, or alias to the typed enum.

        Lookup order: exact enum member → exact canonical value → exact alias
        → case-insensitive alias → case-insensitive canonical value.
        """
        if isinstance(value, self._enum_cls):
            return value
        if not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            # Defence-in-depth: users may ignore the type annotation.
            raise TypeError(
                f"cannot coerce {type(value).__name__} to {self._enum_cls.__name__}",
            )
        try:
            return cast("E", self._enum_cls(value))  # pyright: ignore[reportUnnecessaryCast]
        except ValueError:
            pass
        if value in self._aliases:
            return self._aliases[value]
        lowered = value.lower()
        if lowered in self._aliases_ci:
            return self._aliases_ci[lowered]
        if lowered in self._values_ci:
            return self._values_ci[lowered]
        raise self._error_cls(value, suggestions=self.suggest(value))

    def aliases(self, member: E) -> tuple[str, ...]:
        """Return all alias strings that coerce to ``member``."""
        return tuple(k for k, v in self._aliases.items() if v is member)

    def suggest(self, bad: str, *, n: int = 3) -> tuple[str, ...]:
        """Return up to ``n`` close matches for ``bad`` across values and aliases."""
        return tuple(
            difflib.get_close_matches(bad, self._suggestion_pool, n=n, cutoff=0.6),
        )


class AliasRegistry[E: Gas | Statistic | Unit](_BaseAliasRegistry[E]):
    """Registry for enums whose numeric code is unique across the enum.

    Used by :data:`gas_registry` and :data:`statistic_registry`. Not used by
    units, whose codes collide across categories — see :class:`UnitRegistry`.
    """

    def __init__(
        self,
        enum_cls: type[E],
        *,
        aliases: dict[str, E],
        by_code: dict[int, E],
        error_cls: _LookupErrorCls,
    ) -> None:
        super().__init__(enum_cls, aliases=aliases, error_cls=error_cls)
        self._by_code: dict[int, E] = dict(by_code)

    def by_code(self, code: int) -> E:
        """Resolve a numeric Alicat code to the typed enum."""
        try:
            return self._by_code[code]
        except KeyError:
            raise self._error_cls(code, suggestions=()) from None


class UnitRegistry(_BaseAliasRegistry[Unit]):
    """Unit-specialised registry.

    Alicat unit codes repeat across categories (code 7 = ``SLPM`` in std-flow
    but ``bar`` in pressure), so ``by_code`` requires an explicit
    :class:`UnitCategory`. Separate from :class:`AliasRegistry` on purpose —
    making ``category`` optional would silently pick the wrong unit.
    """

    def __init__(
        self,
        *,
        aliases: dict[str, Unit],
        by_category_code: dict[tuple[UnitCategory, int], Unit],
        categories: dict[Unit, frozenset[UnitCategory]],
    ) -> None:
        super().__init__(Unit, aliases=aliases, error_cls=UnknownUnitError)
        self._by_category_code: dict[tuple[UnitCategory, int], Unit] = dict(by_category_code)
        self._categories: dict[Unit, frozenset[UnitCategory]] = dict(categories)

    def by_code(self, code: int, *, category: UnitCategory) -> Unit:
        """Resolve a ``(category, code)`` pair to the typed :class:`Unit`.

        A bare ``by_code(code)`` would be ambiguous — codes repeat across
        categories — so ``category`` is keyword-only and required.
        """
        try:
            return self._by_category_code[(category, code)]
        except KeyError:
            raise UnknownUnitError(
                f"code={code} in category={category.value}",
                suggestions=(),
            ) from None

    def categories(self, unit: Unit) -> frozenset[UnitCategory]:
        """Return the categories (Primer Appendix B sections) this unit applies to."""
        return self._categories[unit]
