"""Command spec base — ``ResponseMode``, ``Capability``, ``DecodeContext``, ``Command``.

Every Alicat command is one :class:`Command` subclass. The base class is a
frozen dataclass carrying metadata (name, token, firmware gating, multiline
termination contract, destructive/experimental flags); concrete commands
subclass it and override :meth:`Command.encode` / :meth:`Command.decode`.

The command spec is a pure value — all I/O lives in the :class:`Session`
that calls ``encode`` / ``decode`` around a
:class:`~alicatlib.protocol.client.AlicatProtocolClient`. That keeps the
command layer testable without a transport.

Design reference: ``docs/design.md`` §5.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, Flag, auto
from typing import TYPE_CHECKING

from alicatlib.devices.medium import Medium

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from alicatlib.devices.data_frame import DataFrameFormat
    from alicatlib.devices.kind import DeviceKind
    from alicatlib.firmware import FirmwareFamily, FirmwareVersion

__all__ = [
    "Capability",
    "Command",
    "DecodeContext",
    "Medium",
    "ResponseMode",
]

#: Default ``media`` value for every :class:`Command` — medium-agnostic.
#: Commands that must gate by medium (gas select, fluid select, gas-mix
#: editing) override this with :attr:`Medium.GAS` or :attr:`Medium.LIQUID`
#: explicitly. Keeping the default wide means adding a new medium-neutral
#: command never requires thinking about the gate; adding a medium-specific
#: one is one line.
_DEFAULT_COMMAND_MEDIA: Medium = Medium.GAS | Medium.LIQUID


class ResponseMode(Enum):
    """What the transport should do for a command after writing.

    The :class:`~alicatlib.devices.session.Session` uses this to pick
    ``write_only`` / ``query_line`` / ``query_lines`` without per-command
    branching in the session code.
    """

    NONE = "none"
    """Write-only; no read. Example: ``@@`` stop-stream."""

    LINE = "line"
    """Single-line response terminated by CR. The common case."""

    LINES = "lines"
    """Multiline table response. See :attr:`Command.expected_lines` /
    :attr:`Command.is_complete` for the termination contract."""

    STREAM = "stream"
    """Enters streaming mode. Not a normal request/response command."""


class Capability(Flag):
    """Device-level feature flags, discovered per-device at session startup.

    :class:`~alicatlib.devices.base.DeviceKind` is too coarse to gate many
    commands (a flow meter may or may not have a barometer; only some
    devices have a secondary pressure sensor or remote-tare pin). These
    capabilities are orthogonal to ``DeviceKind`` and are declared per
    command via :attr:`Command.required_capabilities`.

    See design §5.4 for the full rationale.
    """

    NONE = 0
    BAROMETER = auto()
    """Device reports a barometric pressure reading (``FPF 15`` returns
    a plausible value with a real unit label). Probed via ``FPF 15``.

    Does NOT imply :attr:`TAREABLE_ABSOLUTE_PRESSURE`. Hardware validation
    on 2026-04-17 established that flow-controller devices (MCR, MCP, …)
    expose a firmware-computed barometer reading used internally for
    abs/gauge pressure derivation, but do not have a process-port
    absolute-pressure sensor that the ``PC`` command can re-zero. Four
    devices (8v17 MCR-200, 8v30 MCR-500, 6v21 MCR-775, 7v09 MCP-50)
    all probed ``BAROMETER`` positive via ``FPF 15`` yet rejected or
    silently ignored ``PC``. See design §16.6.7 for the narrative.
    """
    TAREABLE_ABSOLUTE_PRESSURE = auto()
    """Device has a process-port absolute-pressure sensor whose zero
    can be re-referenced against the current barometric reading via
    ``PC`` (``tare_absolute_pressure``). Gated separately from
    :attr:`BAROMETER` because the two properties dissociate in practice
    (see the ``BAROMETER`` docstring).

    No safe probe exists — test-writing ``PC`` would tare the device.
    Users opt in via ``assume_capabilities=Capability.TAREABLE_ABSOLUTE_PRESSURE``
    on :func:`~alicatlib.devices.factory.open_device` when they know
    their hardware supports it (typically pressure meters/controllers).
    """
    SECONDARY_PRESSURE = auto()
    ANALOG_INPUT = auto()
    ANALOG_OUTPUT = auto()
    SECONDARY_ANALOG_OUTPUT = auto()
    REMOTE_TARE_PIN = auto()
    MULTI_VALVE = auto()
    THIRD_VALVE = auto()
    BIDIRECTIONAL = auto()
    TOTALIZER = auto()
    DISPLAY = auto()


@dataclass(frozen=True, slots=True)
class DecodeContext:
    """Per-call context threaded through encode / decode.

    Built once per command by the session from cached device info, so
    commands never do I/O to figure out the prefix / firmware themselves.

    Attributes:
        unit_id: Single-letter ``A``–``Z`` identifying the device on the bus.
        firmware: Parsed device firmware; used by commands that alter their
            wire format across firmware families (e.g. legacy setpoint
            `S` vs modern `LS`).
        capabilities: Feature flags discovered at session startup. The
            session pre-checks ``Command.required_capabilities`` against
            this, so encode-time commands generally don't re-check.
        command_prefix: Bytes injected between ``unit_id`` and the
            command token. Empty for numeric-family firmware; ``b"$$"`` for
            GP-family devices (per primer p. 4 and design §5.10).
        data_frame_format: The cached ``??D*`` format. ``None`` before the
            session has probed it; commands that decode a data frame
            (``POLL_DATA``, any command returning a post-op state frame)
            raise :class:`AlicatParseError` if they're asked to decode
            with this still ``None``.
    """

    unit_id: str
    firmware: FirmwareVersion
    capabilities: Capability = Capability.NONE
    command_prefix: bytes = b""
    data_frame_format: DataFrameFormat | None = None


@dataclass(frozen=True, slots=True)
class Command[Req, Resp]:
    """Declarative spec for an Alicat command.

    Subclasses are frozen dataclass instances. The overridden
    :meth:`encode` / :meth:`decode` methods are the command's wire format.
    Everything else — firmware gating, capability gating, destructive /
    experimental flags, multiline termination — is metadata that the
    session reads *before* dispatching, so commands fail fast with typed
    errors rather than silently producing a bad wire payload.

    Attributes:
        name: Canonical Python-friendly name (e.g. ``"gas_select"``). Used
            in error messages and telemetry.
        token: Protocol token (e.g. ``"GS"``). Emitted verbatim — Alicat
            commands are case-insensitive *except* ``FACTORY RESTORE ALL``,
            which must be uppercase; set :attr:`case_sensitive` on that.
        response_mode: How the session should dispatch the I/O.
        device_kinds: Which :class:`DeviceKind` values this command applies
            to. Empty means "any".
        media: Which :class:`Medium` flag(s) this command applies to.
            Default (``Medium.GAS | Medium.LIQUID``) is medium-agnostic
            and lets the command run on every device. Gas-specific
            commands (``GS``, ``??G*``, gas-mix edits) narrow to
            :attr:`Medium.GAS`; liquid-specific commands narrow to
            :attr:`Medium.LIQUID`. Gated pre-I/O in
            :class:`~alicatlib.devices.session.Session` — a bitwise
            intersection of command ``media`` against the device's
            configured ``media`` determines whether dispatch proceeds
            (design §5.9a).
        required_capabilities: Capability bits the device must have. A
            command run on a device missing one raises
            :class:`~alicatlib.errors.AlicatMissingHardwareError`.
        min_firmware / max_firmware: Supported firmware range within a
            family. Cross-family comparison raises ``TypeError`` by design
            (see :class:`alicatlib.firmware.FirmwareVersion`).
        firmware_families: Which families support this command —
            **monotonic** gate: declare a family only when every
            captured device in that family either implements the
            command or is documented to. Empty means "any". For
            commands whose availability varies per-device within a
            family (``??G*`` works on 5v12 + 7v09 but rejects on 6v21;
            ``FPF`` rejects on 5v12 but works on 6v+), use a
            conservative superset (include families where *some*
            devices work) and let the runtime rejection path handle
            per-device variation. The empirical matrix
            (``tests/fixtures/device_matrix.yaml``) and its validator
            (``tests/unit/test_device_matrix.py``) enforce this
            policy end-to-end.
        destructive: Requires explicit ``confirm=True`` at the session
            layer.
        experimental: Emits a deprecation-style warning on use.
        case_sensitive: Suppress any hypothetical upstream lowercase
            normalisation. Only needed for ``FACTORY RESTORE ALL``.
        prefix_less: Command opts out of the unit-id prefix
            (e.g. ``@@`` stop-stream).
        expected_lines: Fixed row count for ``LINES`` commands (e.g.
            ``??M*`` is 10 lines).
        is_complete: Predicate that returns ``True`` when a multiline
            response is complete. Checked before ``expected_lines``, so
            takes priority (see design §5.2).

    A :class:`Command` must declare at least one of ``expected_lines``,
    ``is_complete`` for every :attr:`ResponseMode.LINES` command — otherwise
    reads fall through to the idle-timeout fallback every time, adding
    roughly 100 ms of latency per invocation. A test pins this
    invariant.
    """

    name: str
    token: str
    response_mode: ResponseMode
    device_kinds: frozenset[DeviceKind]
    media: Medium = _DEFAULT_COMMAND_MEDIA
    required_capabilities: Capability = Capability.NONE
    min_firmware: FirmwareVersion | None = None
    max_firmware: FirmwareVersion | None = None
    firmware_families: frozenset[FirmwareFamily] = frozenset()
    destructive: bool = False
    experimental: bool = False
    case_sensitive: bool = False
    prefix_less: bool = False
    expected_lines: int | None = None
    is_complete: Callable[[Sequence[bytes]], bool] | None = None

    def encode(self, ctx: DecodeContext, request: Req) -> bytes:
        """Render ``request`` as the exact bytes to put on the wire (incl. EOL)."""
        raise NotImplementedError(f"{self.name}.encode is not implemented")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> Resp:
        """Parse the device's (EOL-stripped) response into the typed result."""
        raise NotImplementedError(f"{self.name}.decode is not implemented")
