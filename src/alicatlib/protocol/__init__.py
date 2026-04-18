"""Protocol layer — frames commands and parses responses.

See ``docs/design.md`` §5.2, §5.11.
"""

from __future__ import annotations

from alicatlib.protocol.client import AlicatProtocolClient
from alicatlib.protocol.framing import EOL, strip_eol
from alicatlib.protocol.parser import (
    parse_ascii,
    parse_fields,
    parse_float,
    parse_int,
)

__all__ = [
    "EOL",
    "AlicatProtocolClient",
    "parse_ascii",
    "parse_fields",
    "parse_float",
    "parse_int",
    "strip_eol",
]
