"""Reads test cases from a workbook according to a schema.

Columns are located by *header text* declared in the schema, never by column
letter or fixed position, so re-ordering or inserting columns in the template
changes nothing here.

Failures are separated by blast radius:

* **Workbook-level** problems (missing sheet, missing required column,
  duplicate test-case ids) raise :class:`WorkbookError` and stop the run before
  a single query executes.
* **Row-level** problems (a bad enum, a missing required value) are collected
  as :class:`InvalidRow` so one malformed row is reported as ``ERROR`` while
  every other row still runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..errors import WorkbookError
from ..models import (
    ComparisonRule,
    DatabaseType,
    ExecutionScope,
    FieldDefinition,
    FieldType,
    TestCase,
    WorkbookSchema,
)

__all__ = ["InvalidRow", "SkippedRow", "WorkbookRead", "read_workbook", "validate_workbook"]

_TRUE_LITERALS = frozenset({"true", "yes", "y", "1", "t", "x", "on", "enabled"})
_FALSE_LITERALS = frozenset({"false", "no", "n", "0", "f", "off", "disabled"})


@dataclass(frozen=True, slots=True)
class InvalidRow:
    """A row that could not be turned into a test case."""

    row_number: int
    test_case_id: str
    message: str


@dataclass(frozen=True, slots=True)
class SkippedRow:
    """A row deliberately not executed (``Enabled`` is false)."""

    row_number: int
    test_case_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class WorkbookRead:
    """Everything one pass over the test-case sheet produced."""

    test_cases: list[TestCase] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)
    invalid: list[InvalidRow] = field(default_factory=list)
    column_map: dict[str, int] = field(default_factory=dict)


def read_workbook(path: str | Path, schema: WorkbookSchema) -> WorkbookRead:
    """Read enabled test cases from ``path`` using ``schema``.

    Opens the workbook read-only. The input file is never modified.
    """
    workbook_path = Path(path)
    if not workbook_path.is_file():
        raise WorkbookError(f"Workbook not found: {workbook_path}")
    try:
        workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    except WorkbookError:
        raise
    except Exception as exc:
        raise WorkbookError(f"Cannot open workbook '{workbook_path.name}': {exc}") from None
    try:
        if schema.sheet_name not in workbook.sheetnames:
            available = ", ".join(workbook.sheetnames) or "<none>"
            raise WorkbookError(
                f"Sheet '{schema.sheet_name}' not found in '{workbook_path.name}'. "
                f"Available sheets: {available}"
            )
        sheet = workbook[schema.sheet_name]
        rows = [tuple(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()

    column_map = _map_columns(rows, schema, workbook_path.name)
    return _read_rows(rows, schema, column_map)


def validate_workbook(path: str | Path, schema: WorkbookSchema) -> WorkbookRead:
    """Validate a workbook against a schema without connecting to anything.

    Raises :class:`WorkbookError` for structural problems; row-level problems
    are returned in :attr:`WorkbookRead.invalid` for the caller to report.
    """
    return read_workbook(path, schema)


def _map_columns(
    rows: list[tuple[Any, ...]], schema: WorkbookSchema, workbook_name: str
) -> dict[str, int]:
    """Locate each schema field's column by matching header text."""
    if len(rows) < schema.header_row:
        raise WorkbookError(
            f"Sheet '{schema.sheet_name}' in '{workbook_name}' has {len(rows)} row(s); "
            f"the schema expects headers on row {schema.header_row}"
        )
    header_cells = rows[schema.header_row - 1]

    seen: dict[str, int] = {}
    for index, raw in enumerate(header_cells, start=1):
        if raw is None:
            continue
        normalized = _normalize_header(str(raw))
        if not normalized:
            continue
        if normalized in seen:
            raise WorkbookError(
                f"Sheet '{schema.sheet_name}' has duplicate header '{str(raw).strip()}' in "
                f"columns {seen[normalized]} and {index}"
            )
        seen[normalized] = index

    column_map: dict[str, int] = {}
    missing: list[str] = []
    for definition in schema.fields.values():
        index = seen.get(_normalize_header(definition.header))
        if index is None:
            if definition.required:
                missing.append(f"{definition.header} ({definition.name})")
            continue
        column_map[definition.name] = index

    if missing:
        raise WorkbookError(
            f"Sheet '{schema.sheet_name}' in '{workbook_name}' is missing required "
            f"column(s): {', '.join(missing)}"
        )
    return column_map


def _read_rows(
    rows: list[tuple[Any, ...]], schema: WorkbookSchema, column_map: dict[str, int]
) -> WorkbookRead:
    result = WorkbookRead(column_map=column_map)
    seen_ids: dict[str, int] = {}
    duplicates: list[str] = []

    for row_number in range(schema.first_data_row, len(rows) + 1):
        row = rows[row_number - 1]
        if _is_blank(row):
            continue

        raw_id = _cell(row, column_map.get("test_case_id"))
        test_case_id = str(raw_id).strip() if raw_id is not None else ""
        if not test_case_id:
            result.invalid.append(InvalidRow(row_number, "", "Test case id is empty but required"))
            continue
        if test_case_id in seen_ids:
            duplicates.append(f"'{test_case_id}' (rows {seen_ids[test_case_id]} and {row_number})")
            continue
        seen_ids[test_case_id] = row_number

        try:
            values = _read_field_values(row, schema, column_map)
        except WorkbookError as exc:
            result.invalid.append(InvalidRow(row_number, test_case_id, str(exc)))
            continue

        if not values["enabled"]:
            result.skipped.append(SkippedRow(row_number, test_case_id, _disabled_reason(values)))
            continue

        result.test_cases.append(_build_test_case(test_case_id, row_number, values, schema))

    if duplicates:
        raise WorkbookError(
            f"Sheet '{schema.sheet_name}' contains duplicate test case id(s): "
            f"{', '.join(duplicates)}"
        )
    return result


#: Fields that belong to one side only. A test that does not use that side
#: must leave them empty, and is not asked for them.
_SOURCE_SIDE_FIELDS = frozenset({"source_connection", "source_sql"})
_TARGET_SIDE_FIELDS = frozenset({"target_connection", "target_sql"})


def _scope_for(
    row: tuple[Any, ...], schema: WorkbookSchema, column_map: dict[str, int]
) -> ExecutionScope:
    """The scope this row declares, read before anything else depends on it."""
    definition = schema.fields.get("execution_scope")
    if definition is None or not definition.read:
        return ExecutionScope.SOURCE_TARGET
    raw = _cell(row, column_map.get("execution_scope"))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if definition.required:
            raise WorkbookError(f"Column '{definition.header}' is required but empty")
        default = _default_for(definition)
        return ExecutionScope(default) if default else ExecutionScope.SOURCE_TARGET
    return ExecutionScope(_coerce(raw, definition))


#: A disabled row's own words are worth more than the framework's. Capped so
#: one very long cell cannot crowd out the rest of a result sheet.
_DISABLED_REASON_CHARS = 300


def _disabled_reason(values: dict[str, Any]) -> str:
    """Why this row was skipped, quoting the workbook when it says.

    Whoever turned a test off usually wrote down why, and that note is more
    use to the next reader than "disabled" repeated down the column. When the
    schema maps no name field, or the cell is empty, the plain statement is
    all there is to say.
    """
    note = values.get("test_name")
    text = " ".join(str(note).split()) if note is not None else ""
    if not text:
        return "Disabled in the workbook"
    if len(text) > _DISABLED_REASON_CHARS:
        text = f"{text[:_DISABLED_REASON_CHARS].rstrip()}..."
    return f"Disabled in the workbook: {text}"


def _read_field_values(
    row: tuple[Any, ...], schema: WorkbookSchema, column_map: dict[str, int]
) -> dict[str, Any]:
    scope = _scope_for(row, schema, column_map)
    values: dict[str, Any] = {"execution_scope": scope}
    for definition in schema.fields.values():
        if not definition.read or definition.name == "execution_scope":
            continue
        # A side the scope does not name is neither required nor permitted:
        # a populated unused side means the row and its scope disagree, and
        # guessing which one is right would run a query nobody asked for.
        unused = (definition.name in _SOURCE_SIDE_FIELDS and not scope.uses_source) or (
            definition.name in _TARGET_SIDE_FIELDS and not scope.uses_target
        )
        raw = _cell(row, column_map.get(definition.name))
        blank = raw is None or (isinstance(raw, str) and not raw.strip())
        if unused:
            if not blank:
                raise WorkbookError(
                    f"Column '{definition.header}' must be empty when "
                    f"Execution_Scope is {scope.value}"
                )
            values[definition.name] = ""
            continue
        if blank:
            if definition.required:
                raise WorkbookError(f"Column '{definition.header}' is required but empty")
            values[definition.name] = _default_for(definition)
            continue
        values[definition.name] = _coerce(raw, definition)
    return values


def _build_test_case(
    test_case_id: str, row_number: int, values: dict[str, Any], schema: WorkbookSchema
) -> TestCase:
    core = {
        "test_case_id",
        "enabled",
        "execution_scope",
        "source_type",
        "source_connection",
        "target_connection",
        "source_sql",
        "target_sql",
        "comparison_rule",
        "tolerance",
        "timeout_seconds",
    }
    extras = {
        name: value
        for name, value in values.items()
        if name not in core and schema.fields[name].read
    }
    return TestCase(
        test_case_id=test_case_id,
        row_number=row_number,
        source_type=(DatabaseType(values["source_type"]) if values.get("source_type") else None),
        source_connection=str(values["source_connection"]),
        target_connection=str(values["target_connection"]),
        source_sql=str(values["source_sql"]),
        target_sql=str(values["target_sql"]),
        execution_scope=values["execution_scope"],
        comparison_rule=ComparisonRule(values.get("comparison_rule") or ComparisonRule.EQUAL),
        tolerance=_as_decimal(values.get("tolerance")),
        timeout_seconds=int(values.get("timeout_seconds") or 0),
        enabled=True,
        extras=extras,
    )


def _default_for(definition: FieldDefinition) -> Any:
    if definition.default is not None:
        return definition.default
    if definition.name == "enabled":
        return True
    return None


def _coerce(raw: Any, definition: FieldDefinition) -> Any:
    """Convert one cell to the type the schema declares, or explain why not."""
    header = definition.header
    match definition.type:
        case FieldType.BOOLEAN:
            return _coerce_boolean(raw, header)
        case FieldType.INTEGER:
            return _coerce_integer(raw, header)
        case FieldType.DECIMAL:
            return _coerce_decimal(raw, header)
        case FieldType.ENUM:
            return _coerce_enum(raw, definition)
        case FieldType.SQL:
            return str(raw).strip()
        case FieldType.DATETIME:
            if isinstance(raw, datetime | date):
                return raw
            raise WorkbookError(f"Column '{header}' must contain a date/time value")
        case _:
            return str(raw).strip()


def _coerce_boolean(raw: Any, header: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        if raw in (0, 1):
            return bool(raw)
        raise WorkbookError(f"Column '{header}' must be TRUE or FALSE (found {raw})")
    text = str(raw).strip().casefold()
    if text in _TRUE_LITERALS:
        return True
    if text in _FALSE_LITERALS:
        return False
    raise WorkbookError(f"Column '{header}' must be TRUE or FALSE (found '{raw}')")


def _coerce_integer(raw: Any, header: str) -> int:
    if isinstance(raw, bool):
        raise WorkbookError(f"Column '{header}' must be a whole number (found '{raw}')")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw.is_integer():
            return int(raw)
        raise WorkbookError(f"Column '{header}' must be a whole number (found {raw})")
    try:
        return int(str(raw).strip())
    except ValueError:
        raise WorkbookError(f"Column '{header}' must be a whole number (found '{raw}')") from None


def _coerce_decimal(raw: Any, header: str) -> Decimal:
    if isinstance(raw, bool):
        raise WorkbookError(f"Column '{header}' must be a number (found '{raw}')")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise WorkbookError(f"Column '{header}' must be a number (found '{raw}')") from None
    if not value.is_finite():
        raise WorkbookError(f"Column '{header}' must be a finite number (found '{raw}')")
    return value


def _coerce_enum(raw: Any, definition: FieldDefinition) -> str:
    text = str(raw).strip()
    for allowed in definition.allowed_values:
        if text.casefold() == allowed.casefold():
            return allowed
    allowed_display = ", ".join(v for v in definition.allowed_values if v) or "<none>"
    raise WorkbookError(
        f"Column '{definition.header}' has unsupported value '{text}' (allowed: {allowed_display})"
    )


def _as_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:  # pragma: no cover - guarded by _coerce_decimal
        return Decimal(0)


def _cell(row: tuple[Any, ...], index: int | None) -> Any:
    if index is None or index > len(row):
        return None
    return row[index - 1]


def _is_blank(row: tuple[Any, ...]) -> bool:
    return all(value is None or (isinstance(value, str) and not value.strip()) for value in row)


def _normalize_header(header: str) -> str:
    return " ".join(header.split()).casefold()
