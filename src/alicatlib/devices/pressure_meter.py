"""Pressure-meter facade (``P-`` / ``PB-`` / ``PS-`` / ``EP-`` prefixes).

Per design §5.9, :class:`PressureMeter` is an empty pass-through that
inherits every user-facing method from :class:`Device`. A pressure
meter reports pressure but does not control it; controller-only
surface (setpoint, valve drive, hold) lives on
:class:`~alicatlib.devices.pressure_controller.PressureController`.

Tare commands (``T`` / ``TP`` / ``PC``) live on :class:`Device` itself
because they're gated by :class:`~alicatlib.commands.base.Capability`
(barometer, pressure sensor) rather than by
:class:`~alicatlib.devices.kind.DeviceKind` — a flow meter with a
barometer can legitimately call ``tare_absolute_pressure`` too.

The class exists as a tag for two purposes:

- The :data:`~alicatlib.devices.factory.MODEL_RULES` dispatch table
  routes ``P-``/``PB-``/``PS-``/``EP-`` prefixed models into this class.
- ``isinstance(dev, PressureMeter)`` lets callers branch on "pressure
  device" without digging into :attr:`DeviceInfo.kind`.
"""

from __future__ import annotations

from alicatlib.devices.base import Device

__all__ = ["PressureMeter"]


class PressureMeter(Device):
    """Pressure *meter* facade — empty pass-through over :class:`Device`.

    A meter reports pressure but does not control it; controller-only
    surface (setpoint / valve drive / hold) lives on
    :class:`~alicatlib.devices.pressure_controller.PressureController`.
    """
