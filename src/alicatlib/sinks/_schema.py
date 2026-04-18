"""Shared first-batch schema-lock for tabular sinks.

Every tabular sink in the tree (SQLite, Parquet, Postgres, and
eventually CSV) shares the same schema-evolution policy:

1. **First batch wins.** The column set and order are locked from the
   first :meth:`write_many` call. For schema-less sinks this is just
   bookkeeping; for schema-ful sinks (Parquet schema, Postgres table,
   SQLite CREATE TABLE) the locked spec drives the backing schema.
2. **Unknown columns are dropped with a one-shot WARN.** Later batches
   carrying a new key (e.g. a hot-plugged device with an extra frame
   field) don't reshape the file/table silently — each new key logs
   once, then gets dropped on subsequent batches without re-logging.
3. **Missing columns are filled with ``None``.** Row projection
   guarantees every locked column appears in the output dict, so
   sinks can rely on a stable shape per row.

This module is sink-facing only. It has no public re-export.

Design reference: ``docs/design.md`` §5.15.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping, Sequence

__all__ = ["ColumnSpec", "SchemaLock"]


_SCALAR_TYPE = type[float] | type[int] | type[str]


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One column in a locked tabular schema.

    Attributes:
        name: Column name, verbatim from the source row dict.
        python_type: The concrete Python scalar type backing the
            column. One of :class:`float`, :class:`int`, :class:`str`.
            Sinks translate this into their native type system
            (pyarrow, sqlite affinity, postgres type).
        nullable: ``True`` if the first batch contained at least one
            ``None`` for this column, or if the column is entirely
            absent from some rows. Parquet and Postgres ``CREATE
            TABLE`` paths both need this.
    """

    name: str
    python_type: _SCALAR_TYPE
    nullable: bool


class SchemaLock:
    """Lock a row-dict schema on first batch; drop unknowns on later batches.

    Not thread-safe. Each sink instance owns one :class:`SchemaLock`
    and guards it with whatever lock protects its write path (for
    SQLite, the single connection; for Parquet, the single writer;
    for Postgres, the pool's per-batch acquire).

    Typical sink flow:

    .. code-block:: python

        self._schema = SchemaLock(sink_name="sqlite", logger=_logger)
        # on first write_many:
        specs = self._schema.lock([sample_to_row(s) for s in samples])
        # for every batch (including the first):
        rows = [self._schema.project(sample_to_row(s)) for s in samples]
    """

    def __init__(self, *, sink_name: str, logger: logging.Logger) -> None:
        self._sink_name = sink_name
        self._logger = logger
        self._columns: tuple[ColumnSpec, ...] | None = None
        self._names: frozenset[str] = frozenset()
        self._unknown_warned: set[str] = set()

    @property
    def columns(self) -> tuple[ColumnSpec, ...] | None:
        """The locked columns in declaration order, or ``None`` before lock."""
        return self._columns

    @property
    def is_locked(self) -> bool:
        """``True`` once :meth:`lock` or :meth:`lock_to` has been called."""
        return self._columns is not None

    def lock(
        self,
        rows: Sequence[Mapping[str, object]],
    ) -> tuple[ColumnSpec, ...]:
        """Infer column specs from ``rows`` and lock the schema.

        Column order is determined by first-encounter across the batch
        (stable across Python ``dict`` iteration). Per-column type is
        inferred from the first non-``None`` value; when the batch
        mixes ``int`` and ``float`` for one column the column widens
        to ``float``; any other mix widens to ``str`` so downstream
        backends don't have to reason about type coercion.

        Columns that are entirely ``None`` in the first batch default
        to ``str`` with ``nullable=True`` — a safe choice that lets
        sinks materialise the column as TEXT-affinity without raising.

        Args:
            rows: The first batch, already flattened to row dicts
                (typically by :func:`alicatlib.sinks.base.sample_to_row`).

        Returns:
            The locked :class:`ColumnSpec` tuple in column order.

        Raises:
            RuntimeError: If the lock has already been set.
            ValueError: If ``rows`` is empty — the first batch must
                carry at least one row so the schema can be inferred.
        """
        if self._columns is not None:
            raise RuntimeError("SchemaLock.lock called twice")
        if not rows:
            raise ValueError("SchemaLock.lock requires a non-empty first batch")

        ordered_keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    ordered_keys.append(key)
                    seen.add(key)

        specs = [self._infer_column(key, rows) for key in ordered_keys]
        self._columns = tuple(specs)
        self._names = frozenset(ordered_keys)
        return self._columns

    @staticmethod
    def _infer_column(
        key: str,
        rows: Sequence[Mapping[str, object]],
    ) -> ColumnSpec:
        """Infer one column's spec from the first batch."""
        inferred: type | None = None
        nullable = False
        for row in rows:
            if key not in row:
                nullable = True
                continue
            value = row[key]
            if value is None:
                nullable = True
                continue
            value_type = type(value)
            if inferred is None:
                inferred = value_type
            elif inferred is not value_type:
                inferred = float if {inferred, value_type} <= {int, float} else str
        if inferred is None:
            inferred = str
            nullable = True
        elif inferred not in (float, int, str):
            inferred = str
        return ColumnSpec(name=key, python_type=inferred, nullable=nullable)

    def lock_to(self, specs: Sequence[ColumnSpec]) -> tuple[ColumnSpec, ...]:
        """Lock the schema from an externally-supplied spec list.

        Used by sinks that validate against an already-existing
        backing schema (e.g. Postgres with ``create_table=False``)
        rather than inferring from the first batch.

        Args:
            specs: Columns in their canonical order.

        Returns:
            The stored spec tuple (same contents as ``specs``).

        Raises:
            RuntimeError: If the lock has already been set.
            ValueError: If ``specs`` is empty.
        """
        if self._columns is not None:
            raise RuntimeError("SchemaLock.lock_to called twice")
        if not specs:
            raise ValueError("SchemaLock.lock_to requires at least one column")
        self._columns = tuple(specs)
        self._names = frozenset(spec.name for spec in self._columns)
        return self._columns

    def project(self, row: Mapping[str, object]) -> dict[str, object]:
        """Return a new dict containing only keys from the locked schema.

        Every locked column appears in the output dict — missing keys
        are filled with ``None`` so sinks can rely on a stable per-row
        shape. Any key in ``row`` that is not part of the locked
        schema is dropped, and the first occurrence of each such key
        is logged at WARN level so users know the sink silently
        ignored it.

        Args:
            row: The row dict to project onto the locked schema.

        Returns:
            A fresh dict keyed by the locked column names, in the
            sink's preferred column order.

        Raises:
            RuntimeError: If the schema has not been locked yet.
        """
        if self._columns is None:
            raise RuntimeError("SchemaLock.project called before lock()")

        result: dict[str, object] = {spec.name: None for spec in self._columns}
        for key, value in row.items():
            if key in self._names:
                result[key] = value
                continue
            if key not in self._unknown_warned:
                self._unknown_warned.add(key)
                self._logger.warning(
                    "sink.unknown_column_dropped",
                    extra={"sink": self._sink_name, "column": key},
                )
        return result
