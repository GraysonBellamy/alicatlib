"""Session — the one object that dispatches commands.

A :class:`Session` owns a validated ``unit_id``, the device's
:class:`DeviceInfo`, and (optionally) its cached :class:`DataFrameFormat`.
It holds no I/O lock of its own — the shared
:class:`~alicatlib.protocol.client.AlicatProtocolClient` serialises
traffic at the port level, so every session pointed at the same client
naturally serialises on the same lock (correct for multi-unit RS-485
buses per design §5.7).

:meth:`Session.execute` is the single pre-I/O gating path:

1. Firmware family membership (``cmd.firmware_families``).
2. Firmware min/max within the matching family.
3. Device kind (``cmd.device_kinds``).
4. Medium compatibility (``cmd.media`` ∩ ``info.media``).
5. Required hardware capabilities (``cmd.required_capabilities``).
6. Destructive-confirm (``cmd.destructive`` + ``request.confirm``).

All six fail loudly (typed exceptions, ``ErrorContext`` populated) and
fail *before* any I/O — the library's "silence is unsafe" stance
(design §5.17).

Lifecycle-changing operations (``change_unit_id`` / ``change_baud_rate``
— design §5.7) use bounded cancellation shields to keep the device
and the client in sync across the write → verify → reconfigure
boundary. An unbounded shield would hang the process if the device
wedged; the bounded shield escalates to :attr:`SessionState.BROKEN`
instead, which is recoverable.

Design reference: ``docs/design.md`` §5.7, §5.10, §5.17, §5.20.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from enum import Enum
from time import monotonic_ns
from typing import TYPE_CHECKING, Any, Final, cast

import anyio

from alicatlib.commands.base import Capability, Command, DecodeContext, Medium, ResponseMode
from alicatlib.commands.polling import POLL_DATA, PollRequest
from alicatlib.commands.system import (
    DATA_FRAME_FORMAT_QUERY,
    VE_QUERY,
    DataFrameFormatRequest,
    VeRequest,
)
from alicatlib.config import AlicatConfig
from alicatlib.devices._eeprom_wear import EepromWearMonitor
from alicatlib.devices.data_frame import DataFrame
from alicatlib.errors import (
    AlicatConnectionError,
    AlicatError,
    AlicatFirmwareError,
    AlicatMediumMismatchError,
    AlicatMissingHardwareError,
    AlicatStreamingModeError,
    AlicatTimeoutError,
    AlicatUnsupportedCommandError,
    AlicatValidationError,
    ErrorContext,
    InvalidUnitIdError,
)
from alicatlib.firmware import FirmwareFamily
from alicatlib.protocol.framing import strip_eol

if TYPE_CHECKING:
    from alicatlib.devices.data_frame import DataFrameFormat, ParsedFrame
    from alicatlib.devices.models import DeviceInfo
    from alicatlib.firmware import FirmwareVersion
    from alicatlib.protocol.client import AlicatProtocolClient
    from alicatlib.registry.loop_control import LoopControlVariable

__all__ = [
    "SUPPORTED_BAUDRATES",
    "UNIT_ID_POLLING",
    "UNIT_ID_STREAMING",
    "Session",
    "SessionState",
    "validate_unit_id",
]

#: Budget (seconds) for the cancellation-shielded reconciliation phase
#: of :meth:`Session.change_unit_id`. Must cover a short grace window
#: plus a ``VE`` round-trip at the new unit id on a real bus; 2s is
#: comfortable on typical USB-to-RS485 adapters and leaves headroom for
#: slower hardware. If the device doesn't come back within the budget,
#: the session raises :class:`AlicatTimeoutError` and the cached
#: ``unit_id`` is *not* updated.
_CHANGE_UNIT_ID_SHIELD_S: Final[float] = 2.0

#: Budget (seconds) for the cancellation-shielded reconciliation phase
#: of :meth:`Session.change_baud_rate`. Larger than the unit-id budget
#: because a serial reopen on some USB-to-RS485 adapters takes up to a
#: second. Exceeding the budget transitions the session to
#: :attr:`SessionState.BROKEN` and raises :class:`AlicatConnectionError`.
_CHANGE_BAUD_RATE_SHIELD_S: Final[float] = 5.0

#: Post-write grace window before verifying :meth:`change_unit_id`.
#: The primer claims the device accepts the rename silently, but some
#: V1_V7 firmware (observed on 6v21 MCR-775SLPM-D, 2026-04-17) emits a
#: data-frame ack at the new unit id after processing the rename — at
#: 19200 baud that frame takes ~25 ms to transmit and the device may
#: take additional time to begin transmitting. 200 ms gives ample
#: margin for the data-frame ack to complete so the subsequent drain
#: clears it cleanly instead of mid-frame.
_RENAME_GRACE_S: Final[float] = 0.2


#: Baud rates the primer documents for ``NCB`` (design §5.20 pt 4 /
#: §5.4 argument-range validation). A request outside this set raises
#: :class:`AlicatValidationError` pre-I/O rather than writing a doomed
#: command.
SUPPORTED_BAUDRATES: Final[frozenset[int]] = frozenset(
    {2400, 9600, 19200, 38400, 57600, 115200},
)


class SessionState(Enum):
    """Lifecycle state of a :class:`Session`.

    ``OPERATIONAL`` is the normal state — commands dispatch freely.
    ``BROKEN`` is entered when an atomic lifecycle operation
    (``change_baud_rate``) cannot reconcile the transport with the
    device's new state. A ``BROKEN`` session rejects every subsequent
    :meth:`Session.execute` with :class:`AlicatConnectionError` and
    the caller must construct a fresh session (typically by
    re-running :func:`open_device`) to recover.
    """

    OPERATIONAL = "operational"
    BROKEN = "broken"


#: The 26 polling unit IDs — one-letter names A..Z addressable on an
#: RS-485 bus. These plus ``@`` (see :data:`UNIT_ID_STREAMING`) are the
#: only values the Alicat protocol accepts.
UNIT_ID_POLLING: Final[frozenset[str]] = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

#: The single streaming unit id — only valid inside a streaming session,
#: because streaming overwrites a device's normal unit id to ``@`` until
#: the stream is stopped (design §5.8).
UNIT_ID_STREAMING: Final[str] = "@"


def _medium_hint(command_media: Medium, device_media: Medium) -> str:
    """Remediation hint for :class:`AlicatMediumMismatchError`.

    Maps the (command, device) medium pair to a concrete API pointer so the
    exception message tells the caller *what to do next* — typically "use
    :meth:`Device.fluid` instead of :meth:`Device.gas`" or the reverse.
    Empty return when no specific hint applies (e.g. device media is
    :attr:`Medium.NONE` during identification). Design §5.9a.
    """
    gas_only = command_media == Medium.GAS
    liquid_only = command_media == Medium.LIQUID
    if gas_only and not (device_media & Medium.GAS):
        return "this device is liquid-only — use device.fluid(...) instead of device.gas(...)"
    if liquid_only and not (device_media & Medium.LIQUID):
        return "this device is gas-only — use device.gas(...) instead of device.fluid(...)"
    return ""


def validate_unit_id(unit_id: str, *, allow_streaming: bool = False) -> str:
    """Return ``unit_id`` if valid, otherwise raise :class:`InvalidUnitIdError`.

    A plain polling id (``"A"``..``"Z"``) is always valid. The streaming
    id ``"@"`` is accepted only when ``allow_streaming`` is True — callers
    that are building a normal :class:`Session` should not pass this
    flag, because a polling session on ``@`` can never talk to a device.
    """
    if unit_id in UNIT_ID_POLLING:
        return unit_id
    if allow_streaming and unit_id == UNIT_ID_STREAMING:
        return unit_id
    raise InvalidUnitIdError(
        f"invalid unit id {unit_id!r}: expected one of {sorted(UNIT_ID_POLLING)}"
        + (" or '@'" if allow_streaming else ""),
    )


class Session:
    """Single-device dispatch path.

    Constructor validates ``unit_id`` eagerly — an invalid id is
    :class:`InvalidUnitIdError` at construction, not at first use.

    The session does not own the :class:`AlicatProtocolClient`; the
    factory does. ``close()`` is a no-op placeholder; the
    factory's context-manager unwind is what drops the transport.
    """

    def __init__(
        self,
        client: AlicatProtocolClient,
        *,
        unit_id: str,
        info: DeviceInfo,
        data_frame_format: DataFrameFormat | None = None,
        port_label: str | None = None,
        config: AlicatConfig | None = None,
    ) -> None:
        self._client = client
        self._unit_id = validate_unit_id(unit_id)
        self._info = info
        self._data_frame_format = data_frame_format
        self._port_label = port_label
        self._closed = False
        cfg = config if config is not None else AlicatConfig()
        self._config = cfg
        self._eeprom_monitor = EepromWearMonitor(
            unit_id=self._unit_id,
            warn_per_minute=cfg.save_rate_warn_per_min,
        )
        # Setpoint-source cache ("S" / "A" / "U"), populated opportunistically
        # by the ``LSS`` command facade. The setpoint facade consults this
        # pre-I/O to short-circuit the "LSS=A silently ignores serial
        # setpoints" failure mode (design §5.20 risk table); when the cache
        # is ``None`` the check is skipped so the facade stays usable before
        # the first LSS round-trip.
        self._setpoint_source: str | None = None
        # Loop-control-variable cache. ``open_device`` pre-populates this
        # for controllers whose firmware supports ``LV`` so
        # :meth:`FlowController.setpoint` can pick the right
        # :class:`FullScaleValue` from :attr:`DeviceInfo.full_scale` for
        # pre-I/O range validation (design §5.20.2 — "setpoint
        # full-scale validation"). Every ``LV`` query / set
        # through the facade refreshes the cache so subsequent setpoint
        # writes validate against the current controlled variable. Stays
        # ``None`` on firmware / kinds that don't support ``LV`` — the
        # range check is skipped rather than failed in that case.
        self._loop_control_variable: LoopControlVariable | None = None
        # Lifecycle state. Transitions to ``BROKEN`` only on an
        # unreconcilable ``change_baud_rate`` failure (design §5.7). A
        # BROKEN session refuses further dispatch, surfacing the
        # situation instead of hiding it behind a silent timeout.
        self._state: SessionState = SessionState.OPERATIONAL

    # ---------------------------------------------------------------- properties

    @property
    def unit_id(self) -> str:
        """The validated single-letter unit id this session targets."""
        return self._unit_id

    @property
    def info(self) -> DeviceInfo:
        """Device identity snapshot — updated by :meth:`refresh_firmware`."""
        return self._info

    @property
    def data_frame_format(self) -> DataFrameFormat | None:
        """Cached :class:`DataFrameFormat`, or ``None`` before it's been probed."""
        return self._data_frame_format

    @property
    def firmware(self) -> FirmwareVersion:
        """Convenience accessor for :attr:`info.firmware`."""
        return self._info.firmware

    @property
    def port_label(self) -> str | None:
        """Human-readable port identifier, surfaced on every :class:`ErrorContext`."""
        return self._port_label

    @property
    def config(self) -> AlicatConfig:
        """The :class:`AlicatConfig` this session was constructed with.

        Used by facades that need to read runtime knobs (e.g. the
        EEPROM-wear threshold) without plumbing a separate config
        through every call site.
        """
        return self._config

    @property
    def state(self) -> SessionState:
        """Current lifecycle state.

        Transitions to :attr:`SessionState.BROKEN` only when
        :meth:`change_baud_rate` cannot reconcile the transport with
        the device's new baud. A BROKEN session refuses every
        subsequent :meth:`execute` with :class:`AlicatConnectionError`
        so callers recognise the situation instead of hitting silent
        timeouts.
        """
        return self._state

    @property
    def setpoint_source(self) -> str | None:
        """Cached setpoint source ("S" / "A" / "U"), or ``None`` if unprobed.

        Populated by :meth:`update_setpoint_source` after an ``LSS``
        query or set. The :meth:`FlowController.setpoint` facade reads
        this pre-I/O to detect the ``LSS=A`` failure mode — a serial
        setpoint write is silently ignored when the source is analog
        (design §5.20 risk table), so rather than let the write
        disappear the facade raises :class:`AlicatValidationError`.
        """
        return self._setpoint_source

    def update_setpoint_source(self, source: str) -> None:
        """Record ``source`` as the session's current setpoint source.

        Called by the ``LSS`` command facade (:meth:`FlowController.setpoint_source`)
        on every query / set so the cache tracks the device's state. Stays
        a plain setter rather than a ``@setpoint_source.setter`` to keep
        the mutation verb visible at call sites (``session.update_setpoint_source("S")``
        reads differently from an assignment).
        """
        self._setpoint_source = source

    @property
    def loop_control_variable(self) -> LoopControlVariable | None:
        """Cached loop-control variable, or ``None`` if unprobed / unsupported.

        :func:`~alicatlib.devices.factory.open_device` pre-populates this
        for controllers whose firmware supports ``LV``; the
        :meth:`FlowController.loop_control_variable` facade refreshes it
        on every query / set. Consumed by
        :meth:`FlowController.setpoint` to pick the right
        :class:`~alicatlib.devices.models.FullScaleValue` from
        :attr:`DeviceInfo.full_scale` for pre-I/O range validation.
        """
        return self._loop_control_variable

    def update_loop_control_variable(
        self,
        variable: LoopControlVariable,
    ) -> None:
        """Record ``variable`` as the session's current loop-control variable."""
        self._loop_control_variable = variable

    # ---------------------------------------------------------------- dispatch

    async def execute[Req, Resp](
        self,
        command: Command[Req, Resp],
        request: Req,
        *,
        timeout: float | None = None,
    ) -> Resp:
        """Dispatch ``command`` with pre-I/O gating and error enrichment.

        Gating order (cheapest first — every check is pre-I/O):
        firmware family → firmware min/max → device kind → capability →
        destructive-confirm. The first failed check raises; no later
        check runs.

        Any :class:`AlicatError` raised from the encode / I/O / decode
        path is re-raised with :attr:`ErrorContext.command_name` /
        ``unit_id`` / ``port`` / ``firmware`` / ``elapsed_s`` populated
        from this session — the pattern described in design §5.7.
        """
        self._check_state()
        self._check_streaming(command)
        self._check_firmware_family(command)
        self._check_firmware_range(command)
        self._check_device_kind(command)
        self._check_media(command)
        self._check_capabilities(command)
        self._check_destructive(command, request)

        # EEPROM-wear guard: WARN when per-device ``save=True`` rate
        # crosses the configured threshold (design §5.20.7). Cheap and
        # pre-I/O — commands without a ``save`` attribute on the request
        # short-circuit inside the monitor.
        self._eeprom_monitor.record(command, request)

        ctx = self._build_decode_context(command)
        wire_bytes = command.encode(ctx, request)

        started = monotonic_ns()
        try:
            return await self._dispatch(command, wire_bytes, ctx, timeout=timeout)
        except AlicatError as err:
            elapsed_s = (monotonic_ns() - started) / 1e9
            raise err.with_context(
                command_name=command.name,
                command_bytes=wire_bytes,
                unit_id=self._unit_id,
                port=self._port_label,
                firmware=self._info.firmware,
                device_kind=self._info.kind,
                device_media=self._info.media,
                command_media=command.media,
                elapsed_s=elapsed_s,
            ) from None

    async def _dispatch[Req, Resp](
        self,
        command: Command[Req, Resp],
        wire_bytes: bytes,
        ctx: DecodeContext,
        *,
        timeout: float | None,
    ) -> Resp:
        mode = command.response_mode
        if mode is ResponseMode.NONE:
            await self._client.write_only(wire_bytes, timeout=timeout)
            # NONE-mode commands carry Resp=None by convention; the decode
            # step is skipped so no-reply commands don't need to implement it.
            return cast("Resp", None)
        if mode is ResponseMode.LINE:
            raw = await self._client.query_line(wire_bytes, timeout=timeout)
            return command.decode(raw, ctx)
        if mode is ResponseMode.LINES:
            lines = await self._client.query_lines(
                wire_bytes,
                first_timeout=timeout,
                max_lines=command.expected_lines,
                is_complete=command.is_complete,
            )
            return command.decode(lines, ctx)
        if mode is ResponseMode.STREAM:
            # Streaming mode is a port-level state transition, not a
            # request/response command — it lives in
            # :class:`~alicatlib.devices.streaming.StreamingSession`,
            # not in ``Session._dispatch``. A command that declares
            # ``ResponseMode.STREAM`` is a spec bug; raise loudly.
            raise RuntimeError(
                f"{command.name} declares ResponseMode.STREAM but streaming is "
                "not a request/response mode — use Device.stream(...) instead",
            )
        raise RuntimeError(f"unhandled response_mode: {mode}")

    # ---------------------------------------------------------------- gating

    def _check_firmware_family(self, command: Command[Any, Any]) -> None:
        families = command.firmware_families
        if not families:
            return
        if self._info.firmware.family in families:
            return
        raise AlicatFirmwareError(
            command=command.name,
            reason="family_not_supported",
            actual=self._info.firmware,
            required_families=families,
            context=ErrorContext(command_name=command.name, unit_id=self._unit_id),
        )

    def _check_firmware_range(self, command: Command[Any, Any]) -> None:
        fw = self._info.firmware
        # Only compare within the same family — FirmwareVersion rejects
        # cross-family ordering with a TypeError. A command's min/max
        # specified in a family different from the device's is a spec
        # inconsistency; the firmware_families check above already
        # guards the intended case, so here we simply skip such mismatches.
        if (
            command.min_firmware is not None
            and command.min_firmware.family is fw.family
            and fw < command.min_firmware
        ):
            raise AlicatFirmwareError(
                command=command.name,
                reason="firmware_too_old",
                actual=fw,
                required_min=command.min_firmware,
                context=ErrorContext(command_name=command.name, unit_id=self._unit_id),
            )
        if (
            command.max_firmware is not None
            and command.max_firmware.family is fw.family
            and fw > command.max_firmware
        ):
            raise AlicatFirmwareError(
                command=command.name,
                reason="firmware_too_new",
                actual=fw,
                required_max=command.max_firmware,
                context=ErrorContext(command_name=command.name, unit_id=self._unit_id),
            )

    def _check_device_kind(self, command: Command[Any, Any]) -> None:
        kinds = command.device_kinds
        if not kinds or self._info.kind in kinds:
            return
        raise AlicatUnsupportedCommandError(
            f"{command.name} not supported on device kind {self._info.kind.value!r}",
            context=ErrorContext(
                command_name=command.name,
                unit_id=self._unit_id,
                extra={
                    "device_kind": self._info.kind.value,
                    "supported_kinds": sorted(k.value for k in kinds),
                },
            ),
        )

    def _check_media(self, command: Command[Any, Any]) -> None:
        device_media = self._info.media
        if command.media & device_media:
            return
        hint = _medium_hint(command.media, device_media)
        raise AlicatMediumMismatchError(
            command=command.name,
            device_media=device_media,
            command_media=command.media,
            hint=hint,
            context=ErrorContext(
                command_name=command.name,
                unit_id=self._unit_id,
                device_kind=self._info.kind,
                device_media=device_media,
                command_media=command.media,
            ),
        )

    def _check_capabilities(self, command: Command[Any, Any]) -> None:
        required = command.required_capabilities
        if required is Capability.NONE:
            return
        missing = required & ~self._info.capabilities
        if missing is Capability.NONE:
            return
        raise AlicatMissingHardwareError(
            f"{command.name} requires capability {missing.name or missing!r}; "
            f"device reports {self._info.capabilities.name or self._info.capabilities!r}",
            context=ErrorContext(
                command_name=command.name,
                unit_id=self._unit_id,
                extra={
                    "missing_capabilities": missing.name or str(missing),
                    "required_capabilities": required.name or str(required),
                    "device_capabilities": (
                        self._info.capabilities.name or str(self._info.capabilities)
                    ),
                },
            ),
        )

    def _check_destructive(
        self,
        command: Command[Any, Any],
        request: object,
    ) -> None:
        if not command.destructive:
            return
        confirmed = getattr(request, "confirm", False) is True
        if confirmed:
            return
        raise AlicatValidationError(
            f"{command.name} is destructive; pass confirm=True to execute",
            context=ErrorContext(
                command_name=command.name,
                unit_id=self._unit_id,
                extra={"destructive": True},
            ),
        )

    def _check_streaming(self, command: Command[Any, Any]) -> None:
        """Refuse dispatch while the shared client is in streaming mode.

        Design §5.8: one streamer per port, and request/response traffic
        on that port is unsafe while the device is pushing unsolicited
        frames. The :class:`~alicatlib.devices.streaming.StreamingSession`
        runtime sets the client's streaming latch on entry; we fail
        fast here rather than let a command land on a bus that is
        already flooded.
        """
        if not self._client.is_streaming:
            return
        raise AlicatStreamingModeError(
            f"{command.name} rejected: client is in streaming mode — "
            "exit the StreamingSession context (or send stop-stream) "
            "before issuing request/response commands.",
            context=ErrorContext(
                command_name=command.name,
                unit_id=self._unit_id,
                port=self._port_label,
                extra={"streaming": True},
            ),
        )

    def _check_state(self) -> None:
        """Refuse dispatch on a ``BROKEN`` session.

        The BROKEN state is entered by :meth:`change_baud_rate` when
        it cannot reconcile the transport with the device's new baud
        (design §5.7). Dispatch has no useful work to do in that
        state — the client and device disagree about the wire rate —
        so the session fails loudly with remediation guidance.
        """
        if self._state is SessionState.BROKEN:
            raise AlicatConnectionError(
                "session is in BROKEN state (typically after a failed "
                "change_baud_rate); construct a fresh session via "
                "open_device(...) at the device's new baud rate to recover.",
                context=ErrorContext(
                    unit_id=self._unit_id,
                    port=self._port_label,
                    extra={"session_state": self._state.value},
                ),
            )

    # ---------------------------------------------------------------- context

    def _build_decode_context(self, command: Command[Any, Any] | None = None) -> DecodeContext:
        # GP devices insert ``$$`` between unit id and token for most
        # commands (primer p. 4). Hardware validation on 2026-04-17 (§16.6.8)
        # surfaced that the ``??M*`` / ``??D*`` / ``??G*`` / poll reads
        # on a GP07R100 device reject the ``$$`` form and accept the
        # prefix-less form — so commands declare ``prefix_less=True``
        # and we skip the prefix on GP accordingly. Non-GP families
        # use an empty prefix unconditionally, so ``prefix_less`` is a
        # no-op there.
        prefix = b""
        if self._info.firmware.family is FirmwareFamily.GP and not (
            command is not None and command.prefix_less
        ):
            prefix = b"$$"
        return DecodeContext(
            unit_id=self._unit_id,
            firmware=self._info.firmware,
            capabilities=self._info.capabilities,
            command_prefix=prefix,
            data_frame_format=self._data_frame_format,
        )

    # ---------------------------------------------------------------- refresh

    async def refresh_firmware(self) -> FirmwareVersion:
        """Re-probe ``VE`` and update the cached :class:`FirmwareVersion`.

        Uses :data:`alicatlib.commands.system.VE_QUERY`; the session's
        cached :class:`DeviceInfo` is updated in place via
        :func:`dataclasses.replace` (the dataclass itself is frozen).
        """
        result = await self.execute(VE_QUERY, VeRequest())
        self._info = dataclasses.replace(
            self._info,
            firmware=result.firmware,
            firmware_date=result.firmware_date,
        )
        return result.firmware

    async def refresh_data_frame_format(self) -> DataFrameFormat:
        """Re-probe ``??D*`` and update the cached :class:`DataFrameFormat`."""
        fmt = await self.execute(DATA_FRAME_FORMAT_QUERY, DataFrameFormatRequest())
        self._data_frame_format = fmt
        return fmt

    def invalidate_data_frame_format(self) -> None:
        """Drop the cached :class:`DataFrameFormat` without re-probing.

        After a command that changes the device's data-frame shape
        (``DCU`` engineering-units set, ``FDF`` field reorder, …) the
        cached format is stale. Clearing it lets the next :meth:`poll`
        lazily re-probe via :meth:`refresh_data_frame_format` — one
        round-trip amortised over the next poll rather than immediately.
        Sync + non-awaitable by design so facade set-paths don't pay a
        second ``??D*`` at every call.
        """
        self._data_frame_format = None

    async def refresh_capabilities(self) -> Capability:
        """Re-probe the device's capability flags.

        Implementation lives in the factory (:mod:`alicatlib.devices.factory`),
        which owns the per-capability probe map
        (``FPF``/``VD``/``??D*``-derived flags). This method is reserved
        on the :class:`Session` surface for API stability; calling it now
        raises :class:`NotImplementedError` pointing at the right place.
        """
        raise NotImplementedError(
            "Session.refresh_capabilities is not yet implemented; "
            "construct the session from open_device(...) for now.",
        )

    # ---------------------------------------------------------------- poll convenience

    async def poll(self) -> DataFrame:
        """Convenience poll — execute ``POLL_DATA`` and wrap with read-site timing.

        This is the one place the session owns timing capture; per design
        §5.6 the :class:`DataFrame` is the session's job, not the
        command's. Callers that want the pure (clock-free)
        :class:`ParsedFrame` go through
        ``session.execute(POLL_DATA, PollRequest())`` instead.
        """
        fmt = self._data_frame_format
        if fmt is None:
            fmt = await self.refresh_data_frame_format()
        parsed: ParsedFrame = await self.execute(POLL_DATA, PollRequest())
        # Timing captured as close to the read site as the Session sees.
        # Exact-to-the-byte timing would require plumbing callbacks into
        # the protocol client; deferred until a real need surfaces (design §5.6).
        return DataFrame.from_parsed(
            parsed,
            format=fmt,
            received_at=datetime.now(UTC),
            monotonic_ns=monotonic_ns(),
        )

    # ---------------------------------------------------------------- lifecycle changes

    async def change_unit_id(
        self,
        new_unit_id: str,
        *,
        confirm: bool = False,
    ) -> None:
        r"""Rename the device this session talks to.

        Sends the primer's bus-level rename ``{old}@ {new}\r`` (no
        ``$$`` prefix on GP — this is a wire-level mode switch, not a
        normal command). The device does *not* ack on the wire; the
        session waits :data:`_RENAME_GRACE_S` and then verifies the
        rename with a ``VE`` at the new unit id.

        Argument rules (design §5.7, §5.20 pt 1):

        - ``confirm=True`` is required: a rename collision (two
          devices ending up on the same unit id) silently splits the
          bus, so the caller must opt in explicitly.
        - ``new_unit_id`` must be ``A``..``Z`` (the polling alphabet).
        - ``new_unit_id`` must differ from the current :attr:`unit_id`.

        Cancellation semantics: the rename write happens outside the
        shield (cancellation there leaves the device untouched). The
        post-write verify runs inside a
        :func:`anyio.move_on_after(timeout, shield=True)` of
        :data:`_CHANGE_UNIT_ID_SHIELD_S`. If the shield fires the
        device may or may not have accepted the rename — the session
        raises :class:`AlicatTimeoutError` and the cached unit id is
        *not* updated, leaving recovery to the caller.
        """
        if not confirm:
            raise AlicatValidationError(
                "change_unit_id is destructive; pass confirm=True to execute",
                context=ErrorContext(
                    command_name="change_unit_id",
                    unit_id=self._unit_id,
                    extra={"new_unit_id": new_unit_id},
                ),
            )
        self._check_state()
        validated = validate_unit_id(new_unit_id)
        if validated == self._unit_id:
            raise AlicatValidationError(
                f"change_unit_id: new_unit_id {validated!r} matches the "
                f"current unit id — pass a different A-Z letter",
                context=ErrorContext(
                    command_name="change_unit_id",
                    unit_id=self._unit_id,
                ),
            )

        old_unit_id = self._unit_id
        rename_bytes = f"{old_unit_id}@ {validated}\r".encode("ascii")

        async with self._client.lock:
            # The rename write is outside the shield so cancellation
            # pre-write leaves the device unchanged.
            await self._client.transport.write(
                rename_bytes,
                timeout=self._config.write_timeout_s,
            )

            with anyio.move_on_after(
                _CHANGE_UNIT_ID_SHIELD_S,
                shield=True,
            ) as scope:
                # Short grace window — the device accepts the rename
                # silently (no ack on the wire); giving it ~50 ms
                # before polling the new unit id avoids a race where
                # our VE lands mid-commit.
                await anyio.sleep(_RENAME_GRACE_S)
                await self._verify_unit_id_via_ve(validated)

            if scope.cancelled_caught:
                raise AlicatTimeoutError(
                    f"change_unit_id verification timed out after "
                    f"{_CHANGE_UNIT_ID_SHIELD_S:.1f}s; device state unknown — "
                    f"did not update cached unit id from {old_unit_id!r}",
                    context=ErrorContext(
                        command_name="change_unit_id",
                        unit_id=old_unit_id,
                        port=self._port_label,
                        firmware=self._info.firmware,
                        extra={
                            "attempted_unit_id": validated,
                            "shield_timeout_s": _CHANGE_UNIT_ID_SHIELD_S,
                        },
                    ),
                )

            # Verified — swap caches. ``DeviceInfo.unit_id`` also updates
            # so downstream errors carry the post-rename id.
            self._unit_id = validated
            self._info = dataclasses.replace(self._info, unit_id=validated)
            self._eeprom_monitor.unit_id = validated

    async def change_baud_rate(
        self,
        new_baud: int,
        *,
        confirm: bool = False,
    ) -> None:
        """Change the device's baud rate and retune the transport.

        Sends ``NCB <new_baud>`` at the current baud, reads the ack
        (still at the old baud), tells the transport to
        :meth:`~alicatlib.transport.base.Transport.reopen` at the new
        baud, then verifies with a ``VE`` round-trip. All four steps
        after the write happen inside a bounded
        :func:`anyio.move_on_after(_CHANGE_BAUD_RATE_SHIELD_S, shield=True)`.

        ``confirm=True`` is required — a failed baud change splits
        the adapter from the device until someone reopens the port.
        ``new_baud`` must be in :data:`SUPPORTED_BAUDRATES`.

        On any failure inside the shielded block (or the shield
        timing out) the session transitions to
        :attr:`SessionState.BROKEN` and raises
        :class:`AlicatConnectionError` with remediation guidance.
        Subsequent :meth:`execute` calls then fail fast instead of
        hanging.
        """
        if not confirm:
            raise AlicatValidationError(
                "change_baud_rate is destructive; pass confirm=True to execute",
                context=ErrorContext(
                    command_name="change_baud_rate",
                    unit_id=self._unit_id,
                    extra={"new_baud": new_baud},
                ),
            )
        self._check_state()
        if new_baud not in SUPPORTED_BAUDRATES:
            raise AlicatValidationError(
                f"change_baud_rate: new_baud {new_baud} not one of {sorted(SUPPORTED_BAUDRATES)}",
                context=ErrorContext(
                    command_name="change_baud_rate",
                    unit_id=self._unit_id,
                    extra={"new_baud": new_baud},
                ),
            )

        prefix = self._command_prefix_bytes().decode("ascii")
        ncb_bytes = f"{self._unit_id}{prefix}NCB {new_baud}\r".encode("ascii")

        async with self._client.lock:
            # Write NCB at the current baud before the shield; a pre-
            # write cancellation leaves the device unchanged.
            await self._client.transport.write(
                ncb_bytes,
                timeout=self._config.write_timeout_s,
            )

            with anyio.move_on_after(
                _CHANGE_BAUD_RATE_SHIELD_S,
                shield=True,
            ) as scope:
                try:
                    # Device acks at the old baud, then switches.
                    ack = await self._client.transport.read_until(
                        self._client.eol,
                        timeout=self._config.default_timeout_s,
                    )
                    self._client.guard_response(
                        strip_eol(ack, eol=self._client.eol),
                        command=ncb_bytes,
                    )
                    await self._client.transport.reopen(baudrate=new_baud)
                    await self._verify_unit_id_via_ve(self._unit_id)
                except AlicatError:
                    # A verifiable failure inside the shield — the
                    # device is on the new baud but our client may not
                    # be. Escalate to BROKEN with a clear error.
                    self._state = SessionState.BROKEN
                    raise AlicatConnectionError(
                        f"change_baud_rate to {new_baud} failed mid-sequence; "
                        "session is BROKEN. Close this session and re-open "
                        f"via open_device(...) at baudrate={new_baud} to "
                        "recover.",
                        context=ErrorContext(
                            command_name="change_baud_rate",
                            unit_id=self._unit_id,
                            port=self._port_label,
                            firmware=self._info.firmware,
                            extra={
                                "new_baud": new_baud,
                                "session_state": self._state.value,
                            },
                        ),
                    ) from None

            if scope.cancelled_caught:
                self._state = SessionState.BROKEN
                raise AlicatConnectionError(
                    f"change_baud_rate to {new_baud} wedged after "
                    f"{_CHANGE_BAUD_RATE_SHIELD_S:.1f}s; session is BROKEN. "
                    "Close this session and re-open via open_device(...) at "
                    f"baudrate={new_baud} to recover.",
                    context=ErrorContext(
                        command_name="change_baud_rate",
                        unit_id=self._unit_id,
                        port=self._port_label,
                        firmware=self._info.firmware,
                        extra={
                            "new_baud": new_baud,
                            "session_state": self._state.value,
                            "shield_timeout_s": _CHANGE_BAUD_RATE_SHIELD_S,
                        },
                    ),
                )

    async def _verify_unit_id_via_ve(self, expected_unit_id: str) -> None:
        r"""Issue ``VE`` at ``expected_unit_id`` and confirm the echo.

        Shared by :meth:`change_unit_id` and :meth:`change_baud_rate` —
        both verify success by checking the device responds to ``VE``
        at the target unit id (and at the target baud, for baud
        changes, since the transport is already reopened at the new
        baud by then).

        Raises :class:`AlicatTimeoutError` if the device does not
        respond. The caller's cancellation shield catches this and
        surfaces it as the appropriate lifecycle failure.

        The buffer is drained before writing ``VE`` because a rename
        round-trip can leave stale bytes in the input buffer (hardware
        day 2026-04-17, §16.6.7): the device emits a data-frame ack at
        the new unit id in response to ``<old>@ <new>\r``, and a
        preceding verify's VE reply may still be sitting behind that
        ack. Without the drain, a second change_unit_id call reads the
        previous call's VE reply instead of the current one — the
        first token matches the previous target, not the current one,
        producing a false-negative verification failure.

        GP firmware does not implement ``VE`` at all (design §16.6.8);
        use the prefix-less poll (``<uid>\r``) as the verify probe
        instead. The poll always works on GP and the first token is
        still the unit id, so the shape-check that follows is unchanged.
        """
        await self._client.transport.drain_input()
        prefix = self._command_prefix_bytes().decode("ascii")
        is_gp = self._info.firmware.family is FirmwareFamily.GP
        verify_bytes = (
            f"{expected_unit_id}\r".encode("ascii")
            if is_gp
            else f"{expected_unit_id}{prefix}VE\r".encode("ascii")
        )
        await self._client.transport.write(
            verify_bytes,
            timeout=self._config.write_timeout_s,
        )
        raw = await self._client.transport.read_until(
            self._client.eol,
            timeout=self._config.default_timeout_s,
        )
        stripped = strip_eol(raw, eol=self._client.eol)
        self._client.guard_response(stripped, command=verify_bytes)
        # Best-effort unit-id match — if the first whitespace-separated
        # token isn't ``expected_unit_id``, the bus returned someone
        # else's reply (or the device didn't rename). Surface as a
        # timeout so the shield treats this the same as "no response".
        first_token = stripped.split(None, 1)[0].decode("ascii", errors="replace")
        if first_token != expected_unit_id:
            raise AlicatTimeoutError(
                f"rename verify got unit id {first_token!r}, expected "
                f"{expected_unit_id!r}; device did not accept the rename",
                context=ErrorContext(
                    command_name="change_unit_id",
                    unit_id=self._unit_id,
                    port=self._port_label,
                    extra={
                        "expected_unit_id": expected_unit_id,
                        "received_unit_id": first_token,
                    },
                ),
            )

    def _command_prefix_bytes(self) -> bytes:
        """``b"$$"`` for GP firmware, ``b""`` otherwise.

        Factored out so lifecycle methods (``VE`` verify, ``NCB``
        encode) pick up the GP prefix without rebuilding a full
        :class:`DecodeContext`. Mirrors
        :meth:`_build_decode_context`'s prefix logic.
        """
        return b"$$" if self._info.firmware.family is FirmwareFamily.GP else b""

    # ---------------------------------------------------------------- lifecycle

    async def close(self) -> None:
        """Mark the session closed. No transport ownership → no I/O teardown."""
        self._closed = True

    @property
    def closed(self) -> bool:
        """``True`` once :meth:`close` has been called."""
        return self._closed
