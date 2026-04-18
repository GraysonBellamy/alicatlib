"""alicatlib — Python library for the full Alicat instrument matrix.

Covers {flow, pressure} × {meter, controller} × {gas, liquid}, plus the
CODA Coriolis line. Orthogonal :class:`Medium` gating (design §5.9a)
lets every command declare which media it applies to; the session
refuses cross-medium dispatch pre-I/O with
:class:`AlicatMediumMismatchError`. Devices whose prefix doesn't
uniquely determine the configured medium can be narrowed at open time
via ``assume_media=`` on :func:`~alicatlib.devices.factory.open_device`.

Core API is ``async`` (built on ``anyio``); a sync facade is available at
:mod:`alicatlib.sync` for scripts, notebooks, and REPL use.

See ``docs/design.md`` for the architectural design.
"""

from __future__ import annotations

from alicatlib.config import AlicatConfig, config_from_env
from alicatlib.devices import DeviceKind, Medium
from alicatlib.devices.discovery import (
    DEFAULT_DISCOVERY_BAUDRATES,
    DiscoveryResult,
    find_devices,
    list_serial_ports,
    probe,
)
from alicatlib.devices.factory import open_device
from alicatlib.errors import (
    AlicatCapabilityError,
    AlicatCommandRejectedError,
    AlicatConfigurationError,
    AlicatConnectionError,
    AlicatDiscoveryError,
    AlicatError,
    AlicatFirmwareError,
    AlicatMediumMismatchError,
    AlicatMissingHardwareError,
    AlicatParseError,
    AlicatProtocolError,
    AlicatSinkDependencyError,
    AlicatSinkError,
    AlicatSinkSchemaError,
    AlicatSinkWriteError,
    AlicatStreamingModeError,
    AlicatTimeoutError,
    AlicatTransportError,
    AlicatUnitIdMismatchError,
    AlicatUnsupportedCommandError,
    AlicatValidationError,
    ErrorContext,
    InvalidUnitIdError,
    UnknownFluidError,
    UnknownGasError,
    UnknownStatisticError,
    UnknownUnitError,
)
from alicatlib.firmware import FirmwareVersion
from alicatlib.manager import AlicatManager, DeviceResult, ErrorPolicy
from alicatlib.registry import Gas, LoopControlVariable, Statistic, Unit
from alicatlib.version import __version__

__all__ = [
    "DEFAULT_DISCOVERY_BAUDRATES",
    "AlicatCapabilityError",
    "AlicatCommandRejectedError",
    "AlicatConfig",
    "AlicatConfigurationError",
    "AlicatConnectionError",
    "AlicatDiscoveryError",
    "AlicatError",
    "AlicatFirmwareError",
    "AlicatManager",
    "AlicatMediumMismatchError",
    "AlicatMissingHardwareError",
    "AlicatParseError",
    "AlicatProtocolError",
    "AlicatSinkDependencyError",
    "AlicatSinkError",
    "AlicatSinkSchemaError",
    "AlicatSinkWriteError",
    "AlicatStreamingModeError",
    "AlicatTimeoutError",
    "AlicatTransportError",
    "AlicatUnitIdMismatchError",
    "AlicatUnsupportedCommandError",
    "AlicatValidationError",
    "DeviceKind",
    "DeviceResult",
    "DiscoveryResult",
    "ErrorContext",
    "ErrorPolicy",
    "FirmwareVersion",
    "Gas",
    "InvalidUnitIdError",
    "LoopControlVariable",
    "Medium",
    "Statistic",
    "Unit",
    "UnknownFluidError",
    "UnknownGasError",
    "UnknownStatisticError",
    "UnknownUnitError",
    "__version__",
    "config_from_env",
    "find_devices",
    "list_serial_ports",
    "open_device",
    "probe",
]
