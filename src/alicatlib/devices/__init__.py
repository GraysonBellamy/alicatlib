"""Device facades for every Alicat instrument family.

Facades: :class:`Device`, :class:`FlowMeter`, :class:`FlowController`,
:class:`PressureMeter`, :class:`PressureController`.

See ``docs/design.md`` §5.9 for the class tree and §5.9a for the
orthogonal :class:`Medium` model.

This package keeps ``__init__`` minimal — only the zero-import leaf
modules (:class:`DeviceKind`, :class:`Medium`) re-export here. The
:class:`Session` layer (:mod:`alicatlib.devices.session`) and data-frame
models (:mod:`alicatlib.devices.data_frame`,
:mod:`alicatlib.devices.models`) are imported by protocol-layer
parsers and the command catalog; promoting them into this package's
``__init__`` would trigger a circular import when parsing helpers
reach back into the devices package. Users import those names from
their own modules (``from alicatlib.devices.session import Session``).
"""

from __future__ import annotations

from alicatlib.devices.kind import DeviceKind
from alicatlib.devices.medium import Medium

__all__ = ["DeviceKind", "Medium"]
