"""Workbook-driven data-migration reconciliation framework.

Milestone 1 is offline: no database driver is imported, no credential is read,
and no network call is made anywhere in this package.
"""

from .errors import (
    ComparisonError,
    DatabaseExecutionError,
    ReconciliationError,
    SchemaError,
    SqlValidationError,
    WorkbookError,
)
from .models import (
    ComparisonRule,
    ConnectionIdentity,
    DatabaseType,
    ErrorSide,
    ExecutionResult,
    ExecutionStatus,
    FieldDefinition,
    FieldType,
    RunSummary,
    TestCase,
    WorkbookSchema,
)
from .runner import ReconciliationRunner, RunOptions

__version__ = "0.1.0"

__all__ = [
    "ComparisonError",
    "ComparisonRule",
    "ConnectionIdentity",
    "DatabaseExecutionError",
    "DatabaseType",
    "ErrorSide",
    "ExecutionResult",
    "ExecutionStatus",
    "FieldDefinition",
    "FieldType",
    "ReconciliationError",
    "ReconciliationRunner",
    "RunOptions",
    "RunSummary",
    "SchemaError",
    "SqlValidationError",
    "TestCase",
    "WorkbookError",
    "WorkbookSchema",
    "__version__",
]
