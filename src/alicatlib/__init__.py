"""alicatlib — Python library for Alicat mass flow meters and controllers.

Core API is ``async`` (built on ``anyio``); a sync facade is available at
:mod:`alicatlib.sync` for scripts, notebooks, and REPL use.

See ``docs/design.md`` for the architectural design and milestone plan.
"""

from __future__ import annotations

from alicatlib.errors import (
    AlicatCapabilityError,
    AlicatCommandRejectedError,
    AlicatConfigurationError,
    AlicatConnectionError,
    AlicatDiscoveryError,
    AlicatError,
    AlicatFirmwareError,
    AlicatParseError,
    AlicatProtocolError,
    AlicatStreamingModeError,
    AlicatTimeoutError,
    AlicatTransportError,
    AlicatUnitIdMismatchError,
    AlicatUnsupportedCommandError,
    AlicatValidationError,
    ErrorContext,
    InvalidUnitIdError,
    UnknownGasError,
    UnknownStatisticError,
    UnknownUnitError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.version import __version__

__all__ = [
    "AlicatCapabilityError",
    "AlicatCommandRejectedError",
    "AlicatConfigurationError",
    "AlicatConnectionError",
    "AlicatDiscoveryError",
    "AlicatError",
    "AlicatFirmwareError",
    "AlicatParseError",
    "AlicatProtocolError",
    "AlicatStreamingModeError",
    "AlicatTimeoutError",
    "AlicatTransportError",
    "AlicatUnitIdMismatchError",
    "AlicatUnsupportedCommandError",
    "AlicatValidationError",
    "ErrorContext",
    "FirmwareVersion",
    "InvalidUnitIdError",
    "UnknownGasError",
    "UnknownStatisticError",
    "UnknownUnitError",
    "__version__",
]
