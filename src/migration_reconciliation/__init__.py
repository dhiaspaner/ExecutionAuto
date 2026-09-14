"""Workbook-driven data-migration reconciliation framework.

Two workbook shapes are supported, side by side:

* The **reconciliation template** (``Test Cases``, ``Run Control``,
  ``Observation Rules``, ``Run History``), driven by a TOML profile that holds
  every database connection. Start at :mod:`.execution.plan` and
  :mod:`.execution.engine`.
* The original schema-DSL template, driven by ``reconcile execute`` and a
  workbook schema file. Start at :mod:`.workbook.schema` and :mod:`.runner`.

Neither ever stores a password. TOML carries no secret, a workbook carries no
connection, and a password is typed at runtime or avoided entirely with
Windows authentication.
"""

from .errors import (
    ComparisonError,
    ConfigurationError,
    ConnectionFailedError,
    DatabaseExecutionError,
    NonScalarResultError,
    QueryTimeoutError,
    ReconciliationError,
    SchemaError,
    SqlValidationError,
    TypeConversionError,
    WorkbookError,
)
from .models import (
    ComparisonRule,
    ComparisonType,
    ConnectionIdentity,
    DatabaseType,
    ErrorCode,
    ErrorSide,
    ExecutionResult,
    ExecutionScope,
    ExecutionStatus,
    FieldDefinition,
    FieldType,
    Platform,
    ResultType,
    RunSummary,
    TestCase,
    TestStatus,
    WorkbookSchema,
)
from .runner import ReconciliationRunner, RunOptions

__version__ = "0.2.0"

__all__ = [
    "ComparisonError",
    "ComparisonRule",
    "ComparisonType",
    "ConfigurationError",
    "ConnectionFailedError",
    "ConnectionIdentity",
    "DatabaseExecutionError",
    "DatabaseType",
    "ErrorCode",
    "ErrorSide",
    "ExecutionResult",
    "ExecutionScope",
    "ExecutionStatus",
    "FieldDefinition",
    "FieldType",
    "NonScalarResultError",
    "Platform",
    "QueryTimeoutError",
    "ReconciliationError",
    "ReconciliationRunner",
    "ResultType",
    "RunOptions",
    "RunSummary",
    "SchemaError",
    "SqlValidationError",
    "TestCase",
    "TestStatus",
    "TypeConversionError",
    "WorkbookError",
    "WorkbookSchema",
    "__version__",
]
