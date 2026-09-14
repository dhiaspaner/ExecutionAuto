"""Result-type normalization: what it accepts, and what it must refuse."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from migration_reconciliation.errors import TypeConversionError
from migration_reconciliation.evaluation.normalization import display, is_zero, normalize
from migration_reconciliation.models import ResultType


def read(value: object, result_type: ResultType) -> object:
    return normalize(value, result_type, label="The source result")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 5), (5.0, 5), ("5", 5), (Decimal("5.00"), 5), (-3, -3), (0, 0)],
)
def test_integers_arrive_in_the_shapes_drivers_use(value: object, expected: int) -> None:
    assert read(value, ResultType.INTEGER) == expected


@pytest.mark.parametrize("value", [5.5, "5.5", "", None, "twelve", True, False, [1]])
def test_anything_that_is_not_a_whole_number_is_refused(value: object) -> None:
    with pytest.raises(TypeConversionError):
        read(value, ResultType.INTEGER)


def test_numbers_normalize_to_decimal_so_money_totals_exactly() -> None:
    assert read("0.1", ResultType.NUMBER) + read("0.2", ResultType.NUMBER) == Decimal("0.3")


@pytest.mark.parametrize("value", [None, "", "  ", "n/a", True])
def test_a_non_number_never_becomes_zero(value: object) -> None:
    with pytest.raises(TypeConversionError):
        read(value, ResultType.NUMBER)


def test_text_is_trimmed_at_the_ends_only() -> None:
    assert read("  POSTED  value ", ResultType.TEXT) == "POSTED  value"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (1, True), (0, False), ("Yes", True), ("false", False)],
)
def test_booleans_accept_only_unambiguous_values(value: object, expected: bool) -> None:
    assert read(value, ResultType.BOOLEAN) is expected


@pytest.mark.parametrize("value", ["maybe", 2, "", None, "0.5"])
def test_arbitrary_text_never_becomes_false(value: object) -> None:
    with pytest.raises(TypeConversionError):
        read(value, ResultType.BOOLEAN)


def test_naive_datetimes_are_read_as_utc() -> None:
    assert read(datetime(2026, 2, 3, 4, 5), ResultType.DATETIME) == datetime(
        2026, 2, 3, 4, 5, tzinfo=UTC
    )


def test_aware_datetimes_are_converted_to_utc() -> None:
    east = timezone(timedelta(hours=2))
    assert read(datetime(2026, 2, 3, 6, 0, tzinfo=east), ResultType.DATETIME) == datetime(
        2026, 2, 3, 4, 0, tzinfo=UTC
    )


def test_dates_and_iso_strings_are_accepted() -> None:
    assert read(date(2026, 2, 3), ResultType.DATETIME) == datetime(2026, 2, 3, tzinfo=UTC)
    assert read("2026-02-03T04:05:06Z", ResultType.DATETIME) == datetime(
        2026, 2, 3, 4, 5, 6, tzinfo=UTC
    )


def test_an_unparseable_timestamp_is_refused() -> None:
    with pytest.raises(TypeConversionError):
        read("last tuesday", ResultType.DATETIME)


def test_null_is_refused_for_every_type() -> None:
    for result_type in ResultType:
        with pytest.raises(TypeConversionError, match="NULL"):
            read(None, result_type)


def test_only_numeric_zero_counts_as_zero() -> None:
    assert is_zero(0) and is_zero(Decimal("0.00"))
    assert not is_zero(False)
    assert not is_zero("0")
    assert not is_zero(1)


def test_display_renders_numbers_without_exponent_noise() -> None:
    assert display(Decimal("1E+3")) == "1000"
    assert display(Decimal("1.500")) == "1.5"
    assert display(True) == "TRUE"
    assert display(None) == ""
