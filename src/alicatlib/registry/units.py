"""Unit registry — typed enum + category-aware lookup singleton.

Alicat unit codes repeat across categories (code 7 is ``SLPM`` in std-flow but
``bar`` in pressure), so :meth:`UnitRegistry.by_code` requires an explicit
:class:`UnitCategory`. See design doc §5.3.
"""

from __future__ import annotations

from alicatlib.registry._codes_gen import (
    UNIT_ALIASES,
    UNIT_BY_CATEGORY_CODE,
    UNIT_CATEGORIES,
    Unit,
    UnitCategory,
)
from alicatlib.registry.aliases import UnitRegistry

__all__ = ["Unit", "UnitCategory", "unit_registry"]

unit_registry: UnitRegistry = UnitRegistry(
    aliases=dict(UNIT_ALIASES),
    by_category_code=dict(UNIT_BY_CATEGORY_CODE),
    categories=dict(UNIT_CATEGORIES),
)
