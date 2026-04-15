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
    from alicatlib.firmware import FirmwareVersion


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
    elapsed_s: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

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
        context: ErrorContext | None = None,
    ) -> None:
        self.command = command
        self.reason = reason
        self.actual = actual
        self.required_min = required_min
        self.required_max = required_max
        required = ""
        if required_min is not None or required_max is not None:
            lo = str(required_min) if required_min is not None else "*"
            hi = str(required_max) if required_max is not None else "*"
            required = f" (requires {lo}..{hi}, have {actual})"
        super().__init__(
            f"Firmware check failed for {command}: {reason}{required}",
            context=context,
        )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class AlicatDiscoveryError(AlicatError):
    """Device discovery failed."""
