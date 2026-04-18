"""Tests for :mod:`alicatlib.testing` — the fixture loader.

Exercises the parser against both synthetic inline fixtures (to pin the
format rules) and the real ``tests/fixtures/responses/*.txt`` files (to
catch fixture-file drift as the M-code / ??D* shape is refined against
hardware captures).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicatlib.commands import GAS_SELECT, GasSelectRequest
from alicatlib.commands.base import DecodeContext
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.registry import Gas
from alicatlib.testing import (
    FakeTransport,
    FakeTransportFromFixture,
    parse_fixture,
)


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "responses"


def _write(tmp_path: Path, content: str) -> Path:
    """Helper — write ``content`` to a tmp fixture file and return its path."""
    path = tmp_path / "fixture.txt"
    path.write_text(content, encoding="ascii")
    return path


# ---------------------------------------------------------------------------
# parse_fixture — format rules
# ---------------------------------------------------------------------------


class TestParseFixture:
    def test_single_send_single_reply(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "> AVE\n< A 10v05\n")
        script = parse_fixture(path)
        assert script == {b"AVE\r": b"A 10v05\r"}

    def test_single_send_multiline_reply_concatenates(self, tmp_path: Path) -> None:
        """Multiple ``<`` lines after one ``>`` become one joined reply."""
        path = _write(
            tmp_path,
            "> A??M*\n< A M01 x\n< A M02 y\n< A M03 z\n",
        )
        script = parse_fixture(path)
        assert script == {b"A??M*\r": b"A M01 x\rA M02 y\rA M03 z\r"}

    def test_send_with_no_reply(self, tmp_path: Path) -> None:
        """``>`` without ``<`` lines → empty reply, for write-only commands."""
        path = _write(tmp_path, "> AFR\n")
        script = parse_fixture(path)
        assert script == {b"AFR\r": b""}

    def test_comments_ignored(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "# scenario: whatever\n# another comment\n> AVE\n# interleaved comment\n< A 10v05\n",
        )
        assert parse_fixture(path) == {b"AVE\r": b"A 10v05\r"}

    def test_blank_lines_ignored(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "\n\n> AVE\n\n< A 10v05\n\n")
        assert parse_fixture(path) == {b"AVE\r": b"A 10v05\r"}

    def test_multiple_send_blocks(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "> AVE\n< A 10v05\n\n> A??D*\n< A D01 Unit_ID text\n< A D02 Mass_Flow decimal\n",
        )
        script = parse_fixture(path)
        assert script == {
            b"AVE\r": b"A 10v05\r",
            b"A??D*\r": b"A D01 Unit_ID text\rA D02 Mass_Flow decimal\r",
        }

    def test_tolerates_missing_space_after_marker(self, tmp_path: Path) -> None:
        """``>AVE`` (no space) parses the same as ``> AVE``."""
        path = _write(tmp_path, ">AVE\n<A 10v05\n")
        assert parse_fixture(path) == {b"AVE\r": b"A 10v05\r"}

    def test_tolerates_crlf_endings(self, tmp_path: Path) -> None:
        """Fixture files authored on Windows still parse cleanly."""
        path = _write(tmp_path, "> AVE\r\n< A 10v05\r\n")
        assert parse_fixture(path) == {b"AVE\r": b"A 10v05\r"}

    def test_reply_line_with_trailing_space_preserved(self, tmp_path: Path) -> None:
        """Trailing whitespace in a reply is wire-realistic (e.g. blank ??M* M10)."""
        # Use a form where trailing whitespace is visible in this test's source:
        # "A M10 " (note trailing space before the newline) is a common
        # real-world shape — the parser must not silently rstrip it.
        path = _write(tmp_path, "> AVE\n< A 10v05  \n")
        script = parse_fixture(path)
        assert script[b"AVE\r"] == b"A 10v05  \r"


# ---------------------------------------------------------------------------
# parse_fixture — errors
# ---------------------------------------------------------------------------


class TestParseFixtureErrors:
    def test_reply_before_send_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "< A 10v05\n")
        with pytest.raises(ValueError, match="without preceding '>'"):
            parse_fixture(path)

    def test_duplicate_send_raises(self, tmp_path: Path) -> None:
        """Duplicate ``>`` entries would silently overwrite — refuse."""
        path = _write(
            tmp_path,
            "> AVE\n< A 10v05\n\n> AVE\n< A 9v00\n",
        )
        with pytest.raises(ValueError, match="duplicate send"):
            parse_fixture(path)

    def test_unrecognized_marker_raises(self, tmp_path: Path) -> None:
        """A line that starts with neither '>' nor '<' nor '#' is an error."""
        path = _write(tmp_path, "bad line\n")
        with pytest.raises(ValueError, match="unrecognized line"):
            parse_fixture(path)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_fixture(tmp_path / "nonexistent.txt")

    def test_error_includes_line_number(self, tmp_path: Path) -> None:
        """Error messages name the source offset so the fixture is easy to fix."""
        path = _write(tmp_path, "# comment\n\n< orphaned reply\n")
        with pytest.raises(ValueError, match=":3:"):
            parse_fixture(path)


# ---------------------------------------------------------------------------
# FakeTransportFromFixture — convenience factory
# ---------------------------------------------------------------------------


class TestFakeTransportFromFixture:
    def test_returns_fake_transport(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "> AVE\n< A 10v05\n")
        fake = FakeTransportFromFixture(path)
        assert isinstance(fake, FakeTransport)

    def test_default_label_points_at_fixture(self, tmp_path: Path) -> None:
        """Default label makes ErrorContext.port identify the fixture file."""
        path = _write(tmp_path, "> AVE\n< A 10v05\n")
        fake = FakeTransportFromFixture(path)
        assert fake.label == "fixture://fixture.txt"

    def test_label_override(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "> AVE\n< A 10v05\n")
        fake = FakeTransportFromFixture(path, label="my-scenario")
        assert fake.label == "my-scenario"

    @pytest.mark.anyio
    async def test_round_trips_through_protocol_client(self, tmp_path: Path) -> None:
        """End-to-end: fixture → transport → client → AlicatProtocolClient query_line."""
        path = _write(tmp_path, "> AVE\n< A 10v05\n")
        fake = FakeTransportFromFixture(path)
        await fake.open()
        client = AlicatProtocolClient(fake, default_timeout=0.1)
        raw = await client.query_line(b"AVE\r")
        assert raw == b"A 10v05"


# ---------------------------------------------------------------------------
# Real fixture files
# ---------------------------------------------------------------------------


class TestShippedFixtures:
    """Parse every shipped fixture — catches drift if a file gets edited."""

    @pytest.mark.parametrize(
        "filename",
        [
            "ve_v10.txt",
            "ve_gp.txt",
            "manufacturing_info_mc.txt",
            "dataframe_format_mc.txt",
            "poll_mc.txt",
            "gas_select_n2.txt",
            "gas_select_legacy_n2.txt",
            "gas_list_mc.txt",
            "engineering_units_mc.txt",
            "full_scale_mc.txt",
            "tare_flow_mc.txt",
            "tare_gauge_pressure_mc.txt",
            "tare_absolute_pressure_mc.txt",
            "setpoint_query_mc.txt",
            "setpoint_set_mc.txt",
            "setpoint_legacy_set_mc.txt",
            "setpoint_source_mc.txt",
            "loop_control_variable_mc.txt",
            "identify_mc_happy.txt",
        ],
    )
    def test_parses(self, filename: str) -> None:
        script = parse_fixture(_FIXTURES_DIR / filename)
        assert script  # non-empty

    def test_ve_v10_maps_to_expected_reply(self) -> None:
        script = parse_fixture(_FIXTURES_DIR / "ve_v10.txt")
        # Real V10 capture (MC-500SCCM-D, 10v20.0-R24) — see design §16.6.
        assert script[b"AVE\r"] == b"A   10v20.0-R24 Jan  9 2025,15:04:07\r"

    def test_manufacturing_info_has_ten_lines(self) -> None:
        """??M* fixture should encode 10 M-code rows."""
        script = parse_fixture(_FIXTURES_DIR / "manufacturing_info_mc.txt")
        reply = script[b"A??M*\r"]
        # 10 reply lines, each CR-terminated.
        assert reply.count(b"\r") == 10

    def test_identify_happy_covers_full_pipeline(self) -> None:
        """The happy-path fixture ships every command open_device emits."""
        script = parse_fixture(_FIXTURES_DIR / "identify_mc_happy.txt")
        assert b"AVE\r" in script
        assert b"A??M*\r" in script
        assert b"A??D*\r" in script
        assert b"A\r" in script

    @pytest.mark.anyio
    async def test_gas_select_fixture_round_trips(self) -> None:
        """The gas_select_n2 fixture round-trips through GAS_SELECT encode/decode."""
        fake = FakeTransportFromFixture(_FIXTURES_DIR / "gas_select_n2.txt")
        await fake.open()
        client = AlicatProtocolClient(fake, default_timeout=0.1)
        ctx = DecodeContext(unit_id="A", firmware=FirmwareVersion.parse("10v05"))
        command = GAS_SELECT.encode(ctx, GasSelectRequest(gas=Gas.N2))
        raw = await client.query_line(command)
        state = GAS_SELECT.decode(raw, ctx)
        assert state.gas is Gas.N2
