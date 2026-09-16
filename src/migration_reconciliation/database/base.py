"""Database-facing interfaces.

The runner depends on :class:`QueryExecutor` and :class:`ExecutorFactory` only.
No driver is imported anywhere in this package in milestone 1 — ``pyodbc`` and
``oracledb`` adapters will be added as new modules implementing these protocols,
without touching the runner.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..errors import NonScalarResultError
from ..models import ConnectionIdentity, DatabaseType, ScalarValue

__all__ = [
    "NO_TIMEOUT",
    "ExecutorFactory",
    "QueryExecutor",
    "QuerySide",
    "normalize_timeout",
    "single_scalar",
]

#: The value that means "wait as long as the query takes". Both drivers spell
#: an unlimited wait as zero: pyodbc's ``cursor.timeout`` and ODBC's
#: ``Connection Timeout``, and python-oracledb's ``call_timeout`` and
#: ``tcp_connect_timeout``.
NO_TIMEOUT = 0


def normalize_timeout(timeout_seconds: int) -> int:
    """Fold any non-positive timeout to :data:`NO_TIMEOUT`.

    A missing, zero or negative timeout all mean the same thing — do not
    impose a limit — so they are answered identically rather than one of them
    becoming a driver error at the moment a long reconciliation finally runs.
    """
    return timeout_seconds if timeout_seconds > 0 else NO_TIMEOUT


class QuerySide(StrEnum):
    """Which half of a reconciliation a connection serves."""

    SOURCE = "source"
    TARGET = "target"


@runtime_checkable
class QueryExecutor(Protocol):
    """Executes one read-only scalar query against one logical connection."""

    def test_connection(self) -> ConnectionIdentity:
        """Confirm the connection works and describe it without secrets."""
        ...

    def validate_syntax(self, sql: str, timeout_seconds: int) -> None:
        """Ask the database whether ``sql`` compiles, without executing it.

        This is the first of the two passes a run makes: every enabled query is
        compiled by the server before any of them is executed, so a typo in the
        last row cannot be discovered halfway through a reconciliation.

        Implementations must use a mechanism that reads nothing and writes
        nothing — SQL Server's ``SET NOEXEC ON``, Oracle's parse-only call —
        and must leave the session exactly as they found it, because the very
        same connection goes on to run the real queries.

        Returns ``None`` when the query compiles. Raises
        :class:`~..errors.SqlSyntaxError` when the database rejects it, and
        :class:`~..errors.SyntaxCheckUnavailableError` when the check itself
        could not be performed — which proves nothing about the SQL and must
        never be reported as if it did.
        """
        ...

    def execute_scalar(self, sql: str, timeout_seconds: int) -> ScalarValue:
        """Run ``sql`` and return its single scalar value.

        Implementations must pass ``sql`` to the driver verbatim — never
        translated between dialects — apply ``timeout_seconds`` (where a
        non-positive value means no limit, see :func:`normalize_timeout`), and
        reject any result that is not exactly one row and one column.
        """
        ...

    def close(self) -> None:
        """Release the connection. Must be safe to call more than once."""
        ...


@runtime_checkable
class ExecutorFactory(Protocol):
    """Supplies executors and owns their lifetime.

    ``test_case_id`` and ``side`` are passed for every request so that offline
    fakes can vary their answers per test case. Real adapters ignore both and
    cache one connection per logical connection name, so a run prompts for a
    given credential at most once.
    """

    def get_executor(
        self,
        *,
        connection_name: str,
        database_type: DatabaseType,
        test_case_id: str,
        side: QuerySide,
    ) -> QueryExecutor: ...

    def close_all(self) -> None:
        """Close every executor handed out so far."""
        ...


def single_scalar(rows: Sequence[Sequence[ScalarValue]], *, label: str = "Query") -> ScalarValue:
    """Enforce the one-row/one-column contract shared by every adapter.

    Reconciliation queries must return exactly one scalar. Anything else stops
    the test case, which is what keeps whole result sets — and therefore
    customer data — out of the framework and out of the result workbook.
    """
    row_count = len(rows)
    if row_count == 0:
        raise NonScalarResultError(f"{label} returned no rows; exactly one row is required")
    if row_count > 1:
        raise NonScalarResultError(
            f"{label} returned {row_count} rows; exactly one row is required"
        )
    column_count = len(rows[0])
    if column_count != 1:
        raise NonScalarResultError(
            f"{label} returned {column_count} columns; exactly one column is required"
        )
    return rows[0][0]
