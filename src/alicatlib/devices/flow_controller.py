"""Flow-controller facade — every :attr:`DeviceKind.FLOW_CONTROLLER` prefix.

A flow controller is a flow meter that can *also* drive valves and
hold setpoints. Routing covers every MFC prefix the Alicat Model Guide
names — gas thermal MFC (``MC-``, ``MCS-``, ``MCQ-``, ``MCW-``,
``MCD-``, ``MCDW-``, ``MCV-``, ``MCE-``, ``SFF-``, ``BC-``) and liquid
laminar-DP MFC (``LC-``, ``LCR-``). The K-family CODA Coriolis
prefixes (``K-`` / ``KM-`` / ``KC-`` / ``KF-`` / ``KG-``) may route
here once the kind-probe lands (design §16.1) — whether a given
CODA unit is a meter or a controller is order-time configuration,
not prefix-deterministic.

Medium (gas vs. liquid) is orthogonal to this class — a
``Medium.LIQUID`` MFC exposes the same method surface as a
``Medium.GAS`` one; the difference is enforced at the per-command
media gate in :class:`~alicatlib.devices.session.Session` (design §5.9a).

Controller-only methods (``setpoint`` / ``setpoint_source`` /
``loop_control_variable``) live on
:class:`~alicatlib.devices._controller._ControllerMixin` because they
are shared verbatim with
:class:`~alicatlib.devices.pressure_controller.PressureController` —
both facades inherit the mixin so duplicated method bodies can't drift.
Valve hold / exhaust / batch are planned Tier-2 future work
(design §9).
"""

from __future__ import annotations

from alicatlib.devices._controller import _ControllerMixin  # pyright: ignore[reportPrivateUsage]
from alicatlib.devices.flow_meter import FlowMeter

__all__ = ["FlowController"]


class FlowController(FlowMeter, _ControllerMixin):
    """Flow *controller* facade — gas or liquid; medium is orthogonal.

    Inherits every :class:`~alicatlib.devices.base.Device` method
    (``poll``, ``gas``, ``engineering_units``, ``full_scale``,
    ``tare_*``, ``execute``, ``stream``, lifecycle) via
    :class:`FlowMeter`, plus the controller surface
    (``setpoint`` / ``setpoint_source`` / ``loop_control_variable``)
    via :class:`~alicatlib.devices._controller._ControllerMixin`.

    Python's C3 MRO collapses the two :class:`Device` bases (one via
    :class:`FlowMeter`, one via the mixin) into one — the single
    ``__init__(session)`` on :class:`Device` is the only constructor
    invoked. ``isinstance(dev, FlowMeter)`` continues to work.
    """
