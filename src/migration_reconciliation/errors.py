"""Exception hierarchy.

All messages raised here are safe to show a user: they never embed credentials,
connection strings or query result data.
"""

from __future__ import annotations

__all__ = [
    "ComparisonError",
    "ConfigurationError",
    "ConnectionFailedError",
    "DatabaseExecutionError",
    "NonScalarResultError",
    "QueryTimeoutError",
    "ReconciliationError",
    "SchemaError",
    "SqlValidationError",
    "TypeConversionError",
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


class ConnectionFailedError(DatabaseExecutionError):
    """A session could not be established. The message is already sanitized."""


class QueryTimeoutError(DatabaseExecutionError):
    """A query exceeded the timeout configured for it."""


class NonScalarResultError(DatabaseExecutionError):
    """A query returned something other than exactly one row and one column.

    Distinct from a general execution failure because it is the guard that
    keeps whole result sets — and therefore client data — out of this process.
    """


class ConfigurationError(ReconciliationError):
    """A configuration problem, carrying the stable code that names it.

    Raised for anything wrong with the profile, the run-control settings or a
    sheet's structure. The code travels with the message so the caller can
    write it to ``Error_Code`` without re-deriving it from prose.
    """

    def __init__(self, message: str, *, code: str, platform: str = "WORKBOOK") -> None:
        super().__init__(message)
        self.code = code
        self.platform = platform


class TypeConversionError(ReconciliationError):
    """A scalar could not be read as the ``Result_Type`` the workbook declared.

    Always an error, never a silent zero or false: a count that arrives as the
    word "unknown" must stop its test case, not reconcile against 0.
    """
