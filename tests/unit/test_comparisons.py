"""Every comparison type, and the registry that holds them."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from migration_reconciliation.evaluation.comparisons import (
    COMPARISONS,
    ComparisonRequest,
    compare,
    requires_expected_value,
    supported_result_types,
)
from migration_reconciliation.models import (
    ComparisonType,
    ErrorCode,
    ExecutionScope,
    ResultType,
    TestStatus,
)


def request(
    comparison: ComparisonType,
    *,
    scope: ExecutionScope = ExecutionScope.SOURCE_TARGET,
    result_type: ResultType = ResultType.INTEGER,
    **values: object,
) -> ComparisonRequest:
    return ComparisonRequest(comparison=comparison, scope=scope, result_type=result_type, **values)


def test_every_comparison_type_has_a_python_function() -> None:
    """A type the workbook can name but Python cannot perform would be a trap."""
    assert set(COMPARISONS) == set(ComparisonType)


# -- EQUAL -------------------------------------------------------------------


def test_equal_passes_on_identical_values() -> None:
    result = compare(request(ComparisonType.EQUAL, source=42, target=42))

    assert result.status is TestStatus.PASS
    assert result.variance == 0


def test_equal_fails_and_reports_the_variance() -> None:
    result = compare(request(ComparisonType.EQUAL, source=100, target=97))

    assert result.status is TestStatus.FAIL
    assert result.error_code is ErrorCode.VALUE_MISMATCH
    assert result.variance == -3


def test_equal_compares_datetimes_and_text_too() -> None:
    moment = datetime(2026, 2, 3, tzinfo=UTC)
    assert (
        compare(
            request(
                ComparisonType.EQUAL,
                result_type=ResultType.DATETIME,
                source=moment,
                target=moment,
            )
        ).status
        is TestStatus.PASS
    )
    assert (
        compare(
            request(
                ComparisonType.EQUAL, result_type=ResultType.TEXT, source="POSTED", target="posted"
            )
        ).status
        is TestStatus.FAIL
    )


# -- tolerances --------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "tolerance", "status"),
    [
        (Decimal("100.00"), Decimal("100.01"), Decimal("0.01"), TestStatus.PASS),
        (Decimal("100.00"), Decimal("99.99"), Decimal("0.01"), TestStatus.PASS),
        (Decimal("100.00"), Decimal("100.02"), Decimal("0.01"), TestStatus.FAIL),
        (Decimal("100"), Decimal("100"), Decimal(0), TestStatus.PASS),
    ],
)
def test_absolute_tolerance_is_the_difference_either_way(
    source: Decimal, target: Decimal, tolerance: Decimal, status: TestStatus
) -> None:
    result = compare(
        request(
            ComparisonType.EQUAL_ABS_TOLERANCE,
            result_type=ResultType.NUMBER,
            source=source,
            target=target,
            absolute_tolerance=tolerance,
        )
    )

    assert result.status is status
    if status is TestStatus.FAIL:
        assert result.error_code is ErrorCode.TOLERANCE_EXCEEDED


def test_percentage_tolerance_compares_against_the_source_baseline() -> None:
    result = compare(
        request(
            ComparisonType.EQUAL_PCT_TOLERANCE,
            result_type=ResultType.NUMBER,
            source=Decimal("1000"),
            target=Decimal("1005"),
            percentage_tolerance=Decimal("1"),
        )
    )

    assert result.status is TestStatus.PASS
    assert result.variance_percentage == Decimal("0.5")


def test_percentage_tolerance_fails_beyond_the_limit() -> None:
    result = compare(
        request(
            ComparisonType.EQUAL_PCT_TOLERANCE,
            result_type=ResultType.NUMBER,
            source=Decimal("1000"),
            target=Decimal("1020"),
            percentage_tolerance=Decimal("1"),
        )
    )

    assert result.status is TestStatus.FAIL
    assert result.error_code is ErrorCode.PERCENTAGE_TOLERANCE_EXCEEDED
    assert result.variance_percentage == Decimal("2")


def test_a_zero_source_passes_only_when_the_target_is_zero_too() -> None:
    result = compare(
        request(
            ComparisonType.EQUAL_PCT_TOLERANCE,
            result_type=ResultType.NUMBER,
            source=Decimal(0),
            target=Decimal(0),
            percentage_tolerance=Decimal("5"),
        )
    )

    assert result.status is TestStatus.PASS
    assert result.variance_percentage == Decimal(0)


def test_a_zero_source_with_a_non_zero_target_fails_rather_than_dividing() -> None:
    result = compare(
        request(
            ComparisonType.EQUAL_PCT_TOLERANCE,
            result_type=ResultType.NUMBER,
            source=Decimal(0),
            target=Decimal("7"),
            percentage_tolerance=Decimal("100"),
        )
    )

    assert result.status is TestStatus.FAIL
    assert result.error_code is ErrorCode.ZERO_SOURCE_BASELINE
    assert result.variance_percentage is None


def test_an_absolute_tolerance_rescues_a_zero_source_explicitly() -> None:
    result = compare(
        request(
            ComparisonType.EQUAL_PCT_TOLERANCE,
            result_type=ResultType.NUMBER,
            source=Decimal(0),
            target=Decimal("0.004"),
            percentage_tolerance=Decimal("1"),
            absolute_tolerance=Decimal("0.01"),
        )
    )

    assert result.status is TestStatus.PASS


# -- expectation-based -------------------------------------------------------


def test_expected_equal_checks_the_executed_side() -> None:
    passing = compare(
        request(
            ComparisonType.EXPECTED_EQUAL,
            scope=ExecutionScope.TARGET_ONLY,
            target=7,
            expected=7,
        )
    )
    failing = compare(
        request(
            ComparisonType.EXPECTED_EQUAL,
            scope=ExecutionScope.TARGET_ONLY,
            target=8,
            expected=7,
        )
    )

    assert passing.status is TestStatus.PASS
    assert failing.status is TestStatus.FAIL
    assert failing.error_code is ErrorCode.VALUE_MISMATCH


def test_expected_zero_passes_only_on_zero() -> None:
    passing = compare(
        request(ComparisonType.EXPECTED_ZERO, scope=ExecutionScope.SOURCE_ONLY, source=0)
    )
    failing = compare(
        request(ComparisonType.EXPECTED_ZERO, scope=ExecutionScope.SOURCE_ONLY, source=4)
    )

    assert passing.status is TestStatus.PASS
    assert failing.status is TestStatus.FAIL
    assert failing.error_code is ErrorCode.EXPECTED_ZERO_FAILED


def test_a_two_sided_scope_requires_every_executed_side_to_hold() -> None:
    """Asserting on one of two executed values would let the other fail unread."""
    result = compare(request(ComparisonType.EXPECTED_ZERO, source=0, target=3))

    assert result.status is TestStatus.FAIL
    assert "target" in result.detail


@pytest.mark.parametrize(
    ("comparison", "value", "expected", "status"),
    [
        (ComparisonType.LESS_THAN_OR_EQUAL, 5, 5, TestStatus.PASS),
        (ComparisonType.LESS_THAN_OR_EQUAL, 6, 5, TestStatus.FAIL),
        (ComparisonType.GREATER_THAN_OR_EQUAL, 5, 5, TestStatus.PASS),
        (ComparisonType.GREATER_THAN_OR_EQUAL, 4, 5, TestStatus.FAIL),
    ],
)
def test_thresholds_are_inclusive(
    comparison: ComparisonType, value: int, expected: int, status: TestStatus
) -> None:
    result = compare(
        request(comparison, scope=ExecutionScope.TARGET_ONLY, target=value, expected=expected)
    )

    assert result.status is status
    if status is TestStatus.FAIL:
        assert result.error_code is ErrorCode.THRESHOLD_EXCEEDED


def test_non_zero_wants_anything_but_zero() -> None:
    assert (
        compare(request(ComparisonType.NON_ZERO, scope=ExecutionScope.TARGET_ONLY, target=1)).status
        is TestStatus.PASS
    )
    failing = compare(request(ComparisonType.NON_ZERO, scope=ExecutionScope.TARGET_ONLY, target=0))
    assert failing.status is TestStatus.FAIL
    assert failing.error_code is ErrorCode.NON_ZERO_FAILED


def test_boolean_true_accepts_only_true() -> None:
    passing = compare(
        request(
            ComparisonType.BOOLEAN_TRUE,
            scope=ExecutionScope.TARGET_ONLY,
            result_type=ResultType.BOOLEAN,
            target=True,
        )
    )
    failing = compare(
        request(
            ComparisonType.BOOLEAN_TRUE,
            scope=ExecutionScope.TARGET_ONLY,
            result_type=ResultType.BOOLEAN,
            target=False,
        )
    )

    assert passing.status is TestStatus.PASS
    assert failing.status is TestStatus.FAIL
    assert failing.error_code is ErrorCode.BOOLEAN_NOT_TRUE


def test_case_insensitive_text_compares_the_two_sides_when_both_ran() -> None:
    result = compare(
        request(
            ComparisonType.TEXT_CASE_INSENSITIVE_EQUAL,
            result_type=ResultType.TEXT,
            source="Posted",
            target="POSTED",
        )
    )

    assert result.status is TestStatus.PASS


def test_case_insensitive_text_compares_with_the_expectation_when_one_side_ran() -> None:
    result = compare(
        request(
            ComparisonType.TEXT_CASE_INSENSITIVE_EQUAL,
            scope=ExecutionScope.TARGET_ONLY,
            result_type=ResultType.TEXT,
            target="posted",
            expected="POSTED",
        )
    )

    assert result.status is TestStatus.PASS


def test_no_comparison_profiles_without_ever_claiming_a_pass() -> None:
    result = compare(
        request(ComparisonType.NO_COMPARISON, scope=ExecutionScope.TARGET_ONLY, target=1234)
    )

    assert result.status is TestStatus.PROFILED
    assert result.error_code is None
    assert "1234" in result.detail


# -- registry metadata -------------------------------------------------------


def test_numeric_comparisons_declare_numeric_result_types() -> None:
    assert supported_result_types(ComparisonType.EXPECTED_ZERO) == frozenset(
        {ResultType.INTEGER, ResultType.NUMBER}
    )
    assert supported_result_types(ComparisonType.BOOLEAN_TRUE) == frozenset({ResultType.BOOLEAN})
    assert supported_result_types(ComparisonType.EQUAL) == frozenset(ResultType)


def test_expected_value_is_required_exactly_where_it_is_used() -> None:
    assert requires_expected_value(ComparisonType.EXPECTED_EQUAL, ExecutionScope.SOURCE_ONLY)
    assert not requires_expected_value(ComparisonType.EXPECTED_ZERO, ExecutionScope.SOURCE_ONLY)
    assert requires_expected_value(
        ComparisonType.TEXT_CASE_INSENSITIVE_EQUAL, ExecutionScope.TARGET_ONLY
    )
    assert not requires_expected_value(
        ComparisonType.TEXT_CASE_INSENSITIVE_EQUAL, ExecutionScope.SOURCE_TARGET
    )
