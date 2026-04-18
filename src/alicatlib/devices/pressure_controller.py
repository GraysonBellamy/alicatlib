"""Pressure-controller facade (``PC-`` / ``PCS-`` / ``PCD-`` / ``EPC-`` / ``EPCD-`` / ``IVC-``).

Per design §5.9, :class:`PressureController` is structurally analogous
to :class:`~alicatlib.devices.flow_controller.FlowController`: it
inherits the meter surface from :class:`PressureMeter` and the shared
controller surface (``setpoint`` / ``setpoint_source`` /
``loop_control_variable``) from
:class:`~alicatlib.devices._controller._ControllerMixin`.

The underlying command specs (``LS`` / ``S`` / ``LSS`` / ``LV``)
already gate to both :attr:`DeviceKind.FLOW_CONTROLLER` and
:attr:`DeviceKind.PRESSURE_CONTROLLER`, so no new wire commands are
required to unlock pressure-controller parity — only the facade
composition. Valve hold / cancel / query and ramp / deadband are
planned future work once the read-only parity surface is proven
(design §9 Tier-2).

Routing is via :data:`~alicatlib.devices.factory.MODEL_RULES`;
``isinstance(dev, PressureController)`` is the user-visible branch
point for "this device is a pressure controller."
"""

from __future__ import annotations

from alicatlib.devices._controller import _ControllerMixin  # pyright: ignore[reportPrivateUsage]
from alicatlib.devices.pressure_meter import PressureMeter

__all__ = ["PressureController"]


class PressureController(PressureMeter, _ControllerMixin):
    """Pressure *controller* facade — shares the controller surface with :class:`FlowController`.

    Inherits :class:`~alicatlib.devices.base.Device` methods (``poll``,
    ``gas``/``fluid`` as applicable, ``engineering_units``,
    ``full_scale``, ``tare_*``, ``execute``, ``stream``, lifecycle)
    via :class:`PressureMeter`, plus the controller surface
    (``setpoint`` / ``setpoint_source`` / ``loop_control_variable``)
    via :class:`~alicatlib.devices._controller._ControllerMixin`.

    Medium-specific behaviour is enforced at the session's per-command
    media gate (design §5.9a); a gas-only or liquid-only pressure
    controller sees identical method shapes here and fails any
    medium-mismatched call pre-I/O with
    :class:`~alicatlib.errors.AlicatMediumMismatchError`.

    ``isinstance(dev, PressureMeter)`` continues to work — the MRO
    routes ``PressureController`` through ``PressureMeter`` → the
    shared ``Device`` base ← ``_ControllerMixin``, collapsing the
    diamond into a single ``Device.__init__``.
    """
