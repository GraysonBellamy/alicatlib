"""Device-medium flag — leaf module, intentionally imports nothing.

:class:`Medium` lives beside :class:`~alicatlib.devices.kind.DeviceKind`
and, like that module, is a zero-import leaf (design §15.1). Every
command spec declares a ``media: Medium`` at class-definition time, so
commands need :class:`Medium` available at *import* time; keeping the
enum in a leaf module lets commands import it freely without creating a
cycle back through :mod:`alicatlib.devices.base`.

External callers should use :data:`alicatlib.devices.Medium`; this
module's direct path is reserved for internal imports inside
:mod:`alicatlib.commands` and :mod:`alicatlib.devices`.

Design reference: ``docs/design.md`` §5.9a.
"""

from __future__ import annotations

from enum import Flag, auto

__all__ = ["Medium"]


class Medium(Flag):
    """What kind of fluid a device moves.

    Orthogonal to :class:`~alicatlib.devices.kind.DeviceKind` (function
    × form). A :class:`Flag` rather than a plain :class:`Enum` so the
    model can represent devices whose media is ambiguous at the prefix
    level — either because the hardware truly supports both (some
    Coriolis lines are reported this way) or because the prefix covers
    multiple order-time configurations. Gating via bitwise intersection
    keeps a single code path for every configuration:

    .. code:: python

        if not (device.info.media & command.media):
            raise AlicatMediumMismatchError(...)

    See design §5.9a for the full rationale on modelling medium as a
    flag (not an enum), why the class tree stays kind-shaped rather
    than medium-shaped, and why ``assume_media`` on the factory
    *replaces* rather than *unions*.
    """

    NONE = 0
    """No medium resolved. Only valid as an intermediate during identification;
    a live :class:`~alicatlib.devices.models.DeviceInfo` always carries at
    least one of :attr:`GAS` / :attr:`LIQUID`."""

    GAS = auto()
    """Device is configured for gas. Gas-specific commands (``GS``, ``??G*``,
    gas-mix edits) pass the media gate; liquid-specific commands fail pre-I/O."""

    LIQUID = auto()
    """Device is configured for liquid. Liquid-specific commands (fluid
    select / list, per-fluid reference density) pass the media gate; gas
    commands fail pre-I/O."""
