"""Framing primitives shared by the client, command, and device layers.

Alicat responses are carriage-return delimited and ASCII-encoded. Keeping
these two facts — EOL handling and ASCII decoding — in a single small,
dependency-free module lets higher layers (protocol client, parsers,
data-frame format) import from here without risking an import cycle
through :mod:`alicatlib.protocol.parser`.

Design reference: ``docs/design.md`` §5.2.
"""

from __future__ import annotations

from typing import Final

from alicatlib.errors import AlicatParseError, ErrorContext

__all__ = ["EOL", "decode_ascii", "strip_eol"]


#: The Alicat serial protocol's end-of-line marker. Commands must end in this
#: byte and responses are delimited by it. Declared as ``bytes`` (not ``str``)
#: because the transport layer is byte-oriented.
EOL: Final[bytes] = b"\r"


def strip_eol(data: bytes, *, eol: bytes = EOL) -> bytes:
    """Return ``data`` without a trailing ``eol``.

    Idempotent: if ``data`` already lacks the EOL, it is returned unchanged.
    """
    if data.endswith(eol):
        return data[: -len(eol)]
    return data


def decode_ascii(raw: bytes) -> str:
    """Decode ``raw`` as ASCII, raising :class:`AlicatParseError` on non-ASCII.

    The Alicat wire format is ASCII-only — non-ASCII bytes indicate line
    noise or a framing error, not a legitimate extended-charset response,
    so raising with the raw bytes preserved is the right behaviour.
    Re-exported as :func:`alicatlib.protocol.parser.parse_ascii` for
    callers that already import from the parser module; implementation
    lives here so :mod:`alicatlib.devices.data_frame` can use it without
    introducing a parser-layer import cycle.
    """
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AlicatParseError(
            f"non-ASCII bytes in response: {raw!r}",
            field_name="response",
            expected="ASCII bytes",
            actual=raw,
            context=ErrorContext(raw_response=raw),
        ) from exc
