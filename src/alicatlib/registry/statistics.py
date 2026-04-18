"""Statistic registry — typed enum + alias-aware lookup singleton."""

from __future__ import annotations

from alicatlib.errors import UnknownStatisticError
from alicatlib.registry._codes_gen import STATISTIC_ALIASES, STATISTIC_BY_CODE, Statistic
from alicatlib.registry.aliases import AliasRegistry

__all__ = ["Statistic", "statistic_registry"]

statistic_registry: AliasRegistry[Statistic] = AliasRegistry(
    Statistic,
    aliases=dict(STATISTIC_ALIASES),
    by_code=dict(STATISTIC_BY_CODE),
    error_cls=UnknownStatisticError,
)
