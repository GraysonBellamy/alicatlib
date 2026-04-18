r"""Analog-output commands — primer §Device setup (Analog Output Source).

One V10 10v05+ command ships here:

- :data:`ANALOG_OUTPUT_SOURCE` (``ASOCV``, all devices with
  :attr:`Capability.ANALOG_OUTPUT`) — query or set which statistic
  (or min/max sentinel) the primary / secondary analog output
  tracks. Gate the secondary channel via
  :attr:`Capability.SECONDARY_ANALOG_OUTPUT` at the facade layer —
  the command-spec allows either channel to pass pre-I/O and the
  session's capability gate catches devices without a second
  analog output.

Design reference: ``docs/design.md`` §9 (Tier-2 all-device scope).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from alicatlib.commands.base import Capability, Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.models import AnalogOutputChannel, AnalogOutputSourceSetting
from alicatlib.errors import (
    AlicatValidationError,
    ErrorContext,
    UnknownUnitError,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.protocol.parser import parse_fields, parse_int
from alicatlib.registry import Unit, unit_registry
from alicatlib.registry._codes_gen import UNIT_BY_CATEGORY_CODE

__all__ = [
    "ANALOG_OUTPUT_SOURCE",
    "AnalogOutputSource",
    "AnalogOutputSourceRequest",
]


_MIN_FIRMWARE_ASOCV: Final[FirmwareVersion] = FirmwareVersion(
    family=FirmwareFamily.V10,
    major=10,
    minor=5,
    raw="10v05",
)


_ALL_DEVICE_KINDS: Final[frozenset[DeviceKind]] = frozenset(DeviceKind)


def _resolve_unit_label(label: str, code: int) -> Unit | None:
    """Best-effort label → :class:`Unit` for the ASOCV decoder."""
    try:
        return unit_registry.coerce(label)
    except UnknownUnitError:
        pass
    matches = {u for (_cat, c), u in UNIT_BY_CATEGORY_CODE.items() if c == code}
    if len(matches) == 1:
        return next(iter(matches))
    return None


@dataclass(frozen=True, slots=True)
class AnalogOutputSourceRequest:
    """Arguments for :data:`ANALOG_OUTPUT_SOURCE`.

    Attributes:
        channel: :class:`AnalogOutputChannel` — primary or secondary
            analog output. Required for both query and set because
            the primer embeds the channel in the query wire form
            (``ASOCV primary_or_secondary``).
        value: Either a :class:`Statistic` wire code (≥2 per primer's
            statistic numbering) or the fixed-level sentinels ``0``
            (minimum) / ``1`` (maximum). ``None`` issues the query
            form.
        unit_code: Engineering-unit code the output should use (primer
            Appendix B). Optional — ``None`` on set leaves units alone;
            ignored on query.
    """

    channel: AnalogOutputChannel
    value: int | None = None
    unit_code: int | None = None


@dataclass(frozen=True, slots=True)
class AnalogOutputSource(Command[AnalogOutputSourceRequest, AnalogOutputSourceSetting]):
    r"""``ASOCV`` — analog-output-source query/set (V10 10v05+).

    Wire shape (primer p. 22):

    - Query: ``<uid><prefix>ASOCV <channel>\r``
    - Set:   ``<uid><prefix>ASOCV <channel> <value> [<unit_code>]\r``

    Response: ``<uid> <value> <unit_code> <unit_label>`` (4 fields).
    When ``value`` is ``0`` / ``1`` (fixed-level sentinels) the primer
    notes ``unit_code=1`` and ``unit_label="---"``.
    """

    name: str = "analog_output_source"
    token: str = "ASOCV"  # noqa: S105 — protocol token
    response_mode: ResponseMode = ResponseMode.LINE
    device_kinds: frozenset[DeviceKind] = _ALL_DEVICE_KINDS
    required_capabilities: Capability = Capability.ANALOG_OUTPUT
    min_firmware: FirmwareVersion | None = _MIN_FIRMWARE_ASOCV
    firmware_families: frozenset[FirmwareFamily] = frozenset({FirmwareFamily.V10})

    def encode(self, ctx: DecodeContext, request: AnalogOutputSourceRequest) -> bytes:
        """Emit ASOCV query or set bytes."""
        if request.value is not None and request.value < 0:
            raise AlicatValidationError(
                f"{self.name}: value must be non-negative (0/1 are min/max "
                f"sentinels; ≥2 are statistic codes); got {request.value}",
                context=ErrorContext(
                    command_name=self.name,
                    unit_id=ctx.unit_id,
                    extra={"value": request.value},
                ),
            )
        prefix = ctx.command_prefix.decode("ascii")
        head = f"{ctx.unit_id}{prefix}{self.token} {int(request.channel)}"
        if request.value is None:
            return (head + "\r").encode("ascii")
        tokens = [head, str(request.value)]
        if request.unit_code is not None:
            tokens.append(str(request.unit_code))
        return (" ".join(tokens) + "\r").encode("ascii")

    def decode(
        self,
        response: bytes | tuple[bytes, ...],
        ctx: DecodeContext,
    ) -> AnalogOutputSourceSetting:
        """Parse 4-field reply into :class:`AnalogOutputSourceSetting`.

        ``channel`` is request-echo; the device doesn't re-echo it so
        the facade fills from the request.
        """
        del ctx
        if isinstance(response, tuple):
            raise TypeError(
                f"{self.name}.decode expected single-line response, got {len(response)} lines",
            )
        text = response.decode("ascii")
        fields = parse_fields(text, command=self.name, expected_count=4)
        unit_id, value_s, code_s, label = fields
        value = parse_int(value_s, field=f"{self.name}.value")
        code = parse_int(code_s, field=f"{self.name}.unit_code")
        return AnalogOutputSourceSetting(
            unit_id=unit_id,
            channel=AnalogOutputChannel.PRIMARY,  # facade replaces with request.channel
            value=value,
            unit_code=code,
            unit=_resolve_unit_label(label, code),
            unit_label=label,
        )


ANALOG_OUTPUT_SOURCE: AnalogOutputSource = AnalogOutputSource()
