"""Internal domain models.

Every model here uses *stable semantic field names*. Excel header text never
appears in this module: the mapping from semantic name to spreadsheet header
lives exclusively in the workbook schema DSL (see :mod:`.workbook.schema`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

__all__ = [
    "ComparisonRule",
    "ComparisonType",
    "ConnectionIdentity",
    "DatabaseType",
    "ErrorCode",
    "ErrorSide",
    "ExecutionResult",
    "ExecutionScope",
    "ExecutionStatus",
    "FieldDefinition",
    "FieldType",
    "Platform",
    "ResultType",
    "RunSummary",
    "ScalarValue",
    "TestCase",
    "TestStatus",
    "WorkbookSchema",
]

#: A single reconciliation query result. Queries must return exactly one row
#: and one column, so only scalars ever cross the database boundary.
type ScalarValue = str | int | float | Decimal | bool | datetime | date | None


class DatabaseType(StrEnum):
    """Supported source database engines. The target is always SQL Server."""

    ORACLE = "oracle"
    SQLSERVER = "sqlserver"


#: Error code for SQL the database refused to compile. Lives here because both
#: the runner that writes it and the summary that counts it need the same
#: spelling.
SQL_SYNTAX_ERROR_CODE = "SQL_SYNTAX_ERROR"


class OnSyntaxError(StrEnum):
    """What a run does when a query will not compile.

    ``CONTINUE`` records that test as ``ERROR`` and runs every test whose SQL
    was sound, so one broken row costs one result rather than all of them.
    ``STOP`` executes nothing at all, for when a partial reconciliation would
    be worse than none.
    """

    CONTINUE = "continue"
    STOP = "stop"


class RunMode(StrEnum):
    """What a run is being asked to do.

    ``VALIDATE`` connects and asks the database to compile every query, then
    stops: it is the pre-execution check on its own, for confirming a workbook
    is sound without touching a reconciliation. ``EXECUTE`` does that same
    check first and, only if it passes, goes on to run the queries.
    """

    VALIDATE = "validate"
    EXECUTE = "execute"


class ExecutionScope(StrEnum):
    """Which database sides one test needs. Nothing else is ever opened."""

    SOURCE_TARGET = "SOURCE_TARGET"
    SOURCE_ONLY = "SOURCE_ONLY"
    TARGET_ONLY = "TARGET_ONLY"

    @property
    def uses_source(self) -> bool:
        return self is not ExecutionScope.TARGET_ONLY

    @property
    def uses_target(self) -> bool:
        return self is not ExecutionScope.SOURCE_ONLY


class ComparisonRule(StrEnum):
    """Strategies for deciding whether a source/target pair reconciles."""

    EQUAL = "equal"
    EXPECTED_ZERO = "expected_zero"
    NUMERIC_TOLERANCE = "numeric_tolerance"
    #: Record both sides and render no verdict. Never reported as a pass:
    #: nothing was compared, so there is nothing to have passed.
    NO_COMPARISON = "no_comparison"


class ExecutionStatus(StrEnum):
    """Outcome of one test case."""

    PASS = "PASS"
    FAIL = "FAIL"
    #: A value was recorded but not verified, because the test asked for no
    #: comparison. Distinct from SKIPPED, which means nothing ran at all.
    PROFILED = "PROFILED"
    #: The query compiled and was deliberately not executed, because the run
    #: was asked to validate only. A successful outcome, not a failure.
    VALIDATED = "VALIDATED"
    #: This query compiled, but another one did not, so the run stopped before
    #: executing anything. Distinct from SKIPPED (disabled in the workbook).
    NOT_EXECUTED = "NOT EXECUTED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class ErrorSide(StrEnum):
    """Which stage produced a failure. ``NONE`` renders as an empty cell."""

    SOURCE = "SOURCE"
    TARGET = "TARGET"
    COMPARISON = "COMPARISON"
    WORKBOOK = "WORKBOOK"
    NONE = ""


class FieldType(StrEnum):
    """Value types a schema field may declare."""

    STRING = "string"
    BOOLEAN = "boolean"
    ENUM = "enum"
    SQL = "sql"
    DECIMAL = "decimal"
    INTEGER = "integer"
    SCALAR = "scalar"
    DATETIME = "datetime"


@dataclass(frozen=True, slots=True)
class ConnectionIdentity:
    """Non-sensitive description of an established connection.

    Deliberately carries no password, no DSN and no full connection string, so
    that it is safe to log or place in a remarks cell.
    """

    connection_name: str
    database_type: DatabaseType
    server_description: str = ""
    database_name: str = ""
    account_name: str = ""
    product_version: str = ""

    def describe(self) -> str:
        parts = [f"{self.connection_name} ({self.database_type})"]
        if self.database_name:
            parts.append(f"db={self.database_name}")
        if self.account_name:
            parts.append(f"user={self.account_name}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    """One semantic field and the Excel header it is bound to."""

    name: str
    header: str
    type: FieldType
    required: bool = False
    read: bool = False
    write: bool = False
    default: object | None = None
    allowed_values: tuple[str, ...] = ()
    format: str | None = None
    timezone: str | None = None


@dataclass(frozen=True, slots=True)
class WorkbookSchema:
    """A validated workbook profile: sheet geometry plus field mappings."""

    schema_version: str
    profile_name: str
    sheet_name: str
    header_row: int
    first_data_row: int
    preserve_other_sheets: bool
    output_filename_pattern: str
    fields: Mapping[str, FieldDefinition]

    def field(self, name: str) -> FieldDefinition:
        try:
            return self.fields[name]
        except KeyError:  # pragma: no cover - guarded by schema validation
            raise KeyError(f"Schema '{self.profile_name}' has no field '{name}'") from None

    def readable_fields(self) -> tuple[FieldDefinition, ...]:
        return tuple(f for f in self.fields.values() if f.read)

    def writable_fields(self) -> tuple[FieldDefinition, ...]:
        return tuple(f for f in self.fields.values() if f.write)

    def is_writable(self, name: str) -> bool:
        definition = self.fields.get(name)
        return bool(definition and definition.write)


@dataclass(frozen=True, slots=True)
class TestCase:
    """One reconciliation row read from the workbook."""

    test_case_id: str
    row_number: int
    #: What the workbook asserts the source engine is. ``None`` when the sheet
    #: does not say, in which case the connection named by the row decides and
    #: nothing is asserted against it.
    source_type: DatabaseType | None
    source_connection: str
    target_connection: str
    source_sql: str
    target_sql: str
    #: Which sides this test needs. Nothing else is ever opened or queried.
    execution_scope: ExecutionScope = ExecutionScope.SOURCE_TARGET
    comparison_rule: ComparisonRule = ComparisonRule.EQUAL
    tolerance: Decimal = Decimal(0)
    #: ``0`` means no limit. See ``database.base.normalize_timeout``.
    timeout_seconds: int = 0
    enabled: bool = True
    #: Optional semantic fields (domain, entity, severity, ...) kept verbatim so
    #: new template columns need no Python change.
    extras: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Everything written back for one test case."""

    test_case_id: str
    row_number: int
    status: ExecutionStatus
    executed_at: datetime
    run_id: str
    duration_ms: int
    source_result: ScalarValue = None
    target_result: ScalarValue = None
    variance: Decimal | None = None
    remarks: str = ""
    error_side: ErrorSide = ErrorSide.NONE
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Aggregate outcome of one ``reconcile execute`` invocation."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    input_path: Path
    output_path: Path | None
    results: Sequence[ExecutionResult]

    def _count(self, status: ExecutionStatus) -> int:
        return sum(1 for r in self.results if r.status is status)

    @property
    def passed(self) -> int:
        return self._count(ExecutionStatus.PASS)

    @property
    def failed(self) -> int:
        return self._count(ExecutionStatus.FAIL)

    @property
    def errored(self) -> int:
        return self._count(ExecutionStatus.ERROR)

    @property
    def skipped(self) -> int:
        return self._count(ExecutionStatus.SKIPPED)

    @property
    def validated(self) -> int:
        """Queries that compiled in a validate-only run."""
        return self._count(ExecutionStatus.VALIDATED)

    @property
    def syntax_errors(self) -> int:
        """Queries the database refused to compile.

        Counted by error code rather than status: these are reported as
        ``ERROR`` like any other failure, because that is what they are — a
        test that produced no result. The code is what says the fault was in
        the SQL rather than in the data.
        """
        return sum(1 for r in self.results if r.error_code == SQL_SYNTAX_ERROR_CODE)

    @property
    def not_executed(self) -> int:
        """Queries that compiled but never ran, because another one did not."""
        return self._count(ExecutionStatus.NOT_EXECUTED)

    @property
    def stopped_by_validation(self) -> bool:
        """True when the pre-execution check stopped the run before executing."""
        return self.not_executed > 0

    @property
    def executed(self) -> int:
        return len(self.results) - self.skipped - self.not_executed - self.validated

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def is_clean(self) -> bool:
        """True when nothing failed, errored, or was refused before execution.

        A run stopped by the pre-execution check is never clean: no query was
        executed, so there is no evidence that anything reconciles.
        """
        return self.failed == 0 and self.errored == 0 and self.not_executed == 0


# ---------------------------------------------------------------------------
# Reconciliation workbook template v2
#
# The vocabulary below belongs to the TOML-driven template whose `Test Cases`
# sheet declares an execution scope, a comparison type and a result type per
# row. It is deliberately separate from the milestone-1 names above: the older
# `ComparisonRule` / `ExecutionStatus` pipeline keeps working unchanged while
# the two templates coexist.
# ---------------------------------------------------------------------------


class ComparisonType(StrEnum):
    """The comparisons the workbook may ask for.

    Every member maps to one Python function in
    :mod:`~migration_reconciliation.evaluation.comparisons`. A workbook can
    select a comparison; it can never define one.
    """

    EQUAL = "EQUAL"
    EQUAL_ABS_TOLERANCE = "EQUAL_ABS_TOLERANCE"
    EQUAL_PCT_TOLERANCE = "EQUAL_PCT_TOLERANCE"
    EXPECTED_EQUAL = "EXPECTED_EQUAL"
    EXPECTED_ZERO = "EXPECTED_ZERO"
    LESS_THAN_OR_EQUAL = "LESS_THAN_OR_EQUAL"
    GREATER_THAN_OR_EQUAL = "GREATER_THAN_OR_EQUAL"
    NON_ZERO = "NON_ZERO"
    BOOLEAN_TRUE = "BOOLEAN_TRUE"
    TEXT_CASE_INSENSITIVE_EQUAL = "TEXT_CASE_INSENSITIVE_EQUAL"
    NO_COMPARISON = "NO_COMPARISON"


class ResultType(StrEnum):
    """How a raw driver value is normalized before it is compared."""

    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    TEXT = "TEXT"
    BOOLEAN = "BOOLEAN"
    DATETIME = "DATETIME"


class TestStatus(StrEnum):
    """The small, stable status set written to ``Status``.

    Specific reasons live in ``Error_Code`` and the platform, never in extra
    statuses: a report that filters on nine values stays readable, and a new
    failure mode never needs a new status.

    ``SYNTAX ERROR`` is the one status that is not decided by executing
    anything. It means the pre-execution validation pass asked the database to
    compile that query and the database refused it, so the run stopped before
    any reconciliation SQL was executed at all.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    PROFILED = "PROFILED"
    #: Compiled and deliberately not executed, in a validate-only run.
    VALIDATED = "VALIDATED"
    ERROR = "ERROR"
    SYNTAX_ERROR = "SYNTAX ERROR"
    BLOCKED = "BLOCKED"
    CONFIG_ERROR = "CONFIG ERROR"
    NOT_EXECUTED = "NOT EXECUTED"
    DISABLED = "DISABLED"


class Platform(StrEnum):
    """Where a status was decided. Half of the observation matching key."""

    SOURCE = "SOURCE"
    TARGET = "TARGET"
    COMPARISON = "COMPARISON"
    WORKBOOK = "WORKBOOK"
    PROFILE = "PROFILE"
    EXECUTOR = "EXECUTOR"
    NONE = ""


class ErrorCode(StrEnum):
    """Stable internal reasons. Filterable, translatable, and never prose."""

    # Profile / configuration
    INVALID_PROFILE = "INVALID_PROFILE"
    FORBIDDEN_SECRET_KEY = "FORBIDDEN_SECRET_KEY"
    UNSUPPORTED_PROFILE_VERSION = "UNSUPPORTED_PROFILE_VERSION"
    UNSUPPORTED_TEMPLATE_VERSION = "UNSUPPORTED_TEMPLATE_VERSION"
    MISSING_CREDENTIALS = "MISSING_CREDENTIALS"
    MISSING_SOURCE_SECTION = "MISSING_SOURCE_SECTION"
    MISSING_TARGET_SECTION = "MISSING_TARGET_SECTION"
    UNEXPECTED_SOURCE_SECTION = "UNEXPECTED_SOURCE_SECTION"
    UNEXPECTED_TARGET_SECTION = "UNEXPECTED_TARGET_SECTION"

    # Test definition
    MISSING_TEST_ID = "MISSING_TEST_ID"
    DUPLICATE_TEST_ID = "DUPLICATE_TEST_ID"
    INVALID_ENABLED = "INVALID_ENABLED"
    INVALID_SCOPE = "INVALID_SCOPE"
    INVALID_COMPARISON_TYPE = "INVALID_COMPARISON_TYPE"
    INVALID_RESULT_TYPE = "INVALID_RESULT_TYPE"
    INVALID_TOLERANCE = "INVALID_TOLERANCE"
    MISSING_SOURCE_SQL = "MISSING_SOURCE_SQL"
    MISSING_TARGET_SQL = "MISSING_TARGET_SQL"
    UNEXPECTED_SOURCE_SQL = "UNEXPECTED_SOURCE_SQL"
    UNEXPECTED_TARGET_SQL = "UNEXPECTED_TARGET_SQL"
    MISSING_EXPECTED_VALUE = "MISSING_EXPECTED_VALUE"
    UNSAFE_SQL = "UNSAFE_SQL"

    # Pre-execution validation
    SYNTAX_ERROR = "SYNTAX_ERROR"
    SYNTAX_CHECK_FAILED = "SYNTAX_CHECK_FAILED"

    # Execution
    CONNECTION_FAILED = "CONNECTION_FAILED"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    QUERY_EXECUTION_FAILED = "QUERY_EXECUTION_FAILED"
    NON_SCALAR_RESULT = "NON_SCALAR_RESULT"
    NULL_RESULT = "NULL_RESULT"
    TYPE_CONVERSION_ERROR = "TYPE_CONVERSION_ERROR"
    RUN_STOPPED = "RUN_STOPPED"
    DRY_RUN = "DRY_RUN"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"

    # Comparison
    VALUE_MISMATCH = "VALUE_MISMATCH"
    TOLERANCE_EXCEEDED = "TOLERANCE_EXCEEDED"
    PERCENTAGE_TOLERANCE_EXCEEDED = "PERCENTAGE_TOLERANCE_EXCEEDED"
    ZERO_SOURCE_BASELINE = "ZERO_SOURCE_BASELINE"
    EXPECTED_ZERO_FAILED = "EXPECTED_ZERO_FAILED"
    THRESHOLD_EXCEEDED = "THRESHOLD_EXCEEDED"
    NON_ZERO_FAILED = "NON_ZERO_FAILED"
    BOOLEAN_NOT_TRUE = "BOOLEAN_NOT_TRUE"

    # Workbook / output
    WORKBOOK_READ_FAILED = "WORKBOOK_READ_FAILED"
    OUTPUT_WRITE_FAILED = "OUTPUT_WRITE_FAILED"
