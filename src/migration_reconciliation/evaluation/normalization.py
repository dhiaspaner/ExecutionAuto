"""Turning a driver's scalar into the type the workbook declared.

``Result_Type`` says what a query is supposed to return, and this module holds
that promise to the letter. Every conversion here is strict in one direction:
it accepts the shapes a database driver genuinely uses for a value of that type
— a count as ``5``, ``5.0`` or ``"5"`` — and refuses everything else.

The refusals matter more than the acceptances. Converting the word ``unknown``
to ``0`` or a blank cell to ``False`` would turn a broken query into a passing
reconciliation, so a value that does not clearly mean what the type says
produces :class:`~migration_reconciliation.errors.TypeConversionError` and the
test case is reported as ``ERROR`` with ``TYPE_CONVERSION_ERROR``.

NULL is not a value of any type. A query that returns nothing meaningful is a
query to fix — wrap the aggregate in ``COALESCE`` or ``NVL`` when zero is the
intended answer — so NULL is refused rather than defaulted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from ..errors import TypeConversionError
from ..models import ResultType, ScalarValue

__all__ = ["NormalizedValue", "display", "is_zero", "normalize"]

#: What a normalized scalar can be, once its declared type has been applied.
type NormalizedValue = int | Decimal | str | bool | datetime

_TRUE_WORDS = frozenset({"true", "t", "yes", "y", "1"})
_FALSE_WORDS = frozenset({"false", "f", "no", "n", "0"})


def normalize(value: ScalarValue, result_type: ResultType, *, label: str) -> NormalizedValue:
    """Read ``value`` as ``result_type``, or explain precisely why it cannot be."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise TypeConversionError(
            f"{label} is NULL or empty, so it cannot be read as {result_type.value}."
        )
    match result_type:
        case ResultType.INTEGER:
            return _as_integer(value, result_type, label)
        case ResultType.NUMBER:
            return _as_number(value, result_type, label)
        case ResultType.TEXT:
            return _as_text(value)
        case ResultType.BOOLEAN:
            return _as_boolean(value, result_type, label)
        case ResultType.DATETIME:
            return _as_datetime(value, result_type, label)
    raise TypeConversionError(f"{label} has an unknown result type '{result_type}'.")


def _refuse(value: ScalarValue, result_type: ResultType, label: str) -> TypeConversionError:
    """A refusal names the type and the offending value, never the query."""
    return TypeConversionError(
        f"{label} is not a valid {result_type.value} value (found '{value}')."
    )


def _as_integer(value: ScalarValue, result_type: ResultType, label: str) -> int:
    # A boolean is an int in Python but never a count in a database, so
    # accepting it would let TRUE reconcile against 1.
    if isinstance(value, bool):
        raise _refuse(value, result_type, label)
    if isinstance(value, int):
        return value
    number = _to_decimal(value, result_type, label)
    if number != number.to_integral_value():
        raise _refuse(value, result_type, label)
    return int(number)


def _as_number(value: ScalarValue, result_type: ResultType, label: str) -> Decimal:
    if isinstance(value, bool):
        raise _refuse(value, result_type, label)
    return _to_decimal(value, result_type, label)


def _to_decimal(value: ScalarValue, result_type: ResultType, label: str) -> Decimal:
    """Decimal is the comparison type: binary floats do not total money correctly."""
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int | float):
        candidate = Decimal(str(value))
    elif isinstance(value, str):
        try:
            candidate = Decimal(value.strip())
        except InvalidOperation:
            raise _refuse(value, result_type, label) from None
    else:
        raise _refuse(value, result_type, label)
    if not candidate.is_finite():
        raise _refuse(value, result_type, label)
    return candidate


def _as_text(value: ScalarValue) -> str:
    """Trim the ends only. Inner spacing can be the difference being tested."""
    return value.strip() if isinstance(value, str) else str(value).strip()


def _as_boolean(value: ScalarValue, result_type: ResultType, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | Decimal) or (isinstance(value, float) and value.is_integer()):
        if int(value) in (0, 1):
            return bool(int(value))
        raise _refuse(value, result_type, label)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    raise _refuse(value, result_type, label)


def _as_datetime(value: ScalarValue, result_type: ResultType, label: str) -> datetime:
    """Always timezone-aware UTC, so two runs on two machines agree."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise _refuse(value, result_type, label) from None
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    raise _refuse(value, result_type, label)


def is_zero(value: NormalizedValue) -> bool:
    """True only for a numeric zero. Text and datetimes are never zero."""
    return isinstance(value, int | Decimal) and not isinstance(value, bool) and value == 0


def display(value: object) -> str:
    """Render a normalized value for an observation, without exponent noise."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Decimal):
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            try:
                return str(normalized.quantize(Decimal(1)))
            except InvalidOperation:  # pragma: no cover - very large magnitudes
                return str(normalized)
        return format(normalized, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return "" if value is None else str(value)
