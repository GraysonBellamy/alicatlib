"""Typed error hierarchy for :mod:`alicatlib`.

Every exception raised by the library is a subclass of :class:`AlicatError`
and carries a structured :class:`ErrorContext`. The context is deliberately a
typed dataclass (not ``**kwargs``) so IDEs and ``mypy --strict`` can reason
about it, and so rendering is consistent across tracebacks.

Design reference: ``docs/design.md`` §5.17.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from alicatlib.devices.kind import DeviceKind
    from alicatlib.devices.medium import Medium
    from alicatlib.firmware import FirmwareFamily, FirmwareVersion

__all__ = [
    "AlicatCapabilityError",
    "AlicatCommandRejectedError",
    "AlicatConfigurationError",
    "AlicatConnectionError",
    "AlicatDiscoveryError",
    "AlicatError",
    "AlicatFirmwareError",
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
    "ErrorContext",
    "InvalidUnitIdError",
    "UnknownFluidError",
    "UnknownGasError",
    "UnknownStatisticError",
    "UnknownUnitError",
]


def _empty_extra() -> dict[str, Any]:
    return {}


@dataclass(frozen=True, slots=True)
class ErrorContext:
    """Structured context attached to every :class:`AlicatError`.

    Every field is optional so callers can build a context progressively as a
    command flows through layers (transport → protocol → session → command).
    """

    command_name: str | None = None
    command_bytes: bytes | None = None
    raw_response: bytes | None = None
    unit_id: str | None = None
    port: str | None = None
    firmware: FirmwareVersion | None = None
    device_kind: DeviceKind | None = None
    device_media: Medium | None = None
    command_media: Medium | None = None
    elapsed_s: float | None = None
    extra: dict[str, Any] = field(default_factory=_empty_extra)

    def merged(self, **updates: Any) -> Self:
        """Return a new context with ``updates`` overlaid. Unknown keys go to ``extra``."""
        known: dict[str, Any] = {}
        extra_updates: dict[str, Any] = {}
        for key, value in updates.items():
            if key in {
                "command_name",
                "command_bytes",
                "raw_response",
                "unit_id",
                "port",
                "firmware",
                "device_kind",
                "device_media",
                "command_media",
                "elapsed_s",
            }:
                known[key] = value
            else:
                extra_updates[key] = value

        new_extra = {**self.extra, **extra_updates} if extra_updates else self.extra
        return replace(self, **known, extra=new_extra)


_EMPTY_CONTEXT = ErrorContext()


class AlicatError(Exception):
    """Base class for every exception raised by :mod:`alicatlib`.

    Carries a typed :class:`ErrorContext`. The ``message`` is the human-readable
    summary; the context is the machine-readable detail.
    """

    context: ErrorContext

    def __init__(self, message: str = "", *, context: ErrorContext | None = None) -> None:
        super().__init__(message)
        self.context = context if context is not None else _EMPTY_CONTEXT

    def with_context(self, **updates: Any) -> Self:
        """Return a copy of this error with its context updated.

        Useful when an inner layer raises and an outer layer wants to enrich
        the context (for instance adding ``port`` or ``elapsed_s``).
        """
        new = type(self)(str(self), context=self.context.merged(**updates))
        new.__cause__ = self.__cause__
        new.__context__ = self.__context__
        new.__traceback__ = self.__traceback__
        return new

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        ctx = self.context
        bits: list[str] = []
        if ctx.command_name is not None:
            bits.append(f"command={ctx.command_name}")
        if ctx.unit_id is not None:
            bits.append(f"unit_id={ctx.unit_id}")
        if ctx.port is not None:
            bits.append(f"port={ctx.port}")
        if ctx.elapsed_s is not None:
            bits.append(f"elapsed_s={ctx.elapsed_s:.3f}")
        return f"{base} [{', '.join(bits)}]" if bits else base


# ---------------------------------------------------------------------------
# Configuration / user-input errors
# ---------------------------------------------------------------------------


class AlicatConfigurationError(AlicatError):
    """User-supplied configuration was invalid."""


class UnknownGasError(AlicatConfigurationError):
    """A gas name or code did not resolve against the registry."""

    def __init__(
        self,
        value: str | int,
        *,
        suggestions: tuple[str, ...] = (),
        context: ErrorContext | None = None,
    ) -> None:
        self.value = value
        self.suggestions = suggestions
        hint = f" (did you mean: {', '.join(suggestions)}?)" if suggestions else ""
        super().__init__(f"Unknown gas: {value!r}{hint}", context=context)


class UnknownFluidError(AlicatConfigurationError):
    """A fluid (working-liquid) name or code did not resolve against the registry."""

    def __init__(
        self,
        value: str | int,
        *,
        suggestions: tuple[str, ...] = (),
        context: ErrorContext | None = None,
    ) -> None:
        self.value = value
        self.suggestions = suggestions
        hint = f" (did you mean: {', '.join(suggestions)}?)" if suggestions else ""
        super().__init__(f"Unknown fluid: {value!r}{hint}", context=context)


class UnknownUnitError(AlicatConfigurationError):
    """A unit name or code did not resolve against the registry."""

    def __init__(
        self,
        value: str | int,
        *,
        suggestions: tuple[str, ...] = (),
        context: ErrorContext | None = None,
    ) -> None:
        self.value = value
        self.suggestions = suggestions
        hint = f" (did you mean: {', '.join(suggestions)}?)" if suggestions else ""
        super().__init__(f"Unknown unit: {value!r}{hint}", context=context)


class UnknownStatisticError(AlicatConfigurationError):
    """A statistic name or code did not resolve against the registry."""

    def __init__(
        self,
        value: str | int,
        *,
        suggestions: tuple[str, ...] = (),
        context: ErrorContext | None = None,
    ) -> None:
        self.value = value
        self.suggestions = suggestions
        hint = f" (did you mean: {', '.join(suggestions)}?)" if suggestions else ""
        super().__init__(f"Unknown statistic: {value!r}{hint}", context=context)


class InvalidUnitIdError(AlicatConfigurationError):
    """A unit ID was not a single letter ``A`` — ``Z``."""


class AlicatValidationError(AlicatConfigurationError):
    """Arguments failed validation before any I/O (range checks, missing ``confirm``)."""


class AlicatMediumMismatchError(AlicatConfigurationError):
    """A command's declared medium doesn't intersect the device's configured medium.

    Raised pre-I/O from :class:`alicatlib.devices.session.Session` at the
    media gate (design §5.4, §5.9a). The typical shape: calling
    :meth:`Device.gas` on a liquid-only device, or
    :meth:`Device.fluid` on a gas-only device. The error carries the
    mismatch in :attr:`ErrorContext.device_media` and
    :attr:`ErrorContext.command_media` and points at the remediation
    API in its message.
    """

    def __init__(
        self,
        *,
        command: str,
        device_media: Medium,
        command_media: Medium,
        hint: str | None = None,
        context: ErrorContext | None = None,
    ) -> None:
        self.command = command
        self.device_media = device_media
        self.command_media = command_media
        suffix = f" — {hint}" if hint else ""
        super().__init__(
            (
                f"{command} requires medium {command_media.name or command_media!r} but "
                f"device is configured as {device_media.name or device_media!r}{suffix}"
            ),
            context=context,
        )


# ---------------------------------------------------------------------------
# Transport errors
# ---------------------------------------------------------------------------


class AlicatTransportError(AlicatError):
    """Serial/TCP transport failed to move bytes."""


class AlicatTimeoutError(AlicatTransportError):
    """An I/O timeout expired.

    A timeout is never represented as an empty successful response.
    """


class AlicatConnectionError(AlicatTransportError):
    """Connection could not be established or was lost."""


# ---------------------------------------------------------------------------
# Protocol errors
# ---------------------------------------------------------------------------


class AlicatProtocolError(AlicatError):
    """The bytes arrived but did not parse as a valid Alicat response."""


class AlicatParseError(AlicatProtocolError):
    """A response could not be parsed into its typed model."""

    def __init__(
        self,
        message: str,
        *,
        field_name: str | None = None,
        expected: object = None,
        actual: object = None,
        context: ErrorContext | None = None,
    ) -> None:
        self.field_name = field_name
        self.expected = expected
        self.actual = actual
        super().__init__(message, context=context)


class AlicatCommandRejectedError(AlicatProtocolError):
    """The device replied with its error marker (``?`` / similar)."""


class AlicatStreamingModeError(AlicatProtocolError):
    """A request/response command was attempted while the client was in streaming mode."""


class AlicatUnitIdMismatchError(AlicatProtocolError):
    """The response's unit ID did not match the request's."""


# ---------------------------------------------------------------------------
# Capability errors
# ---------------------------------------------------------------------------


class AlicatCapabilityError(AlicatError):
    """The device cannot perform the requested command."""


class AlicatUnsupportedCommandError(AlicatCapabilityError):
    """The command is not supported on this device kind."""


class AlicatMissingHardwareError(AlicatCapabilityError):
    """The device lacks hardware the command requires.

    Raised from :class:`alicatlib.devices.session.Session` *before* any
    I/O, using the :class:`alicatlib.commands.base.Capability` bits declared
    on the :class:`alicatlib.commands.base.Command` spec. More useful than
    letting the device silently respond ``?`` — tells the caller exactly
    which capability is missing (``BAROMETER``, ``MULTI_VALVE``,
    ``ANALOG_INPUT``, ...). See design §5.17.
    """


class AlicatFirmwareError(AlicatCapabilityError):
    """The device's firmware version is outside the command's supported range."""

    def __init__(
        self,
        *,
        command: str,
        reason: str,
        actual: FirmwareVersion | None = None,
        required_min: FirmwareVersion | None = None,
        required_max: FirmwareVersion | None = None,
        required_families: frozenset[FirmwareFamily] | None = None,
        context: ErrorContext | None = None,
    ) -> None:
        self.command = command
        self.reason = reason
        self.actual = actual
        self.required_min = required_min
        self.required_max = required_max
        self.required_families = required_families
        required = ""
        if required_min is not None or required_max is not None:
            lo = str(required_min) if required_min is not None else "*"
            hi = str(required_max) if required_max is not None else "*"
            required = f" (requires {lo}..{hi}, have {actual})"
        elif required_families:
            fams = ", ".join(f.value for f in sorted(required_families, key=lambda x: x.value))
            required = f" (requires family in {{{fams}}}, have {actual})"
        super().__init__(
            f"Firmware check failed for {command}: {reason}{required}",
            context=context,
        )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class AlicatDiscoveryError(AlicatError):
    """Device discovery failed."""


# ---------------------------------------------------------------------------
# Sinks (design §5.15)
# ---------------------------------------------------------------------------


class AlicatSinkError(AlicatError):
    """Base class for errors raised by sinks (CSV, JSONL, SQLite, Parquet, Postgres)."""


class AlicatSinkDependencyError(AlicatSinkError, AlicatConfigurationError):
    """A sink's optional backing library is not installed.

    Raised when the user instantiates (or calls ``open()`` on) a sink
    whose extras have not been installed — e.g. ``ParquetSink`` without
    ``alicatlib[parquet]`` or ``PostgresSink`` without
    ``alicatlib[postgres]``. The message always names the exact extra
    to install so the remediation is copy-pasteable.

    Multi-inherits :class:`AlicatConfigurationError` because callers
    that already branch on configuration errors (missing extras being
    a configuration problem from their perspective) keep working
    without changes.
    """


class AlicatSinkSchemaError(AlicatSinkError):
    """A batch's shape is incompatible with the sink's locked schema.

    Raised when a sink has locked its schema on the first batch (or
    validated against an existing table) and a subsequent batch
    carries rows whose shape can't be reconciled — for example, a
    Postgres target table that's missing a required column, or a
    Parquet writer that would need a type change mid-file.

    Dropping unknown *optional* columns is handled by a per-sink WARN
    log and does not raise.
    """


class AlicatSinkWriteError(AlicatSinkError):
    """The backing store rejected a write.

    Wraps the underlying driver exception (sqlite3, asyncpg, pyarrow)
    so downstream error handlers don't need to import optional
    dependencies. The original exception is preserved via
    ``raise ... from original`` so tracebacks remain intact.
    """
