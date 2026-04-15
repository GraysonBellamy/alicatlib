"""Core package configuration.

Keeping this as a plain dataclass (rather than ``pydantic-settings``) keeps the
core install free of any validation-library dependency; the tradeoff is that
env coercion lives in :func:`config_from_env` rather than being automatic.

Design reference: ``docs/design.md`` §5.18.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Final, Self

DEFAULT_ENV_PREFIX: Final[str] = "PYALICAT_"


@dataclass(frozen=True, slots=True)
class AlicatConfig:
    """Process-wide default settings.

    Individual sessions may override any of these at construction time.

    Attributes:
        default_timeout_s: Default per-command response timeout, in seconds.
        default_baudrate: Default serial baudrate when none is specified.
        drain_before_write: Whether the protocol client should drain any stale
            input bytes before each command. Useful for re-syncing after a
            timeout; adds latency per command.
    """

    default_timeout_s: float = 0.5
    default_baudrate: int = 19200
    drain_before_write: bool = False

    def replace(self, **updates: Any) -> Self:
        """Return a copy of this config with ``updates`` applied."""
        return replace(self, **updates)


def config_from_env(prefix: str = DEFAULT_ENV_PREFIX) -> AlicatConfig:
    """Best-effort env loader.

    Only reads well-known keys; unknown keys are ignored. Missing or
    unparseable values fall back to :class:`AlicatConfig`'s defaults — this
    function never raises. Use explicit dataclass construction when you need
    strict validation.

    Recognised keys (with ``prefix="PYALICAT_"``):

    - ``PYALICAT_DEFAULT_TIMEOUT_S`` — float seconds
    - ``PYALICAT_DEFAULT_BAUDRATE`` — int
    - ``PYALICAT_DRAIN_BEFORE_WRITE`` — ``"1"`` / ``"true"`` / ``"yes"``

    Args:
        prefix: Prefix to prepend to each env key. Defaults to ``"PYALICAT_"``.

    Returns:
        An :class:`AlicatConfig`, falling back to defaults for any missing or
        unparseable env var.
    """
    base = AlicatConfig()

    timeout = _float_env(f"{prefix}DEFAULT_TIMEOUT_S", base.default_timeout_s)
    baudrate = _int_env(f"{prefix}DEFAULT_BAUDRATE", base.default_baudrate)
    drain = _bool_env(f"{prefix}DRAIN_BEFORE_WRITE", base.drain_before_write)

    return AlicatConfig(
        default_timeout_s=timeout,
        default_baudrate=baudrate,
        drain_before_write=drain,
    )


def _float_env(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_TRUE_STRS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRS = frozenset({"0", "false", "no", "off", ""})


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE_STRS:
        return True
    if lowered in _FALSE_STRS:
        return False
    return default
