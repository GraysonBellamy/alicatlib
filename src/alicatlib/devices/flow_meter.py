"""Flow-meter facade — every :attr:`DeviceKind.FLOW_METER` prefix.

Per design §5.9, :class:`FlowMeter` is an empty pass-through that
inherits every user-facing method from :class:`Device` —
``poll``, ``execute``, ``gas``, ``gas_list``, ``engineering_units``,
``full_scale``, tare, and lifecycle hooks all live there.

The class exists as a tag for two purposes:

- The :data:`~alicatlib.devices.factory.MODEL_RULES` dispatch table
  routes every flow-meter prefix into this class. That includes gas
  thermal MFM families (``M-``, ``MS-``, ``MQ-``, ``MW-``, ``MB-``,
  ``MBS-``, ``MWB-``) and liquid laminar-DP meter families (``L-``,
  ``LB-``). The K-family CODA Coriolis prefixes (``K-`` / ``KM-`` /
  ``KC-`` / ``KF-`` / ``KG-``) may route here as well once the
  kind-probe lands (design §16.1) — whether a given CODA unit
  classifies as meter or controller is order-time configuration, not
  prefix-deterministic.
  Flow *controllers* route into
  :class:`~alicatlib.devices.flow_controller.FlowController`; pressure
  instruments route into
  :class:`~alicatlib.devices.pressure_meter.PressureMeter` /
  :class:`~alicatlib.devices.pressure_controller.PressureController`.
- ``isinstance(dev, FlowMeter)`` lets callers branch without digging
  into :attr:`DeviceInfo.kind`.

Medium (gas vs. liquid) is orthogonal to this class tree — see
:class:`~alicatlib.devices.medium.Medium` and design §5.9a. A
``Medium.LIQUID`` flow meter exposes the same method surface as a
``Medium.GAS`` one; the difference is enforced at the per-command
media gate in :class:`~alicatlib.devices.session.Session`.

Controller-only methods (``setpoint``, ``hold_valves``, ``exhaust``,
``batch``, …) live on :class:`FlowController`.
"""

from __future__ import annotations

from alicatlib.devices.base import Device

__all__ = ["FlowMeter"]


class FlowMeter(Device):
    """Mass-flow *meter* facade — empty pass-through over :class:`Device`.

    A meter reports flow but does not control it; controller-only
    surface (setpoint / valve drive / batch) lives on
    :class:`~alicatlib.devices.flow_controller.FlowController`.
    """
