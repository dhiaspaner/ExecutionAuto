"""SQL Server adapter built on ``pyodbc``.

``pyodbc`` is imported lazily, inside :meth:`SqlServerExecutor.connect`, so that
the framework still imports, tests and runs offline on a machine where no ODBC
driver is installed.

Two different timeouts matter here and are easy to confuse:

``timeout=`` on ``pyodbc.connect``
    How long to wait for the *login* to complete. This is what makes an
    unreachable host fail quickly instead of hanging.

``connection.timeout``
    How long to wait for a *query*. Comes from the workbook's timeout column,
    and ``0`` means no limit. It belongs to the connection: a pyodbc *cursor*
    has no ``timeout`` attribute at all, and assigning one raises
    ``AttributeError`` against the real driver.
"""

from __future__ import annotations

from typing import Any

from ..errors import (
    ConnectionFailedError,
    DatabaseExecutionError,
    NonScalarResultError,
    QueryTimeoutError,
    SqlSyntaxError,
    SyntaxCheckUnavailableError,
)
from ..models import ConnectionIdentity, ScalarValue
from ..security.redaction import sanitize_error
from .base import normalize_timeout, single_scalar
from .failures import (
    FailureCause,
    classify_failure,
    connection_failure_message,
    is_timeout_failure,
)
from .settings import ConnectionSettings

__all__ = ["PREFERRED_DRIVERS", "SqlServerExecutor"]

#: Tried in order. 18 is current, 17 is still widespread, and the ancient
#: "SQL Server" driver is a last resort that at least yields a clear error.
PREFERRED_DRIVERS: tuple[str, ...] = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)


class SqlServerExecutor:
    """A :class:`~.base.QueryExecutor` backed by one live ``pyodbc`` connection."""

    def __init__(self, settings: ConnectionSettings, *, driver: object | None = None) -> None:
        self._settings = settings
        #: Injected in tests. ``None`` means "import pyodbc when first needed".
        self._driver = driver
        self._connection: Any | None = None
        self._closed = False

    # -- connection ------------------------------------------------------

    def connect(self) -> None:
        """Open the connection. Safe to call more than once."""
        if self._connection is not None:
            return
        module = self._load_driver()
        connection_string = self._build_connection_string(module)
        try:
            self._connection = module.connect(
                connection_string,
                timeout=self._settings.connect_timeout,
                autocommit=True,
            )
        except Exception as exc:
            raise ConnectionFailedError(connection_failure_message(exc, self._settings)) from None

    def test_connection(self) -> ConnectionIdentity:
        """Confirm the connection works and describe it without secrets.

        Deliberately runs no SQL: establishing the session already proves the
        host, the credentials and the database name, and the driver can report
        its version without a round trip.
        """
        self.connect()
        return ConnectionIdentity(
            connection_name=self._settings.connection_name,
            database_type=self._settings.database_type,
            server_description=self._settings.server_address,
            database_name=self._settings.database,
            account_name=self._account_name(),
            product_version=self._product_version(),
        )

    # -- validation ------------------------------------------------------

    def validate_syntax(self, sql: str, timeout_seconds: int) -> None:
        """Compile ``sql`` on the server without executing it.

        ``SET NOEXEC ON`` makes SQL Server parse and compile every following
        statement and execute none of them, so a syntax error, an unknown table
        and a misspelled column are all reported while no row is ever read.

        The option belongs to the *session*, and this session goes on to run
        the real queries, so turning it off again is not tidiness — a session
        left with ``NOEXEC ON`` would silently execute nothing for the rest of
        the run. It is switched off in a ``finally``, and a session whose
        switch-off did not confirm is dropped rather than reused.
        """
        self.connect()
        assert self._connection is not None  # connect() raises otherwise
        label = f"{self._settings.side.value.capitalize()} query"

        self._connection.timeout = normalize_timeout(timeout_seconds)
        cursor = self._connection.cursor()
        try:
            try:
                cursor.execute("SET NOEXEC ON")
            except Exception as exc:
                raise SyntaxCheckUnavailableError(
                    f"{label} could not be checked: the server refused SET NOEXEC ON. "
                    f"{sanitize_error(exc, max_length=200)}"
                ) from None
            try:
                cursor.execute(sql)
            except Exception as exc:
                raise _syntax_failure(exc, label, timeout_seconds) from None
            finally:
                self._resume_execution(cursor, label)
        finally:
            _quietly(cursor.close)

    def _resume_execution(self, cursor: Any, label: str) -> None:
        """Undo ``SET NOEXEC ON``, or throw the session away.

        A connection that might still be in NOEXEC is worse than no connection:
        every later query would succeed and return nothing. Closing it means
        the next query opens a fresh session instead.
        """
        try:
            cursor.execute("SET NOEXEC OFF")
        except Exception as exc:
            self.close()
            raise SyntaxCheckUnavailableError(
                f"{label} was checked but SET NOEXEC OFF did not confirm, so the session "
                f"was closed rather than reused. {sanitize_error(exc, max_length=200)}"
            ) from None

    # -- execution -------------------------------------------------------

    def execute_scalar(self, sql: str, timeout_seconds: int) -> ScalarValue:
        """Run one read-only query and return its single scalar value.

        ``sql`` is passed to the driver verbatim; no dialect translation ever
        happens. At most two rows are fetched, so a query that wrongly matches
        a whole table never pulls that table into this process.
        """
        self.connect()
        assert self._connection is not None  # connect() raises otherwise
        label = f"{self._settings.side.value.capitalize()} query"

        self._connection.timeout = normalize_timeout(timeout_seconds)
        cursor = self._connection.cursor()
        try:
            try:
                cursor.execute(sql)
            except Exception as exc:
                raise _execution_failure(exc, label, timeout_seconds) from None
            if cursor.description is None:
                raise NonScalarResultError(
                    f"{label} returned no result set; exactly one row and one column is required"
                )
            column_count = len(cursor.description)
            rows = cursor.fetchmany(2)
            if len(rows) > 1:
                raise NonScalarResultError(
                    f"{label} returned more than one row; exactly one row is required"
                )
            if rows and column_count != 1:
                raise NonScalarResultError(
                    f"{label} returned {column_count} columns; exactly one column is required"
                )
            return single_scalar([list(row) for row in rows], label=label)
        finally:
            _quietly(cursor.close)

    def close(self) -> None:
        """Release the connection. Idempotent, and never raises."""
        self._closed = True
        connection, self._connection = self._connection, None
        if connection is not None:
            _quietly(connection.close)

    # -- internals -------------------------------------------------------

    def _load_driver(self) -> Any:
        if self._driver is not None:
            return self._driver
        try:
            import pyodbc
        except ImportError as exc:
            raise DatabaseExecutionError(
                "pyodbc is not installed, so no SQL Server connection can be opened. "
                f"Install it with 'uv add pyodbc'. ({exc})"
            ) from None
        self._driver = pyodbc
        return pyodbc

    def _build_connection_string(self, module: Any) -> str:
        """Assemble the ODBC connection string.

        Never logged and never shown: it holds the password. Everything a person
        is told about a failure comes from :mod:`.failures` instead.
        """
        settings = self._settings
        # SERVER carries a port only when the profile gave one. Left off, the
        # ODBC driver resolves the port itself: for a named instance
        # (HOST\INSTANCE) it asks the SQL Server Browser service over UDP 1434,
        # which is the only way to reach an instance listening on a dynamic
        # port that moves between restarts. Naming an explicit port alongside
        # an instance name disables that lookup, so no port is assumed here —
        # 1433 is right only for a default, unnamed instance.
        server = settings.server if settings.port is None else f"{settings.server},{settings.port}"
        parts = [
            f"DRIVER={{{self._choose_driver(module)}}}",
            f"SERVER={server}",
            f"DATABASE={settings.database}",
        ]
        if settings.uses_windows_authentication:
            # The signed-in Windows account authenticates. No username and no
            # password are placed in the connection string at all.
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={settings.username}")
            parts.append(f"PWD={settings.password}")
        parts += [
            "Encrypt=yes",
            f"TrustServerCertificate={'yes' if settings.trust_server_certificate else 'no'}",
            f"Connection Timeout={settings.connect_timeout}",
            "APP=migration-reconciliation",
        ]
        return ";".join(parts) + ";"

    def _choose_driver(self, module: Any) -> str:
        """Pick the newest installed ODBC driver, newest first."""
        try:
            installed = {name.strip() for name in module.drivers()}
        except Exception:
            return PREFERRED_DRIVERS[0]
        for candidate in PREFERRED_DRIVERS:
            if candidate in installed:
                return candidate
        return PREFERRED_DRIVERS[0]

    def _account_name(self) -> str:
        """Who the session authenticated as, for the connection summary."""
        if self._settings.uses_windows_authentication:
            return "(Windows authentication)"
        return self._settings.username

    def _product_version(self) -> str:
        """Ask the driver for the server version. Absence is not an error."""
        module = self._driver
        connection = self._connection
        if module is None or connection is None:  # pragma: no cover - guarded by connect()
            return ""
        try:
            return str(connection.getinfo(module.SQL_DBMS_VER))
        except Exception:
            return ""


def _quietly(action: Any) -> None:
    """Run a cleanup callable, swallowing whatever it raises.

    Cleanup must never mask the error that triggered it, and a connection that
    fails to close is not something the person running a reconciliation can act
    on.
    """
    try:
        action()
    except Exception:
        return


def _execution_failure(
    exc: BaseException, label: str, timeout_seconds: int
) -> DatabaseExecutionError:
    """Classify a driver failure so the caller gets a specific error code."""
    message = f"{label} failed: {sanitize_error(exc, max_length=200)}"
    if is_timeout_failure(exc):
        return QueryTimeoutError(f"{label} exceeded the {timeout_seconds}s timeout. {message}")
    return DatabaseExecutionError(message)


def _syntax_failure(exc: BaseException, label: str, timeout_seconds: int) -> DatabaseExecutionError:
    """Separate "the SQL is wrong" from "the check could not be made".

    A compile error is the answer the check exists to get. A dropped
    connection or an expired timeout is not an answer at all, and reporting
    one as a syntax error would condemn a query the server never finished
    looking at.
    """
    detail = sanitize_error(exc, max_length=200)
    if is_timeout_failure(exc):
        return QueryTimeoutError(
            f"{label} did not compile within the {timeout_seconds}s timeout, so nothing "
            f"is known about its syntax. {detail}"
        )
    if classify_failure(exc) is FailureCause.NETWORK:
        return SyntaxCheckUnavailableError(
            f"{label} could not be checked: the connection failed during the check. {detail}"
        )
    return SqlSyntaxError(f"{label} was rejected by the database before execution: {detail}")
