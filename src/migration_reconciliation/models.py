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
    "ConnectionIdentity",
    "DatabaseType",
    "ErrorSide",
    "ExecutionResult",
    "ExecutionStatus",
    "FieldDefinition",
    "FieldType",
    "RunSummary",
    "ScalarValue",
    "TestCase",
    "WorkbookSchema",
]

#: A single reconciliation query result. Queries must return exactly one row
#: and one column, so only scalars ever cross the database boundary.
type ScalarValue = str | int | float | Decimal | bool | datetime | date | None


class DatabaseType(StrEnum):
    """Supported source database engines. The target is always SQL Server."""

    ORACLE = "oracle"
    SQLSERVER = "sqlserver"


class ComparisonRule(StrEnum):
    """Strategies for deciding whether a source/target pair reconciles."""

    EQUAL = "equal"
    EXPECTED_ZERO = "expected_zero"
    NUMERIC_TOLERANCE = "numeric_tolerance"


class ExecutionStatus(StrEnum):
    """Outcome of one test case."""

    PASS = "PASS"
    FAIL = "FAIL"
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
    source_type: DatabaseType
    source_connection: str
    target_connection: str
    source_sql: str
    target_sql: str
    comparison_rule: ComparisonRule = ComparisonRule.EQUAL
    tolerance: Decimal = Decimal(0)
    timeout_seconds: int = 120
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
    def executed(self) -> int:
        return len(self.results) - self.skipped

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def is_clean(self) -> bool:
        """True when nothing failed and nothing errored."""
        return self.failed == 0 and self.errored == 0
