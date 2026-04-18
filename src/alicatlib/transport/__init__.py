"""Transport layer — moves bytes, knows nothing about Alicat.

See ``docs/design.md`` §5.1.
"""

from __future__ import annotations

from alicatlib.transport.base import (
    ByteSize,
    Parity,
    SerialSettings,
    StopBits,
    Transport,
)
from alicatlib.transport.fake import FakeTransport, ScriptedReply
from alicatlib.transport.serial import SerialTransport

__all__ = [
    "ByteSize",
    "FakeTransport",
    "Parity",
    "ScriptedReply",
    "SerialSettings",
    "SerialTransport",
    "StopBits",
    "Transport",
]
