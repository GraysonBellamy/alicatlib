r"""Loop-control variable command — primer §Loop Control Variable (9v00+).

:data:`LOOP_CONTROL_VARIABLE` (``LV``) gets or sets the statistic a
controller's feedback loop tracks. The valid subset is modelled by
:class:`~alicatlib.registry.LoopControlVariable` —
the encoder uses
:func:`~alicatlib.registry.coerce_loop_control_variable` so invalid
choices fail at the facade layer with :class:`AlicatValidationError`
rather than getting rejected by the device after I/O.

Firmware gating: ``9v00+`` within V8_V9, all V10. Older families
have no loop-control command (the controlled variable is determined
by device configuration at manufacturing time). The session's
firmware-family gate raises :class:`AlicatFirmwareError` for
unsupported families.

Wire shape:

- Query: ``<uid><prefix>LV\r``
- Set:   ``<uid><prefix>LV <stat_code>\r``

Response (primer-derived, hardware-correctable per §16.4):
``<uid> <stat_code> <label>`` — 3 fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from alicatlib.commands._firmware_cutoffs import MIN_FIRMWARE_SETPOINT_LS
from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import LoopControlState
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_fields, parse_int
from alicatlib.registry import LoopControlVariable, coerce_loop_control_variable

__all__ = [
    "LOOP_CONTROL_VARIABLE",
    "LoopControlVariableCommand",
    "LoopControlVariableRequest",
]


# ``LV`` and ``LS`` share the same 9v00 cut-in inside V8_V9 — both
# were introduced in the same firmware revision. Reuse the shared
# constant from the firmware-cutoffs module rather than duplicate.
_MIN_FIRMWARE_LV: Final[FirmwareVersion] = MIN_FIRMWARE_SETPOINT_LS


_CONTROLLER_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(
    {DeviceKind.FLOW_CONTROLLER, DeviceKind.PRESSURE_CONTROLLER},
)


@dataclass(frozen=True, slots=True)
class LoopControlVariableRequest:
    """Arguments for :data:`LOOP_CONTROL_VARIABLE`.

    Attributes:
        variable: One of the eight LV-eligible statistics, accepted as
            :class:`LoopControlVariable` / :class:`Statistic` / ``int``
            code / name string. ``None`` issues the query form.
    """

    variable: LoopControlVariable | str | int | None = None


@dataclass(frozen=True, slots=True)
class LoopControlVariableCommand(
    Command[LoopControlVariableRequest, LoopControlState],
):
    r"""``LV`` — loop-control variable get/set.

    Firmware-gated at ``9v00`` within :attr:`FirmwareFamily.V8_V9`
    and available on every V10 release. Set form validates the
    ``variable`` against the restricted eight-member subset via
    :func:`coerce_loop_control_variable`; an ineligible
    :class:`Statistic` raises :class:`AlicatValidationError` pre-I/O
    rather than letting the device reject silently.
    """

    name: str = "loop_control_variable"
    token: str = "LV"  # noqa: S105 — protocol token, not a password
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _CONTROLLER_DEVICE_KINDS
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_LV
    firmware_families: frozenset[FirmwareFamily] = frozenset(
        {FirmwareFamily.V8_V9, FirmwareFamily.V10},
    )

    def encode(
        self,
        ctx: DecodeContext,
        request: LoopControlVariableRequest,
    ) -> bytes:
        """Emit the LV query or set bytes."""
        prefix = ctx.command_prefix.decode("ascii")
        if request.variable is None:
            return f"{ctx.unit_id}{prefix}{self.token}\r".encode("ascii")
        lv = coerce_loop_control_variable(request.variable)
        return f"{ctx.unit_id}{prefix}{self.token} {int(lv)}\r".encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> LoopControlState:
        """Parse ``<uid> <stat_code>`` into :class:`LoopControlState`.

        Verified against a V10 capture on 2026-04-17 (design §16.6) — the
        device replies with just ``<uid> <stat_code>`` (2 fields, no
        human-readable label). The label is derived from the typed
        variable's :class:`Statistic` display name.
        """
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=2)
        unit_id, stat_code_s = fields
        stat_code = parse_int(stat_code_s, field="statistic_code")
        variable = coerce_loop_control_variable(stat_code)
        # Derive a human-readable label from the corresponding Statistic
        # since the device doesn't echo one. LoopControlVariable is an
        # IntEnum (its `.value` is the wire code, not a name); the
        # statistic registry's display name is the right label source.
        return LoopControlState(
            unit_id=unit_id,
            variable=variable,
            label=variable.name,
        )


LOOP_CONTROL_VARIABLE: LoopControlVariableCommand = LoopControlVariableCommand()
