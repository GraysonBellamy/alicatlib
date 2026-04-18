"""Gas registry — typed enum + alias-aware lookup singleton."""

from __future__ import annotations

from alicatlib.errors import UnknownGasError
from alicatlib.registry._codes_gen import GAS_ALIASES, GAS_BY_CODE, Gas
from alicatlib.registry.aliases import AliasRegistry

__all__ = ["Gas", "gas_registry"]

gas_registry: AliasRegistry[Gas] = AliasRegistry(
    Gas,
    aliases=dict(GAS_ALIASES),
    by_code=dict(GAS_BY_CODE),
    error_cls=UnknownGasError,
)
