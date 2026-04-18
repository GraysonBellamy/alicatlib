r"""Testing helpers — re-exports + fixture-file loader.

Importing from this module keeps downstream test code one import deep:

.. code-block:: python

    from alicatlib.testing import FakeTransport, FakeTransportFromFixture

The fixture format is plaintext, intentionally skimmable by humans so
captured hardware sessions round-trip through code review::

    # scenario: identify-flow-controller (10v05, MC-100SCCM-D)
    > A??M*
    < A M01 Alicat Scientific
    < A M02 www.example.com
    < A M03 +1 555-0000
    < A M04 MC-100SCCM-D
    ...

Parsing rules are deliberately narrow (design §6.2):

- Lines starting with ``#`` are comments; ignored.
- Blank lines are ignored.
- ``>`` introduces a send — the carriage-return terminator is appended
  automatically so the fixture stays readable.
- ``<`` introduces one reply line (``\\r``-terminated).
- Multiple ``<`` lines after a single ``>`` concatenate into one
  :class:`FakeTransport` scripted reply — the right shape for
  multiline commands like ``??M*``.
- Duplicate ``>`` entries are a file-format error rather than a silent
  overwrite.

Not shipped yet: ``record_session(device, scenario, path)``, which
captures live hardware traffic into this format. Planned alongside the
hardware integration suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alicatlib.transport.fake import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "FakeTransport",
    "FakeTransportFromFixture",
    "parse_fixture",
]


def _iter_semantic_lines(text: Iterable[str]) -> Iterable[tuple[int, str]]:
    """Yield ``(line_number, stripped_line)`` for lines that carry content.

    Blank lines and comment lines are skipped — the caller sees only
    semantic (``>`` / ``<``) rows, each with its 1-based source line
    number so error messages pinpoint the fixture offset.
    """
    for line_number, raw in enumerate(text, start=1):
        stripped = raw.rstrip("\r\n")
        lean = stripped.lstrip()
        if not lean or lean.startswith("#"):
            continue
        yield line_number, stripped


def _content_after_marker(line: str, marker: str) -> str:
    """Return the payload after a single ``>`` or ``<`` marker.

    Tolerates a single optional space after the marker for readability
    (``> A??M*`` → ``A??M*``), but preserves *trailing* whitespace
    because a wire-realistic ``A M10`` (with trailing blank payload) is
    a legitimate Alicat reply shape.
    """
    without = line.lstrip()[len(marker) :]
    return without.removeprefix(" ")


def parse_fixture(path: str | Path) -> dict[bytes, bytes]:
    r"""Parse a fixture file into a :class:`FakeTransport` script map.

    The returned dict maps ``send_bytes → reply_bytes``. Both payloads
    are ASCII-encoded with a trailing ``\r`` on every logical line — the
    exact shape :class:`FakeTransport` expects.

    Raises:
        ValueError: On malformed lines, a ``<`` before any ``>``, or a
            duplicate ``>`` entry. Every error message names the
            offending line number in the source file.
        FileNotFoundError: Via the underlying :meth:`Path.read_text`.
    """
    fixture_path = Path(path)
    script: dict[bytes, bytes] = {}
    current_send: bytes | None = None
    current_reply_chunks: list[bytes] = []

    def _flush() -> None:
        nonlocal current_send, current_reply_chunks
        if current_send is None:
            return
        if current_send in script:
            raise ValueError(
                f"{fixture_path}: duplicate send entry {current_send!r}",
            )
        script[current_send] = b"".join(current_reply_chunks)
        current_send = None
        current_reply_chunks = []

    # File is read as UTF-8 so comments can include non-ASCII characters.
    # Individual ``>`` / ``<`` payloads are encoded back to ASCII at the
    # byte-emit step below — any non-ASCII payload surfaces as UnicodeEncodeError
    # at that point, which is the right behavior (Alicat wire is ASCII-only).
    text = fixture_path.read_text(encoding="utf-8")
    for line_number, line in _iter_semantic_lines(text.splitlines()):
        lean = line.lstrip()
        if lean.startswith(">"):
            _flush()
            payload = _content_after_marker(line, ">")
            current_send = payload.encode("ascii") + b"\r"
        elif lean.startswith("<"):
            if current_send is None:
                raise ValueError(
                    f"{fixture_path}:{line_number}: '<' line without preceding '>'",
                )
            payload = _content_after_marker(line, "<")
            current_reply_chunks.append(payload.encode("ascii") + b"\r")
        else:
            raise ValueError(
                f"{fixture_path}:{line_number}: unrecognized line {line!r}; "
                f"lines must start with '>', '<', or '#'",
            )
    _flush()
    return script


def FakeTransportFromFixture(  # noqa: N802 — public factory, title-case matches the class it returns
    path: str | Path,
    *,
    label: str | None = None,
) -> FakeTransport:
    """Load a fixture file into a new, already-built :class:`FakeTransport`.

    The convenience for test code is that one line replaces the
    boilerplate "parse fixture, construct transport". The returned
    transport is *not* open — the caller awaits ``.open()`` as usual —
    because :class:`FakeTransport`'s construction is synchronous but
    opening isn't.

    Args:
        path: Path to the ``.txt`` fixture.
        label: Optional override for :attr:`FakeTransport.label`; defaults
            to ``"fixture://<basename>"`` so :class:`ErrorContext.port`
            entries point at the actual fixture file during failures.
    """
    script = parse_fixture(path)
    resolved_label = label if label is not None else f"fixture://{Path(path).name}"
    return FakeTransport(script, label=resolved_label)
