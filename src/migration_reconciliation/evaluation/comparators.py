"""Comparison strategies.

Each strategy decides whether one source/target scalar pair reconciles. Adding a
rule means adding a class here and registering it — never a domain-specific
runner. Strategies never coerce across value kinds: a comparison that is not
meaningful raises :class:`ComparisonError` and the test case is reported as
``ERROR`` on the ``COMPARISON`` side rather than quietly passing or failing.

NULL policy
-----------
A NULL (empty) result never reconciles, under any rule — not even NULL vs NULL.
An empty scalar almost always means the query matched nothing, which is exactly
the case that must not be reported as a pass. Wrap aggregates in ``COALESCE``
(or ``NVL``) so the query returns ``0`` when that is the intended answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..errors import ComparisonError
from ..models import ComparisonRule, ScalarValue

__all__ = [
    "Comparator",
    "ComparisonOutcome",
    "EqualComparator",
    "ExpectedZeroComparator",
    "NumericToleranceComparator",
    "ValueKind",
    "compare",
    "get_comparator",
    "to_decimal",
    "variance_of",
]


class ValueKind(StrEnum):
    """The comparable kinds a scalar can normalize to."""

    NULL = "null"
    NUMERIC = "numeric"
    TEXT = "text"
    TEMPORAL = "temporal"


@dataclass(frozen=True, slots=True)
class ComparisonOutcome:
    """Result of applying one rule to one source/target pair."""

    passed: bool
    variance: Decimal | None
    remarks: str


@runtime_checkable
class Comparator(Protocol):
    """Interface every comparison strategy implements."""

    rule: ComparisonRule

    def compare(
        self, source: ScalarValue, target: ScalarValue, tolerance: Decimal
    ) -> ComparisonOutcome: ...


def _classify(value: ScalarValue) -> tuple[ValueKind, object]:
    """Normalize a scalar to ``(kind, normalized_value)``.

    Booleans and numeric-looking strings normalize to :class:`~decimal.Decimal`:
    Excel and the drivers move freely between ``1``, ``1.0`` and ``"1"`` for the
    same count, so treating those as one numeric kind reflects the data rather
    than coercing across kinds.
    """
    if value is None:
        return ValueKind.NULL, None
    if isinstance(value, bool):
        return ValueKind.NUMERIC, Decimal(1) if value else Decimal(0)
    if isinstance(value, int | float | Decimal):
        try:
            return ValueKind.NUMERIC, Decimal(str(value))
        except InvalidOperation:
            raise ComparisonError(f"value '{value}' is not a finite number") from None
    if isinstance(value, datetime):
        return ValueKind.TEMPORAL, value
    if isinstance(value, date):
        return ValueKind.TEMPORAL, datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ValueKind.NULL, None
        try:
            return ValueKind.NUMERIC, Decimal(text)
        except InvalidOperation:
            return ValueKind.TEXT, text
    return ValueKind.TEXT, str(value)


def to_decimal(value: ScalarValue, *, label: str) -> Decimal:
    """Normalize ``value`` to a Decimal or raise a clear comparison error."""
    kind, normalized = _classify(value)
    if kind is ValueKind.NULL:
        raise ComparisonError(f"{label} result is NULL and cannot be compared numerically")
    if kind is not ValueKind.NUMERIC:
        raise ComparisonError(f"{label} result is not numeric ({kind})")
    assert isinstance(normalized, Decimal)
    if not normalized.is_finite():
        raise ComparisonError(f"{label} result is not a finite number")
    return normalized


def variance_of(source: ScalarValue, target: ScalarValue) -> Decimal | None:
    """``source - target`` when both sides are numeric, otherwise ``None``.

    Never raises: variance is reported alongside failures and errors, so it must
    degrade to a blank cell rather than mask the real problem.
    """
    try:
        source_kind, source_value = _classify(source)
        target_kind, target_value = _classify(target)
    except ComparisonError:
        return None
    if source_kind is not ValueKind.NUMERIC or target_kind is not ValueKind.NUMERIC:
        return None
    assert isinstance(source_value, Decimal) and isinstance(target_value, Decimal)
    return source_value - target_value


def _require_comparable(
    source: ScalarValue, target: ScalarValue
) -> tuple[ValueKind, object, object]:
    source_kind, source_value = _classify(source)
    target_kind, target_value = _classify(target)
    if source_kind is ValueKind.NULL and target_kind is ValueKind.NULL:
        raise ComparisonError("both source and target results are NULL")
    if source_kind is ValueKind.NULL:
        raise ComparisonError("source result is NULL")
    if target_kind is ValueKind.NULL:
        raise ComparisonError("target result is NULL")
    if source_kind is not target_kind:
        raise ComparisonError(
            f"incompatible result types: source is {source_kind}, target is {target_kind}"
        )
    return source_kind, source_value, target_value


class EqualComparator:
    """Pass when the normalized source and target values are equal."""

    rule = ComparisonRule.EQUAL

    def compare(
        self, source: ScalarValue, target: ScalarValue, tolerance: Decimal
    ) -> ComparisonOutcome:
        kind, source_value, target_value = _require_comparable(source, target)
        passed = source_value == target_value
        if passed:
            remarks = f"Source and target {kind} results are equal."
        else:
            remarks = (
                f"Mismatch: source {_display(source_value)} != target {_display(target_value)}."
            )
        return ComparisonOutcome(passed, variance_of(source, target), remarks)


class ExpectedZeroComparator:
    """Pass when **both** the source and target results are numeric zero.

    ``expected_zero`` is for mismatch/orphan-count queries, where each side
    independently answers "how many rows are wrong?". Both sides are executed
    and both must answer zero. Requiring both — rather than picking one side —
    means a non-zero count can never be discarded unread. If only one side is
    meaningful for a given check, put the same query in both columns.
    """

    rule = ComparisonRule.EXPECTED_ZERO

    def compare(
        self, source: ScalarValue, target: ScalarValue, tolerance: Decimal
    ) -> ComparisonOutcome:
        source_value = to_decimal(source, label="Source")
        target_value = to_decimal(target, label="Target")
        source_zero = source_value == 0
        target_zero = target_value == 0
        variance = source_value - target_value
        if source_zero and target_zero:
            return ComparisonOutcome(True, variance, "Source and target mismatch counts are 0.")
        offenders = []
        if not source_zero:
            offenders.append(f"source {_display(source_value)}")
        if not target_zero:
            offenders.append(f"target {_display(target_value)}")
        return ComparisonOutcome(
            False, variance, f"Expected 0 on both sides but found {' and '.join(offenders)}."
        )


class NumericToleranceComparator:
    """Pass when ``abs(source - target) <= tolerance``."""

    rule = ComparisonRule.NUMERIC_TOLERANCE

    def compare(
        self, source: ScalarValue, target: ScalarValue, tolerance: Decimal
    ) -> ComparisonOutcome:
        if tolerance < 0:
            raise ComparisonError(f"tolerance must not be negative (got {_display(tolerance)})")
        source_value = to_decimal(source, label="Source")
        target_value = to_decimal(target, label="Target")
        variance = source_value - target_value
        difference = abs(variance)
        if difference <= tolerance:
            return ComparisonOutcome(
                True,
                variance,
                f"Difference {_display(difference)} is within tolerance {_display(tolerance)}.",
            )
        return ComparisonOutcome(
            False,
            variance,
            f"Difference {_display(difference)} exceeds tolerance {_display(tolerance)}.",
        )


_REGISTRY: dict[ComparisonRule, Comparator] = {
    comparator.rule: comparator
    for comparator in (
        EqualComparator(),
        ExpectedZeroComparator(),
        NumericToleranceComparator(),
    )
}


def get_comparator(rule: ComparisonRule) -> Comparator:
    """Look up the strategy for ``rule``."""
    try:
        return _REGISTRY[rule]
    except KeyError:  # pragma: no cover - ComparisonRule is a closed enum
        raise ComparisonError(f"No comparison strategy registered for '{rule}'") from None


def compare(
    rule: ComparisonRule,
    source: ScalarValue,
    target: ScalarValue,
    tolerance: Decimal = Decimal(0),
) -> ComparisonOutcome:
    """Apply ``rule`` to one source/target pair."""
    return get_comparator(rule).compare(source, target, tolerance)


def _display(value: object) -> str:
    """Render a normalized value without exponent noise."""
    if isinstance(value, Decimal):
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            try:
                return str(normalized.quantize(Decimal(1)))
            except InvalidOperation:  # pragma: no cover - very large magnitudes
                return str(normalized)
        return format(normalized, "f")
    return str(value)
