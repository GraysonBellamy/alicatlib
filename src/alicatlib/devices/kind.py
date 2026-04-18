"""Device-kind enum — leaf module, intentionally imports nothing.

:class:`DeviceKind` lives here (and not in :mod:`alicatlib.devices.base`)
to break what would otherwise be a mandatory import cycle:

- Every command spec in :mod:`alicatlib.commands` declares a
  ``device_kinds: frozenset[DeviceKind]`` at class-definition time,
  so commands need :class:`DeviceKind` available at *import* time.
- :class:`~alicatlib.devices.base.Device` and its subclasses want to
  reference command specs (``GAS_SELECT``, ``ENGINEERING_UNITS``, …)
  in their method bodies.

If ``DeviceKind`` stayed co-located with ``Device`` in ``devices/base.py``,
``Device`` could never import from ``alicatlib.commands`` without
creating a ``devices/base`` ↔ ``commands`` cycle. Extracting the enum
into a zero-import leaf module lets commands import the enum freely
and lets ``Device`` import commands freely — each arrow in the
dependency graph now flows one way (design §15.1).

External callers should continue to use the public re-export
:data:`alicatlib.devices.DeviceKind`. This module's direct path
(``from alicatlib.devices.kind import DeviceKind``) is reserved for
internal use inside :mod:`alicatlib.commands` and :mod:`alicatlib.devices`.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["DeviceKind"]


class DeviceKind(StrEnum):
    """What kind of Alicat device we're talking to.

    Coarser than :class:`alicatlib.commands.base.Capability` — a flow *meter*
    might or might not have a barometer; a flow *controller* might have one,
    two, or three valves. Per-feature gating is via ``Capability``; this enum
    just says "mass-flow meter vs mass-flow controller vs pressure meter ..."
    so commands can declare a short list of compatible kinds.
    """

    FLOW_METER = "flow_meter"
    FLOW_CONTROLLER = "flow_controller"
    PRESSURE_METER = "pressure_meter"
    PRESSURE_CONTROLLER = "pressure_controller"
    UNKNOWN = "unknown"
    """Catch-all for models the factory's ``MODEL_RULES`` table doesn't match.

    A device with this kind still gets a generic :class:`Device` facade
    (``poll()`` and ``execute()`` work); only commands whose
    ``device_kinds`` explicitly list ``UNKNOWN`` will dispatch — the
    session's kind-gating (§5.7) rejects the rest. This is the "loud
    silence" path: we'd rather tell users "unknown model, try model_hint"
    than silently classify a new MFC as a pressure controller.
    """
