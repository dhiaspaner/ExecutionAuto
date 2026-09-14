"""The comparison registry.

Every comparison the workbook can ask for is a Python function in the table at
the bottom of this module. A workbook cell *selects* one by name; it can never
supply one. No Excel formula, Python expression or other executable text from a
spreadsheet is evaluated anywhere in this framework, which is what keeps a
reconciliation result a property of the code that was reviewed rather than of
the file that was opened.

Two conventions run through the whole module:

**Variance is ``target - source``.** Positive means the migrated system has
more. The percentage is that difference against the source, which is the
baseline being migrated away from.

**"The executed side" can be both sides.** One-sided comparisons such as
``EXPECTED_ZERO`` are defined against the value a test actually produced. Under
``SOURCE_ONLY`` or ``TARGET_ONLY`` that is one value; under ``SOURCE_TARGET``
both queries ran, so both must satisfy the comparison. Asserting on only one of
two executed values would let the other fail unread.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation

from ..errors import ComparisonError
from ..models import ComparisonType, ErrorCode, ExecutionScope, ResultType, TestStatus
from .normalization import NormalizedValue, display, is_zero

__all__ = [
    "BOOLEAN_COMPARISONS",
    "COMPARISONS",
    "NUMERIC_COMPARISONS",
    "REQUIRES_EXPECTED_VALUE",
    "TEXT_COMPARISONS",
    "TWO_SIDED_COMPARISONS",
    "ComparisonRequest",
    "ComparisonResult",
    "compare",
    "requires_expected_value",
    "supported_result_types",
]

#: Comparisons that read both sides, and therefore need ``SOURCE_TARGET``.
TWO_SIDED_COMPARISONS: frozenset[ComparisonType] = frozenset(
    {
        ComparisonType.EQUAL,
        ComparisonType.EQUAL_ABS_TOLERANCE,
        ComparisonType.EQUAL_PCT_TOLERANCE,
    }
)

#: Comparisons that only mean something for a number.
NUMERIC_COMPARISONS: frozenset[ComparisonType] = frozenset(
    {
        ComparisonType.EQUAL_ABS_TOLERANCE,
        ComparisonType.EQUAL_PCT_TOLERANCE,
        ComparisonType.EXPECTED_ZERO,
        ComparisonType.LESS_THAN_OR_EQUAL,
        ComparisonType.GREATER_THAN_OR_EQUAL,
        ComparisonType.NON_ZERO,
    }
)

BOOLEAN_COMPARISONS: frozenset[ComparisonType] = frozenset({ComparisonType.BOOLEAN_TRUE})

TEXT_COMPARISONS: frozenset[ComparisonType] = frozenset(
    {ComparisonType.TEXT_CASE_INSENSITIVE_EQUAL}
)

#: Comparisons whose meaning depends on ``Expected_Value`` being filled in.
REQUIRES_EXPECTED_VALUE: frozenset[ComparisonType] = frozenset(
    {
        ComparisonType.EXPECTED_EQUAL,
        ComparisonType.LESS_THAN_OR_EQUAL,
        ComparisonType.GREATER_THAN_OR_EQUAL,
    }
)

_NUMERIC_TYPES: frozenset[ResultType] = frozenset({ResultType.INTEGER, ResultType.NUMBER})

_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class ComparisonRequest:
    """Everything one comparison is allowed to see.

    Deliberately free of the test's SQL, connection and identity: a comparison
    decides from values alone, so it cannot become query-specific behaviour.
    """

    comparison: ComparisonType
    scope: ExecutionScope
    result_type: ResultType
    source: NormalizedValue | None = None
    target: NormalizedValue | None = None
    expected: NormalizedValue | None = None
    absolute_tolerance: Decimal = Decimal(0)
    percentage_tolerance: Decimal = Decimal(0)

    def executed(self) -> tuple[tuple[str, NormalizedValue], ...]:
        """The values this test actually produced, labelled by side."""
        produced: list[tuple[str, NormalizedValue]] = []
        if self.scope.uses_source and self.source is not None:
            produced.append(("source", self.source))
        if self.scope.uses_target and self.target is not None:
            produced.append(("target", self.target))
        return tuple(produced)

    @property
    def actual(self) -> NormalizedValue | None:
        """The headline value for ``Actual_Value``: the migrated side when there is one."""
        if self.scope.uses_target and self.target is not None:
            return self.target
        return self.source


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """The verdict, plus the numbers a report needs to explain it."""

    status: TestStatus
    error_code: ErrorCode | None = None
    variance: Decimal | None = None
    variance_percentage: Decimal | None = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status is TestStatus.PASS


def supported_result_types(comparison: ComparisonType) -> frozenset[ResultType]:
    """Which ``Result_Type`` values make sense for ``comparison``."""
    if comparison in NUMERIC_COMPARISONS:
        return _NUMERIC_TYPES
    if comparison in BOOLEAN_COMPARISONS:
        return frozenset({ResultType.BOOLEAN})
    if comparison in TEXT_COMPARISONS:
        return frozenset({ResultType.TEXT})
    return frozenset(ResultType)


def requires_expected_value(comparison: ComparisonType, scope: ExecutionScope) -> bool:
    """True when ``Expected_Value`` must be filled in for this pairing."""
    if comparison in REQUIRES_EXPECTED_VALUE:
        return True
    # A one-sided text check has nothing to compare against but the expectation.
    return (
        comparison is ComparisonType.TEXT_CASE_INSENSITIVE_EQUAL
        and scope is not ExecutionScope.SOURCE_TARGET
    )


def compare(request: ComparisonRequest) -> ComparisonResult:
    """Apply the registered comparison for ``request``."""
    function = COMPARISONS.get(request.comparison)
    if function is None:  # pragma: no cover - ComparisonType is a closed enum
        raise ComparisonError(f"No comparison registered for '{request.comparison}'.")
    return function(request)


# -- numeric helpers ---------------------------------------------------------


def _decimal_of(value: NormalizedValue | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return None


def _variance(request: ComparisonRequest) -> Decimal | None:
    """``target - source`` when both sides are numeric, else nothing."""
    source = _decimal_of(request.source)
    target = _decimal_of(request.target)
    if source is None or target is None:
        return None
    return target - source


def _variance_percentage(source: Decimal | None, variance: Decimal | None) -> Decimal | None:
    if source is None or variance is None or source == 0:
        return None
    try:
        return (variance / abs(source)) * _HUNDRED
    except (InvalidOperation, DivisionByZero):  # pragma: no cover - guarded by source == 0
        return None


def _require_numeric(value: NormalizedValue | None, label: str) -> Decimal:
    number = _decimal_of(value)
    if number is None:
        raise ComparisonError(f"The {label} value is not numeric, so it cannot be compared.")
    return number


# -- comparisons -------------------------------------------------------------


def _equal(request: ComparisonRequest) -> ComparisonResult:
    variance = _variance(request)
    if request.source == request.target:
        return ComparisonResult(
            TestStatus.PASS,
            variance=variance,
            variance_percentage=_variance_percentage(_decimal_of(request.source), variance),
            detail=f"Source and target both returned {display(request.target)}.",
        )
    return ComparisonResult(
        TestStatus.FAIL,
        ErrorCode.VALUE_MISMATCH,
        variance=variance,
        variance_percentage=_variance_percentage(_decimal_of(request.source), variance),
        detail=(
            f"Source returned {display(request.source)} but target returned "
            f"{display(request.target)}."
        ),
    )


def _equal_abs_tolerance(request: ComparisonRequest) -> ComparisonResult:
    source = _require_numeric(request.source, "source")
    target = _require_numeric(request.target, "target")
    tolerance = request.absolute_tolerance
    variance = target - source
    difference = abs(variance)
    percentage = _variance_percentage(source, variance)
    if difference <= tolerance:
        return ComparisonResult(
            TestStatus.PASS,
            variance=variance,
            variance_percentage=percentage,
            detail=(
                f"Difference {display(difference)} is within the absolute tolerance "
                f"{display(tolerance)}."
            ),
        )
    return ComparisonResult(
        TestStatus.FAIL,
        ErrorCode.TOLERANCE_EXCEEDED,
        variance=variance,
        variance_percentage=percentage,
        detail=(
            f"Difference {display(difference)} exceeds the absolute tolerance "
            f"{display(tolerance)} (source {display(source)}, target {display(target)})."
        ),
    )


def _equal_pct_tolerance(request: ComparisonRequest) -> ComparisonResult:
    source = _require_numeric(request.source, "source")
    target = _require_numeric(request.target, "target")
    variance = target - source
    difference = abs(variance)

    if source == 0:
        # A percentage of zero is undefined, so it is never guessed. Equal
        # zeroes reconcile; anything else needs an absolute tolerance to say
        # explicitly how much drift from nothing is acceptable.
        if target == 0:
            return ComparisonResult(
                TestStatus.PASS,
                variance=variance,
                variance_percentage=Decimal(0),
                detail="Source and target are both 0.",
            )
        if request.absolute_tolerance > 0 and difference <= request.absolute_tolerance:
            return ComparisonResult(
                TestStatus.PASS,
                variance=variance,
                detail=(
                    f"Source is 0, so the percentage is undefined; difference "
                    f"{display(difference)} is within the absolute tolerance "
                    f"{display(request.absolute_tolerance)}."
                ),
            )
        return ComparisonResult(
            TestStatus.FAIL,
            ErrorCode.ZERO_SOURCE_BASELINE,
            variance=variance,
            detail=(
                f"Source is 0 and target is {display(target)}, so a percentage cannot be "
                f"calculated. Set Absolute_Tolerance to state how much drift from zero is "
                f"acceptable."
            ),
        )

    percentage = _variance_percentage(source, variance)
    assert percentage is not None  # source is non-zero
    tolerance = request.percentage_tolerance
    if abs(percentage) <= tolerance:
        return ComparisonResult(
            TestStatus.PASS,
            variance=variance,
            variance_percentage=percentage,
            detail=(
                f"Variance {display(percentage)}% is within the percentage tolerance "
                f"{display(tolerance)}%."
            ),
        )
    return ComparisonResult(
        TestStatus.FAIL,
        ErrorCode.PERCENTAGE_TOLERANCE_EXCEEDED,
        variance=variance,
        variance_percentage=percentage,
        detail=(
            f"Variance {display(percentage)}% exceeds the percentage tolerance "
            f"{display(tolerance)}% (source {display(source)}, target {display(target)})."
        ),
    )


def _one_sided(
    request: ComparisonRequest,
    *,
    holds: Callable[[NormalizedValue], bool],
    code: ErrorCode,
    describe_pass: Callable[[str], str],
    describe_fail: Callable[[str, str], str],
) -> ComparisonResult:
    """Apply a single-value rule to every side this test executed."""
    executed = request.executed()
    if not executed:  # pragma: no cover - the engine never reaches a comparison without a value
        raise ComparisonError("There is no executed value to compare.")
    variance = _variance(request)
    percentage = _variance_percentage(_decimal_of(request.source), variance)
    failures = [(side, value) for side, value in executed if not holds(value)]
    if failures:
        side, value = failures[0]
        return ComparisonResult(
            TestStatus.FAIL,
            code,
            variance=variance,
            variance_percentage=percentage,
            detail=describe_fail(side, display(value)),
        )
    return ComparisonResult(
        TestStatus.PASS,
        variance=variance,
        variance_percentage=percentage,
        detail=describe_pass(", ".join(f"{side} {display(value)}" for side, value in executed)),
    )


def _expected_equal(request: ComparisonRequest) -> ComparisonResult:
    expected = request.expected
    return _one_sided(
        request,
        holds=lambda value: value == expected,
        code=ErrorCode.VALUE_MISMATCH,
        describe_pass=lambda seen: (
            f"Executed value equals the expected {display(expected)} ({seen})."
        ),
        describe_fail=lambda side, seen: (
            f"Expected {display(expected)} but the {side} query returned {seen}."
        ),
    )


def _expected_zero(request: ComparisonRequest) -> ComparisonResult:
    return _one_sided(
        request,
        holds=is_zero,
        code=ErrorCode.EXPECTED_ZERO_FAILED,
        describe_pass=lambda seen: f"Expected 0 and found 0 ({seen}).",
        describe_fail=lambda side, seen: f"Expected 0 but the {side} query returned {seen}.",
    )


def _less_than_or_equal(request: ComparisonRequest) -> ComparisonResult:
    limit = _require_numeric(request.expected, "expected")
    return _one_sided(
        request,
        holds=lambda value: _require_numeric(value, "executed") <= limit,
        code=ErrorCode.THRESHOLD_EXCEEDED,
        describe_pass=lambda seen: f"Executed value is at most {display(limit)} ({seen}).",
        describe_fail=lambda side, seen: (
            f"The {side} query returned {seen}, above the limit {display(limit)}."
        ),
    )


def _greater_than_or_equal(request: ComparisonRequest) -> ComparisonResult:
    floor = _require_numeric(request.expected, "expected")
    return _one_sided(
        request,
        holds=lambda value: _require_numeric(value, "executed") >= floor,
        code=ErrorCode.THRESHOLD_EXCEEDED,
        describe_pass=lambda seen: f"Executed value is at least {display(floor)} ({seen}).",
        describe_fail=lambda side, seen: (
            f"The {side} query returned {seen}, below the minimum {display(floor)}."
        ),
    )


def _non_zero(request: ComparisonRequest) -> ComparisonResult:
    return _one_sided(
        request,
        holds=lambda value: not is_zero(value),
        code=ErrorCode.NON_ZERO_FAILED,
        describe_pass=lambda seen: f"Executed value is not zero ({seen}).",
        describe_fail=lambda side, seen: (
            f"The {side} query returned {seen}; a non-zero value was required."
        ),
    )


def _boolean_true(request: ComparisonRequest) -> ComparisonResult:
    return _one_sided(
        request,
        holds=lambda value: value is True,
        code=ErrorCode.BOOLEAN_NOT_TRUE,
        describe_pass=lambda seen: f"Executed value is TRUE ({seen}).",
        describe_fail=lambda side, seen: f"The {side} query returned {seen}; TRUE was required.",
    )


def _text_case_insensitive_equal(request: ComparisonRequest) -> ComparisonResult:
    if request.scope is ExecutionScope.SOURCE_TARGET:
        source = _text(request.source)
        target = _text(request.target)
        if source.casefold() == target.casefold():
            return ComparisonResult(
                TestStatus.PASS,
                detail=f"Source and target text match ignoring case ('{target}').",
            )
        return ComparisonResult(
            TestStatus.FAIL,
            ErrorCode.VALUE_MISMATCH,
            detail=f"Source returned '{source}' but target returned '{target}'.",
        )
    expected = _text(request.expected)
    return _one_sided(
        request,
        holds=lambda value: _text(value).casefold() == expected.casefold(),
        code=ErrorCode.VALUE_MISMATCH,
        describe_pass=lambda seen: f"Executed text matches '{expected}' ignoring case ({seen}).",
        describe_fail=lambda side, seen: (
            f"Expected '{expected}' but the {side} query returned {seen}."
        ),
    )


def _no_comparison(request: ComparisonRequest) -> ComparisonResult:
    """Record the value and say so. A profiled test is never a pass."""
    seen = ", ".join(f"{side} {display(value)}" for side, value in request.executed())
    return ComparisonResult(
        TestStatus.PROFILED,
        variance=_variance(request),
        detail=f"Profiled without a pass/fail rule ({seen}).",
    )


def _text(value: NormalizedValue | None) -> str:
    return "" if value is None else str(value)


#: The whole vocabulary, in one table. Nothing outside it can be executed.
COMPARISONS: Mapping[ComparisonType, Callable[[ComparisonRequest], ComparisonResult]] = {
    ComparisonType.EQUAL: _equal,
    ComparisonType.EQUAL_ABS_TOLERANCE: _equal_abs_tolerance,
    ComparisonType.EQUAL_PCT_TOLERANCE: _equal_pct_tolerance,
    ComparisonType.EXPECTED_EQUAL: _expected_equal,
    ComparisonType.EXPECTED_ZERO: _expected_zero,
    ComparisonType.LESS_THAN_OR_EQUAL: _less_than_or_equal,
    ComparisonType.GREATER_THAN_OR_EQUAL: _greater_than_or_equal,
    ComparisonType.NON_ZERO: _non_zero,
    ComparisonType.BOOLEAN_TRUE: _boolean_true,
    ComparisonType.TEXT_CASE_INSENSITIVE_EQUAL: _text_case_insensitive_equal,
    ComparisonType.NO_COMPARISON: _no_comparison,
}
