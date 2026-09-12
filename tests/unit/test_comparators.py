"""Comparison strategies."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from migration_reconciliation.errors import ComparisonError
from migration_reconciliation.evaluation.comparators import compare, get_comparator, variance_of
from migration_reconciliation.models import ComparisonRule, ScalarValue

EQUAL = ComparisonRule.EQUAL
EXPECTED_ZERO = ComparisonRule.EXPECTED_ZERO
TOLERANCE = ComparisonRule.NUMERIC_TOLERANCE


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (100, 100),
        (100, 100.0),
        (100, "100"),
        ("100", Decimal("100.00")),
        (Decimal("1.50"), 1.5),
        (0, 0),
        ("POSTED", "POSTED"),
        ("  POSTED  ", "POSTED"),
        (True, 1),
        (datetime(2026, 9, 12, 8, 0), datetime(2026, 9, 12, 8, 0)),
        (date(2026, 9, 12), datetime(2026, 9, 12, 0, 0)),
    ],
)
def test_equal_passes_for_equivalent_values(source: ScalarValue, target: ScalarValue) -> None:
    assert compare(EQUAL, source, target).passed is True


@pytest.mark.parametrize(
    ("source", "target"),
    [(100, 101), ("POSTED", "SETTLED"), (Decimal("1.51"), Decimal("1.50")), (0, 1)],
)
def test_equal_fails_for_different_values(source: ScalarValue, target: ScalarValue) -> None:
    outcome = compare(EQUAL, source, target)

    assert outcome.passed is False
    assert "Mismatch" in outcome.remarks


def test_equal_reports_variance_for_numbers() -> None:
    assert compare(EQUAL, 4201, 4198).variance == Decimal(3)


def test_equal_leaves_variance_blank_for_text() -> None:
    assert compare(EQUAL, "POSTED", "SETTLED").variance is None


def test_equal_is_case_sensitive_for_text() -> None:
    assert compare(EQUAL, "posted", "POSTED").passed is False


def test_expected_zero_passes_only_when_both_sides_are_zero() -> None:
    outcome = compare(EXPECTED_ZERO, 0, 0)

    assert outcome.passed is True
    assert outcome.variance == Decimal(0)


@pytest.mark.parametrize(
    ("source", "target", "expected_in_remarks"),
    [
        (3, 0, "source 3"),
        (0, 5, "target 5"),
        (2, 7, "source 2"),
    ],
)
def test_expected_zero_fails_when_either_side_is_non_zero(
    source: ScalarValue, target: ScalarValue, expected_in_remarks: str
) -> None:
    outcome = compare(EXPECTED_ZERO, source, target)

    assert outcome.passed is False
    assert expected_in_remarks in outcome.remarks


def test_expected_zero_names_both_offending_sides() -> None:
    outcome = compare(EXPECTED_ZERO, 2, 7)

    assert "source 2" in outcome.remarks
    assert "target 7" in outcome.remarks


def test_expected_zero_rejects_non_numeric_results() -> None:
    with pytest.raises(ComparisonError, match="not numeric"):
        compare(EXPECTED_ZERO, "none found", 0)


@pytest.mark.parametrize(
    ("source", "target", "tolerance", "passed"),
    [
        (Decimal("100.00"), Decimal("100.00"), Decimal("0"), True),
        (Decimal("100.05"), Decimal("100.00"), Decimal("0.05"), True),
        (Decimal("100.00"), Decimal("100.05"), Decimal("0.05"), True),
        (Decimal("100.06"), Decimal("100.00"), Decimal("0.05"), False),
        (100.5, 100, Decimal("1"), True),
        (1000, 900, Decimal("50"), False),
        ("98765.43", "98765.44", Decimal("0.01"), True),
    ],
)
def test_numeric_tolerance(
    source: ScalarValue, target: ScalarValue, tolerance: Decimal, passed: bool
) -> None:
    assert compare(TOLERANCE, source, target, tolerance).passed is passed


def test_numeric_tolerance_is_symmetric_in_magnitude() -> None:
    high = compare(TOLERANCE, 105, 100, Decimal(10))
    low = compare(TOLERANCE, 100, 105, Decimal(10))

    assert high.passed is low.passed is True
    assert high.variance == -low.variance


def test_numeric_tolerance_rejects_a_negative_tolerance() -> None:
    with pytest.raises(ComparisonError, match="must not be negative"):
        compare(TOLERANCE, 1, 1, Decimal("-1"))


def test_numeric_tolerance_rejects_text() -> None:
    with pytest.raises(ComparisonError, match="not numeric"):
        compare(TOLERANCE, "abc", 1, Decimal(1))


@pytest.mark.parametrize("rule", list(ComparisonRule))
@pytest.mark.parametrize(
    ("source", "target"),
    [(None, 1), (1, None), (None, None), ("", 1), (1, "   ")],
)
def test_null_never_reconciles_under_any_rule(
    rule: ComparisonRule, source: ScalarValue, target: ScalarValue
) -> None:
    with pytest.raises(ComparisonError, match="NULL"):
        compare(rule, source, target, Decimal(0))


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("POSTED", 100),
        (100, "POSTED"),
        (datetime(2026, 1, 1), 100),
        ("2026-01-01", datetime(2026, 1, 1)),
    ],
)
def test_equal_rejects_incompatible_types(source: ScalarValue, target: ScalarValue) -> None:
    with pytest.raises(ComparisonError, match="incompatible result types"):
        compare(EQUAL, source, target)


def test_float_comparison_avoids_binary_representation_noise() -> None:
    """0.1 + 0.2 != 0.3 in binary floats; the Decimal path must not care."""
    assert compare(EQUAL, 0.1 + 0.2, "0.30000000000000004").passed is True
    assert compare(TOLERANCE, 2.675, 2.67, Decimal("0.005")).passed is True


def test_variance_of_is_none_for_non_numeric_pairs() -> None:
    assert variance_of("POSTED", "POSTED") is None
    assert variance_of(None, 1) is None
    assert variance_of(5, 2) == Decimal(3)


def test_get_comparator_returns_the_registered_strategy() -> None:
    assert get_comparator(EQUAL).rule is EQUAL
    assert get_comparator(EXPECTED_ZERO).rule is EXPECTED_ZERO
    assert get_comparator(TOLERANCE).rule is TOLERANCE
