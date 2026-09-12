"""Exception hierarchy.

All messages raised here are safe to show a user: they never embed credentials,
connection strings or query result data.
"""

from __future__ import annotations

__all__ = [
    "ComparisonError",
    "DatabaseExecutionError",
    "ReconciliationError",
    "SchemaError",
    "SqlValidationError",
    "WorkbookError",
]


class ReconciliationError(Exception):
    """Base class for every error this framework raises deliberately."""


class SchemaError(ReconciliationError):
    """The workbook schema DSL is invalid or unsupported."""


class WorkbookError(ReconciliationError):
    """The workbook does not match the schema, or cannot be read/written."""


class SqlValidationError(ReconciliationError):
    """SQL was rejected by the offline read-only guard."""


class ComparisonError(ReconciliationError):
    """Source and target results cannot be compared under the chosen rule."""


class DatabaseExecutionError(ReconciliationError):
    """A query executor failed. Messages are sanitized before display."""
