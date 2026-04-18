"""Tests for :mod:`alicatlib.devices.data_frame`.

The parse path is the only non-trivial logic in this module; it has to
round-trip required fields positionally, peel off status-code tails,
tolerate missing conditional fields, and preserve the ``--`` → ``None``
distinction on a per-field basis.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from alicatlib.devices.data_frame import (
    DataFrame,
    DataFrameField,
    DataFrameFormat,
    DataFrameFormatFlavor,
    ParsedFrame,
)
from alicatlib.devices.models import StatusCode
from alicatlib.errors import AlicatParseError
from alicatlib.protocol.parser import parse_float, parse_optional_float
from alicatlib.registry._codes_gen import Statistic, Unit
from tests._typing import approx


def _identity(value: str) -> str:
    return value


def _sample_format() -> DataFrameFormat:
    """A minimal format mirroring a single-valve flow-controller poll.

    First field is the unit-ID text token; three numeric required fields
    follow. No conditional fields — keeps the baseline test tight.
    """
    return DataFrameFormat(
        fields=(
            DataFrameField(
                name="Unit_ID",
                raw_name="Unit_ID",
                type_name="text",
                statistic=None,
                unit=None,
                conditional=False,
                parser=_identity,
            ),
            DataFrameField(
                name="Abs_Press",
                raw_name="Abs_Press",
                type_name="decimal",
                statistic=Statistic.ABS_PRESS,
                unit=None,
                conditional=False,
                parser=lambda s: parse_float(s, field="Abs_Press"),
            ),
            DataFrameField(
                name="Mass_Flow",
                raw_name="Mass_Flow",
                type_name="decimal",
                statistic=Statistic.MASS_FLOW,
                unit=Unit.SCCM,
                conditional=False,
                parser=lambda s: parse_float(s, field="Mass_Flow"),
            ),
            DataFrameField(
                name="Setpoint",
                raw_name="Setpoint",
                type_name="decimal",
                statistic=Statistic.MASS_FLOW_SETPT,
                unit=Unit.SCCM,
                conditional=False,
                parser=lambda s: parse_optional_float(s, field="Setpoint"),
            ),
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


def _sample_format_with_conditional() -> DataFrameFormat:
    """Format whose trailing field is conditional — for the ``*`` path."""
    return DataFrameFormat(
        fields=(
            DataFrameField(
                name="Unit_ID",
                raw_name="Unit_ID",
                type_name="text",
                statistic=None,
                unit=None,
                conditional=False,
                parser=_identity,
            ),
            DataFrameField(
                name="Mass_Flow",
                raw_name="Mass_Flow",
                type_name="decimal",
                statistic=Statistic.MASS_FLOW,
                unit=Unit.SCCM,
                conditional=False,
                parser=lambda s: parse_float(s, field="Mass_Flow"),
            ),
            DataFrameField(
                name="Gas_Label",
                raw_name="Gas_Label",
                type_name="text",
                statistic=None,
                unit=None,
                conditional=True,
                parser=_identity,
            ),
        ),
        flavor=DataFrameFormatFlavor.DEFAULT,
    )


# ---------------------------------------------------------------------------
# DataFrameFormatFlavor
# ---------------------------------------------------------------------------


class TestDataFrameFormatFlavor:
    def test_values_are_stable(self) -> None:
        assert DataFrameFormatFlavor.DEFAULT.value == 0
        assert DataFrameFormatFlavor.SIGNED.value == 1
        assert DataFrameFormatFlavor.VARIABLE_V8.value == 2


# ---------------------------------------------------------------------------
# DataFrameField / DataFrameFormat basics
# ---------------------------------------------------------------------------


class TestDataFrameFormat:
    def test_names_in_declared_order(self) -> None:
        fmt = _sample_format()
        assert fmt.names() == ("Unit_ID", "Abs_Press", "Mass_Flow", "Setpoint")

    def test_is_frozen(self) -> None:
        fmt = _sample_format()
        with pytest.raises(FrozenInstanceError):
            fmt.flavor = DataFrameFormatFlavor.SIGNED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DataFrameFormat.parse — happy path
# ---------------------------------------------------------------------------


class TestParseHappyPath:
    def test_parses_required_fields(self) -> None:
        fmt = _sample_format()
        parsed = fmt.parse(b"A 14.70 25.5 50.0")
        assert parsed.unit_id == "A"
        assert parsed.values["Unit_ID"] == "A"
        assert parsed.values["Abs_Press"] == approx(14.70)
        assert parsed.values["Mass_Flow"] == approx(25.5)
        assert parsed.values["Setpoint"] == approx(50.0)
        assert parsed.status == frozenset()

    def test_absent_setpoint_becomes_none(self) -> None:
        """`--` sentinel on a field with ``parse_optional_float`` → None."""
        fmt = _sample_format()
        parsed = fmt.parse(b"A 14.70 0.001 --")
        assert parsed.values["Setpoint"] is None
        # 0.001 != None — regression guard for conflating zero with missing.
        assert parsed.values["Mass_Flow"] == approx(0.001)

    def test_zero_mass_flow_round_trips(self) -> None:
        """0.0 is a valid measurement, never collapse to None or False."""
        fmt = _sample_format()
        parsed = fmt.parse(b"A 14.70 0 50.0")
        assert parsed.values["Mass_Flow"] == approx(0.0)
        assert parsed.values["Mass_Flow"] is not None

    def test_signed_mass_flow(self) -> None:
        """Bidirectional controllers emit leading ``-`` / ``+``."""
        fmt = _sample_format()
        parsed = fmt.parse(b"A 14.70 -1.5 +50.0")
        assert parsed.values["Mass_Flow"] == approx(-1.5)
        assert parsed.values["Setpoint"] == approx(50.0)

    def test_values_by_statistic_is_populated(self) -> None:
        fmt = _sample_format()
        parsed = fmt.parse(b"A 14.70 25.5 50.0")
        assert parsed.values_by_statistic[Statistic.MASS_FLOW] == approx(25.5)
        assert parsed.values_by_statistic[Statistic.ABS_PRESS] == approx(14.70)
        # Unit_ID has statistic=None — must not appear in values_by_statistic.
        assert len(parsed.values_by_statistic) == 3

    def test_fields_without_statistic_omitted_from_values_by_statistic(self) -> None:
        fmt = _sample_format()
        parsed = fmt.parse(b"A 14.70 25.5 50.0")
        for stat in parsed.values_by_statistic:
            assert stat is not None


# ---------------------------------------------------------------------------
# Conditional fields and status codes
# ---------------------------------------------------------------------------


class TestParseConditionalAndStatus:
    def test_conditional_field_present(self) -> None:
        fmt = _sample_format_with_conditional()
        parsed = fmt.parse(b"A 25.5 N2")
        assert parsed.values["Mass_Flow"] == approx(25.5)
        assert parsed.values["Gas_Label"] == "N2"

    def test_conditional_field_absent(self) -> None:
        """Absent conditional must *not* be present in values (absent != None)."""
        fmt = _sample_format_with_conditional()
        parsed = fmt.parse(b"A 25.5")
        assert parsed.values["Mass_Flow"] == approx(25.5)
        assert "Gas_Label" not in parsed.values

    def test_single_status_code_collapsed(self) -> None:
        fmt = _sample_format_with_conditional()
        parsed = fmt.parse(b"A 25.5 N2 HLD")
        assert parsed.status == frozenset({StatusCode.HLD})

    def test_multiple_status_codes(self) -> None:
        fmt = _sample_format_with_conditional()
        parsed = fmt.parse(b"A 25.5 N2 MOV TMF")
        assert parsed.status == frozenset({StatusCode.MOV, StatusCode.TMF})

    def test_status_without_conditional(self) -> None:
        """Status codes trail even when conditional slots are empty."""
        fmt = _sample_format_with_conditional()
        parsed = fmt.parse(b"A 25.5 HLD")
        assert parsed.values["Mass_Flow"] == approx(25.5)
        assert "Gas_Label" not in parsed.values
        assert parsed.status == frozenset({StatusCode.HLD})

    def test_status_codes_unordered_on_wire(self) -> None:
        """Order is lost via frozenset — regression guard for "order matters" bugs."""
        fmt = _sample_format_with_conditional()
        a = fmt.parse(b"A 25.5 N2 HLD MOV")
        b = fmt.parse(b"A 25.5 N2 MOV HLD")
        assert a.status == b.status


# ---------------------------------------------------------------------------
# Parse errors
# ---------------------------------------------------------------------------


class TestParseErrors:
    def test_empty_frame(self) -> None:
        fmt = _sample_format()
        with pytest.raises(AlicatParseError) as ei:
            fmt.parse(b"")
        assert ei.value.context.raw_response == b""

    def test_truncated_missing_required(self) -> None:
        """Fewer tokens than required fields is a protocol error."""
        fmt = _sample_format()
        with pytest.raises(AlicatParseError) as ei:
            fmt.parse(b"A 14.70 25.5")
        assert ei.value.expected == 4
        assert ei.value.actual == 3

    def test_non_ascii_rejects(self) -> None:
        fmt = _sample_format()
        with pytest.raises(AlicatParseError):
            fmt.parse(b"A \xff\xfe 25.5 50.0")

    def test_garbage_numeric_field_raises(self) -> None:
        """A required numeric field containing garbage surfaces per-field."""
        fmt = _sample_format()
        with pytest.raises(AlicatParseError) as ei:
            fmt.parse(b"A 14.70 not-a-number 50.0")
        assert ei.value.field_name == "Mass_Flow"


# ---------------------------------------------------------------------------
# DataFrame (timing wrapper)
# ---------------------------------------------------------------------------


class TestDataFrame:
    def _fixture(self) -> DataFrame:
        fmt = _sample_format()
        parsed = fmt.parse(b"A 14.70 25.5 50.0")
        return DataFrame.from_parsed(
            parsed,
            format=fmt,
            received_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
            monotonic_ns=123_456_789,
        )

    def test_from_parsed_preserves_values(self) -> None:
        df = self._fixture()
        assert df.unit_id == "A"
        assert df.values["Mass_Flow"] == approx(25.5)
        assert df.monotonic_ns == 123_456_789

    def test_get_float_on_numeric_field(self) -> None:
        df = self._fixture()
        got = df.get_float("Mass_Flow")
        assert got == approx(25.5)

    def test_get_float_on_text_field_returns_none(self) -> None:
        """Text-valued fields yield None — forgiving accessor."""
        df = self._fixture()
        assert df.get_float("Unit_ID") is None

    def test_get_float_on_missing_field_returns_none(self) -> None:
        df = self._fixture()
        assert df.get_float("Not_A_Real_Field") is None

    def test_get_statistic_returns_typed_value(self) -> None:
        df = self._fixture()
        assert df.get_statistic(Statistic.MASS_FLOW) == approx(25.5)

    def test_get_statistic_for_unmodelled_returns_none(self) -> None:
        df = self._fixture()
        assert df.get_statistic(Statistic.VALVE_DRIVE) is None

    def test_as_dict_includes_status_and_received_at(self) -> None:
        df = self._fixture()
        result = df.as_dict()
        assert result["Mass_Flow"] == approx(25.5)
        assert result["status"] == ""  # no codes active
        assert result["received_at"] == "2026-04-16T12:00:00+00:00"

    def test_as_dict_status_is_sorted_comma_joined(self) -> None:
        """Schema-stable: status column always present, deterministic order."""
        fmt = _sample_format_with_conditional()
        parsed = fmt.parse(b"A 25.5 N2 MOV HLD")
        df = DataFrame.from_parsed(
            parsed,
            format=fmt,
            received_at=datetime(2026, 4, 16, tzinfo=UTC),
            monotonic_ns=0,
        )
        result = df.as_dict()
        assert result["status"] == "HLD,MOV"

    def test_is_frozen(self) -> None:
        df = self._fixture()
        with pytest.raises(FrozenInstanceError):
            df.unit_id = "B"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ParsedFrame — pure parse result (no timing)
# ---------------------------------------------------------------------------


class TestParsedFrame:
    def test_has_no_timing_fields(self) -> None:
        """ParsedFrame is clock-free by design (§5.6). Regression guard."""
        fmt = _sample_format()
        parsed = fmt.parse(b"A 14.70 25.5 50.0")
        # Attribute access for timing fields should fail.
        assert not hasattr(parsed, "received_at")
        assert not hasattr(parsed, "monotonic_ns")
        assert isinstance(parsed, ParsedFrame)
