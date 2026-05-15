"""Protocol layer — frames commands and parses responses.

See ``docs/design.md`` §5.2, §5.11.
"""

from __future__ import annotations

from enum import Enum

from alicatlib.protocol.client import AlicatProtocolClient
from alicatlib.protocol.framing import EOL, strip_eol
from alicatlib.protocol.parser import (
    parse_ascii,
    parse_fields,
    parse_float,
    parse_int,
)


class ProtocolKind(Enum):
    """Wire protocol an Alicat library instance speaks.

    Alicat devices use a single line-oriented ASCII protocol on every
    transport (RS-232 / RS-485 / USB-CDC). The enum exists so the
    cross-lib :class:`DiscoveryResult` / :class:`ErrorContext` base
    fields can carry a typed protocol marker; for alicat the value is
    always :attr:`ASCII` (or ``None`` when not applicable).
    """

    ASCII = "ascii"


__all__ = [
    "EOL",
    "AlicatProtocolClient",
    "ProtocolKind",
    "parse_ascii",
    "parse_fields",
    "parse_float",
    "parse_int",
    "strip_eol",
]
