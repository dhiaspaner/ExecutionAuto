"""Reading and validating the ``Test Cases`` sheet.

Everything a test *is* comes from this sheet; nothing about how to connect
does. Columns are found by exact header name, so the template can be
re-ordered, widened or re-styled without touching Python.

Validation is split by blast radius:

* A missing required **column** stops the run. There is no sensible per-row
  answer to "this workbook has no ``Source_SQL`` column".
* A bad **row** becomes a :class:`DefinitionProblem` and is reported as
  ``CONFIG ERROR`` against that row alone, so one malformed test never
  prevents the other two hundred from running.

Disabled rows are read but not validated beyond their identity. A row someone
has switched off should never be able to block a run, and it never reaches a
database.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from ..errors import SqlValidationError, TypeConversionError, WorkbookError
from ..evaluation.comparisons import (
    TWO_SIDED_COMPARISONS,
    requires_expected_value,
    supported_result_types,
)
from ..evaluation.normalization import NormalizedValue, normalize
from ..models import (
    ComparisonType,
    ErrorCode,
    ExecutionScope,
    Platform,
    ResultType,
    TestStatus,
)
from ..security.sql_guard import assert_read_only
from .columns import (
    DEFINITION_COLUMNS,
    LEGACY_COLUMNS,
    OUTPUT_COLUMNS,
    REQUIRED_DEFINITION_COLUMNS,
    REQUIRED_OUTPUT_COLUMNS,
    normalize_header,
)
from .sheetio import (
    cell,
    find_sheet,
    is_blank_row,
    locate_header_row,
    map_headers,
    sheet_rows,
    text_of,
)

__all__ = [
    "KNOWN_PROFILE_SECTIONS",
    "DefinitionProblem",
    "TestCaseSheet",
    "TestDefinition",
    "read_test_cases",
]

#: The TOML tables a workbook cell may name. The workbook chooses *which*
#: configured connection to use; it never carries the connection itself.
KNOWN_PROFILE_SECTIONS: tuple[str, ...] = ("source", "target")

#: Which code names each side's missing / unexpected condition.
_SECTION_CODES: dict[str, tuple[ErrorCode, ErrorCode]] = {
    "Source": (ErrorCode.MISSING_SOURCE_SECTION, ErrorCode.UNEXPECTED_SOURCE_SECTION),
    "Target": (ErrorCode.MISSING_TARGET_SECTION, ErrorCode.UNEXPECTED_TARGET_SECTION),
}
_SQL_CODES: dict[str, tuple[ErrorCode, ErrorCode]] = {
    "Source": (ErrorCode.MISSING_SOURCE_SQL, ErrorCode.UNEXPECTED_SOURCE_SQL),
    "Target": (ErrorCode.MISSING_TARGET_SQL, ErrorCode.UNEXPECTED_TARGET_SQL),
}

_YES_WORDS = frozenset({"yes", "y", "true", "t", "1", "enabled"})
_NO_WORDS = frozenset({"no", "n", "false", "f", "0", "disabled"})


@dataclass(frozen=True, slots=True)
class TestDefinition:
    """One validated row of the ``Test Cases`` sheet."""

    test_id: str
    row_number: int
    enabled: bool
    scope: ExecutionScope
    comparison: ComparisonType
    result_type: ResultType
    source_section: str = ""
    target_section: str = ""
    source_sql: str = ""
    target_sql: str = ""
    expected_value: NormalizedValue | None = None
    expected_raw: Any = None
    absolute_tolerance: Decimal = Decimal(0)
    percentage_tolerance: Decimal = Decimal(0)
    #: Descriptive columns, kept verbatim for reports and observations.
    details: dict[str, str] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.details.get("Domain", "")

    @property
    def name(self) -> str:
        return self.details.get("Test_Name", "")

    @property
    def severity(self) -> str:
        return self.details.get("Severity", "")

    def sections(self) -> tuple[str, ...]:
        """The TOML sections this test needs opened, and no others."""
        needed: list[str] = []
        if self.enabled and self.scope.uses_source and self.source_section:
            needed.append(self.source_section)
        if self.enabled and self.scope.uses_target and self.target_section:
            needed.append(self.target_section)
        return tuple(dict.fromkeys(needed))


@dataclass(frozen=True, slots=True)
class DefinitionProblem:
    """A row that cannot be executed as written."""

    row_number: int
    test_id: str
    code: ErrorCode
    message: str
    platform: Platform = Platform.WORKBOOK
    status: TestStatus = TestStatus.CONFIG_ERROR


@dataclass(frozen=True, slots=True)
class TestCaseSheet:
    """The result of one pass over the ``Test Cases`` sheet."""

    sheet_name: str
    header_row: int
    first_data_row: int
    columns: dict[str, int]
    definitions: tuple[TestDefinition, ...] = ()
    problems: tuple[DefinitionProblem, ...] = ()
    missing_output_columns: tuple[str, ...] = ()
    legacy_columns: tuple[str, ...] = ()

    @property
    def enabled(self) -> tuple[TestDefinition, ...]:
        return tuple(d for d in self.definitions if d.enabled)

    @property
    def disabled(self) -> tuple[TestDefinition, ...]:
        return tuple(d for d in self.definitions if not d.enabled)

    def required_sections(self) -> tuple[str, ...]:
        """Every TOML section the enabled tests need. Nothing else is opened."""
        needed: list[str] = []
        for definition in self.enabled:
            needed.extend(definition.sections())
        return tuple(dict.fromkeys(needed))

    def column_of(self, header: str) -> int | None:
        return self.columns.get(normalize_header(header))


def read_test_cases(
    workbook: Any,
    *,
    sheet_name: str,
    known_sections: Sequence[str] = KNOWN_PROFILE_SECTIONS,
) -> TestCaseSheet:
    """Read, validate and return every row of the test-case sheet."""
    sheet = find_sheet(workbook, sheet_name)
    if sheet is None:
        available = ", ".join(workbook.sheetnames) or "<none>"
        raise WorkbookError(f"Sheet '{sheet_name}' was not found. Available sheets: {available}")

    rows = sheet_rows(sheet)
    header_row = locate_header_row(rows, DEFINITION_COLUMNS, must_contain="Test_ID")
    if header_row is None:
        raise WorkbookError(
            f"Sheet '{sheet.title}' has no header row containing 'Test_ID' in its first "
            f"rows, so its columns cannot be identified."
        )

    columns = map_headers(rows[header_row - 1])
    _require_columns(sheet.title, columns)

    missing_output = tuple(
        header for header in OUTPUT_COLUMNS if normalize_header(header) not in columns
    )
    legacy = tuple(header for header in LEGACY_COLUMNS if normalize_header(header) in columns)

    definitions: list[TestDefinition] = []
    problems: list[DefinitionProblem] = []
    seen: dict[str, int] = {}

    for row_number in range(header_row + 1, len(rows) + 1):
        row = rows[row_number - 1]
        if is_blank_row(row):
            continue
        outcome = _read_row(row, row_number, columns, seen, known_sections)
        if isinstance(outcome, DefinitionProblem):
            problems.append(outcome)
        elif outcome is not None:
            definitions.append(outcome)

    return TestCaseSheet(
        sheet_name=sheet.title,
        header_row=header_row,
        first_data_row=header_row + 1,
        columns=columns,
        definitions=tuple(definitions),
        problems=tuple(problems),
        missing_output_columns=missing_output,
        legacy_columns=legacy,
    )


def _require_columns(sheet_title: str, columns: dict[str, int]) -> None:
    missing = [
        header for header in REQUIRED_DEFINITION_COLUMNS if normalize_header(header) not in columns
    ]
    if missing:
        raise WorkbookError(
            f"Sheet '{sheet_title}' is missing required column(s): {', '.join(missing)}"
        )
    missing_output = [
        header for header in REQUIRED_OUTPUT_COLUMNS if normalize_header(header) not in columns
    ]
    if missing_output:
        raise WorkbookError(
            f"Sheet '{sheet_title}' has nowhere to record the outcome: it is missing "
            f"{', '.join(missing_output)}."
        )


def _value(row: Sequence[Any], columns: dict[str, int], header: str) -> Any:
    return cell(row, columns.get(normalize_header(header)))


def _text(row: Sequence[Any], columns: dict[str, int], header: str) -> str:
    return text_of(_value(row, columns, header))


def _read_row(
    row: Sequence[Any],
    row_number: int,
    columns: dict[str, int],
    seen: dict[str, int],
    known_sections: Sequence[str],
) -> TestDefinition | DefinitionProblem | None:
    test_id = _text(row, columns, "Test_ID")
    if not test_id:
        return DefinitionProblem(
            row_number, "", ErrorCode.MISSING_TEST_ID, "Test_ID is empty but required."
        )
    if test_id in seen:
        return DefinitionProblem(
            row_number,
            test_id,
            ErrorCode.DUPLICATE_TEST_ID,
            f"Test_ID '{test_id}' is already used on row {seen[test_id]}; ids must be unique.",
        )
    seen[test_id] = row_number

    enabled_text = _text(row, columns, "Enabled")
    enabled = _as_enabled(enabled_text)
    if enabled is None:
        return DefinitionProblem(
            row_number,
            test_id,
            ErrorCode.INVALID_ENABLED,
            f"Enabled must be Yes or No (found '{enabled_text}').",
        )

    details = {
        header: _text(row, columns, header)
        for header in DEFINITION_COLUMNS
        if header
        not in {
            "Test_ID",
            "Enabled",
            "Source_SQL",
            "Target_SQL",
            "Expected_Value",
            "Absolute_Tolerance",
            "Percentage_Tolerance",
        }
    }

    if not enabled:
        # A switched-off row is recorded and left alone. Validating it could
        # only ever stop a run over a test nobody asked to perform.
        return TestDefinition(
            test_id=test_id,
            row_number=row_number,
            enabled=False,
            scope=_as_scope(_text(row, columns, "Execution_Scope")) or ExecutionScope.SOURCE_TARGET,
            comparison=_as_comparison(_text(row, columns, "Comparison_Type"))
            or ComparisonType.NO_COMPARISON,
            result_type=_as_result_type(_text(row, columns, "Result_Type")) or ResultType.TEXT,
            details=details,
        )

    scope = _as_scope(_text(row, columns, "Execution_Scope"))
    if scope is None:
        allowed = ", ".join(s.value for s in ExecutionScope)
        return DefinitionProblem(
            row_number,
            test_id,
            ErrorCode.INVALID_SCOPE,
            f"Execution_Scope '{_text(row, columns, 'Execution_Scope')}' is not one of: {allowed}.",
        )

    comparison = _as_comparison(_text(row, columns, "Comparison_Type"))
    if comparison is None:
        allowed = ", ".join(c.value for c in ComparisonType)
        return DefinitionProblem(
            row_number,
            test_id,
            ErrorCode.INVALID_COMPARISON_TYPE,
            f"Comparison_Type '{_text(row, columns, 'Comparison_Type')}' is not implemented. "
            f"Supported: {allowed}.",
        )

    result_type = _as_result_type(_text(row, columns, "Result_Type"))
    if result_type is None:
        allowed = ", ".join(r.value for r in ResultType)
        return DefinitionProblem(
            row_number,
            test_id,
            ErrorCode.INVALID_RESULT_TYPE,
            f"Result_Type '{_text(row, columns, 'Result_Type')}' is not one of: {allowed}.",
        )

    supported = supported_result_types(comparison)
    if result_type not in supported:
        allowed = ", ".join(sorted(r.value for r in supported))
        return DefinitionProblem(
            row_number,
            test_id,
            ErrorCode.INVALID_RESULT_TYPE,
            f"Comparison_Type {comparison.value} needs a Result_Type of {allowed}, "
            f"not {result_type.value}.",
        )

    if comparison in TWO_SIDED_COMPARISONS and scope is not ExecutionScope.SOURCE_TARGET:
        return DefinitionProblem(
            row_number,
            test_id,
            ErrorCode.INVALID_COMPARISON_TYPE,
            f"Comparison_Type {comparison.value} compares two sides, so it needs "
            f"Execution_Scope SOURCE_TARGET, not {scope.value}.",
        )

    tolerances = _tolerances(row, columns)
    if isinstance(tolerances, str):
        return DefinitionProblem(row_number, test_id, ErrorCode.INVALID_TOLERANCE, tolerances)
    absolute_tolerance, percentage_tolerance = tolerances

    scope_problem = _check_scope_columns(row, columns, row_number, test_id, scope, known_sections)
    if scope_problem is not None:
        return scope_problem

    sql_problem = _check_sql(row, columns, row_number, test_id, scope)
    if sql_problem is not None:
        return sql_problem

    expected_raw = _value(row, columns, "Expected_Value")
    expected: NormalizedValue | None = None
    if requires_expected_value(comparison, scope) and _is_blank(expected_raw):
        return DefinitionProblem(
            row_number,
            test_id,
            ErrorCode.MISSING_EXPECTED_VALUE,
            f"Comparison_Type {comparison.value} needs an Expected_Value.",
        )
    if not _is_blank(expected_raw):
        try:
            expected = normalize(expected_raw, result_type, label="Expected_Value")
        except TypeConversionError as exc:
            return DefinitionProblem(row_number, test_id, ErrorCode.TYPE_CONVERSION_ERROR, str(exc))

    return TestDefinition(
        test_id=test_id,
        row_number=row_number,
        enabled=True,
        scope=scope,
        comparison=comparison,
        result_type=result_type,
        source_section=_section(row, columns, "Source_Profile_Section"),
        target_section=_section(row, columns, "Target_Profile_Section"),
        source_sql=_text(row, columns, "Source_SQL"),
        target_sql=_text(row, columns, "Target_SQL"),
        expected_value=expected,
        expected_raw=expected_raw,
        absolute_tolerance=absolute_tolerance,
        percentage_tolerance=percentage_tolerance,
        details=details,
    )


def _check_scope_columns(
    row: Sequence[Any],
    columns: dict[str, int],
    row_number: int,
    test_id: str,
    scope: ExecutionScope,
    known_sections: Sequence[str],
) -> DefinitionProblem | None:
    """Each scope names exactly the sides it uses, and leaves the other blank.

    The blank half is enforced, not ignored: a ``SOURCE_ONLY`` row carrying a
    target query is ambiguous about whether the target was meant to run, and
    the safe reading of an ambiguous test is to refuse it.
    """
    known = {name.casefold() for name in known_sections}
    for side, uses, missing_code, unexpected_code in (
        (
            "Source",
            scope.uses_source,
            ErrorCode.MISSING_SOURCE_SECTION,
            ErrorCode.UNEXPECTED_SOURCE_SECTION,
        ),
        (
            "Target",
            scope.uses_target,
            ErrorCode.MISSING_TARGET_SECTION,
            ErrorCode.UNEXPECTED_TARGET_SECTION,
        ),
    ):
        header = f"{side}_Profile_Section"
        value = _text(row, columns, header)
        if uses:
            if not value:
                return DefinitionProblem(
                    row_number,
                    test_id,
                    missing_code,
                    f"Execution_Scope {scope.value} requires {header}.",
                )
            if value.strip().strip("[]").casefold() not in known:
                allowed = ", ".join(sorted(known))
                return DefinitionProblem(
                    row_number,
                    test_id,
                    missing_code,
                    f"{header} names '{value}', which is not a section of the profile "
                    f"(available: {allowed}).",
                )
        elif value:
            return DefinitionProblem(
                row_number,
                test_id,
                unexpected_code,
                f"Execution_Scope {scope.value} never opens the {side.lower()} database, "
                f"so {header} must be blank (found '{value}').",
            )
    return None


def _check_sql(
    row: Sequence[Any],
    columns: dict[str, int],
    row_number: int,
    test_id: str,
    scope: ExecutionScope,
) -> DefinitionProblem | None:
    for side, uses, missing_code, unexpected_code in (
        (
            "Source",
            scope.uses_source,
            ErrorCode.MISSING_SOURCE_SQL,
            ErrorCode.UNEXPECTED_SOURCE_SQL,
        ),
        (
            "Target",
            scope.uses_target,
            ErrorCode.MISSING_TARGET_SQL,
            ErrorCode.UNEXPECTED_TARGET_SQL,
        ),
    ):
        header = f"{side}_SQL"
        sql = _text(row, columns, header)
        if uses:
            if not sql:
                return DefinitionProblem(
                    row_number,
                    test_id,
                    missing_code,
                    f"Execution_Scope {scope.value} requires {header}.",
                )
            try:
                assert_read_only(sql, label=header)
            except SqlValidationError as exc:
                return DefinitionProblem(
                    row_number,
                    test_id,
                    ErrorCode.UNSAFE_SQL,
                    str(exc),
                    platform=Platform.SOURCE if side == "Source" else Platform.TARGET,
                )
        elif sql:
            return DefinitionProblem(
                row_number,
                test_id,
                unexpected_code,
                f"Execution_Scope {scope.value} never runs a {side.lower()} query, so "
                f"{header} must be blank.",
            )
    return None


def _tolerances(row: Sequence[Any], columns: dict[str, int]) -> tuple[Decimal, Decimal] | str:
    parsed: list[Decimal] = []
    for header in ("Absolute_Tolerance", "Percentage_Tolerance"):
        raw = _value(row, columns, header)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            parsed.append(Decimal(0))
            continue
        if isinstance(raw, bool):
            return f"{header} must be a number of 0 or more (found '{raw}')."
        try:
            value = Decimal(str(raw).strip())
        except InvalidOperation:
            return f"{header} must be a number of 0 or more (found '{raw}')."
        if not value.is_finite() or value < 0:
            return f"{header} must be a number of 0 or more (found '{raw}')."
        parsed.append(value)
    return parsed[0], parsed[1]


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _section(row: Sequence[Any], columns: dict[str, int], header: str) -> str:
    return _text(row, columns, header).strip().strip("[]").casefold()


def _as_enabled(text: str) -> bool | None:
    folded = text.strip().casefold()
    if folded in _YES_WORDS:
        return True
    if folded in _NO_WORDS:
        return False
    return None


def _as_scope(text: str) -> ExecutionScope | None:
    return _as_member(ExecutionScope, text)


def _as_comparison(text: str) -> ComparisonType | None:
    return _as_member(ComparisonType, text)


def _as_result_type(text: str) -> ResultType | None:
    return _as_member(ResultType, text)


def _as_member[T](enum: type[T], text: str) -> T | None:
    candidate = text.strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return enum(candidate)  # type: ignore[call-arg]
    except ValueError:
        return None
