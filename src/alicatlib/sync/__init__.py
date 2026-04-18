"""Sync facade over the async core.

Async is canonical; the sync facade wraps it through
:class:`SyncPortal` so scripts, notebooks, and REPL sessions can drive
devices without ``await``.

Surfaces:

* Device / manager — :class:`Alicat`, :class:`SyncDevice`,
  :class:`SyncFlowMeter`, :class:`SyncFlowController`,
  :class:`SyncPressureMeter`, :class:`SyncPressureController`,
  :class:`SyncAlicatManager` (+ :class:`ErrorPolicy` /
  :class:`DeviceResult` re-exports).
* Recording — :func:`record`, :func:`pipe`,
  :class:`AcquisitionSummary`, :class:`OverflowPolicy`.
* Sinks — :class:`SyncSinkAdapter` +
  :class:`SyncInMemorySink` / :class:`SyncCsvSink` /
  :class:`SyncJsonlSink` / :class:`SyncSqliteSink` /
  :class:`SyncParquetSink` / :class:`SyncPostgresSink`
  (+ :class:`PostgresConfig` re-export).
* Discovery — :func:`list_serial_ports`, :func:`probe`,
  :func:`find_devices`, :class:`DiscoveryResult`,
  :data:`DEFAULT_DISCOVERY_BAUDRATES`.
* Portal primitives — :class:`SyncPortal`, :func:`run_sync`.

See ``docs/design.md`` §5.16 for the design.
"""

from __future__ import annotations

from alicatlib.sync.device import (
    Alicat,
    SyncDevice,
    SyncFlowController,
    SyncFlowMeter,
    SyncPressureController,
    SyncPressureMeter,
)
from alicatlib.sync.discovery import (
    DEFAULT_DISCOVERY_BAUDRATES,
    DiscoveryResult,
    find_devices,
    list_serial_ports,
    probe,
)
from alicatlib.sync.manager import DeviceResult, ErrorPolicy, SyncAlicatManager
from alicatlib.sync.portal import SyncPortal, run_sync
from alicatlib.sync.recording import (
    AcquisitionSummary,
    OverflowPolicy,
    pipe,
    record,
)
from alicatlib.sync.sinks import (
    PostgresConfig,
    SyncCsvSink,
    SyncInMemorySink,
    SyncJsonlSink,
    SyncParquetSink,
    SyncPostgresSink,
    SyncSinkAdapter,
    SyncSqliteSink,
)

__all__ = [
    "DEFAULT_DISCOVERY_BAUDRATES",
    "AcquisitionSummary",
    "Alicat",
    "DeviceResult",
    "DiscoveryResult",
    "ErrorPolicy",
    "OverflowPolicy",
    "PostgresConfig",
    "SyncAlicatManager",
    "SyncCsvSink",
    "SyncDevice",
    "SyncFlowController",
    "SyncFlowMeter",
    "SyncInMemorySink",
    "SyncJsonlSink",
    "SyncParquetSink",
    "SyncPortal",
    "SyncPostgresSink",
    "SyncPressureController",
    "SyncPressureMeter",
    "SyncSinkAdapter",
    "SyncSqliteSink",
    "find_devices",
    "list_serial_ports",
    "pipe",
    "probe",
    "record",
    "run_sync",
]
