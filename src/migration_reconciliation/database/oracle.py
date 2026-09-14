"""Oracle adapter built on ``oracledb``.

``oracledb`` is imported lazily, inside :meth:`OracleExecutor.connect`, so that
nobody has to install it — or an Oracle Client — unless they actually choose an
Oracle source.

The driver's default *thin* mode is used: it speaks the wire protocol directly
and needs no Oracle Instant Client on the machine.

Oracle spells its timeouts differently from ODBC:

``tcp_connect_timeout``
    Seconds to wait for the TCP connection and handshake.

``connection.call_timeout``
    Milliseconds any single round trip may take. Set per query from the
    workbook's timeout column.
"""

from __future__ import annotations

from typing import Any

from ..errors import (
    ConnectionFailedError,
    DatabaseExecutionError,
    NonScalarResultError,
    QueryTimeoutError,
)
from ..models import ConnectionIdentity, ScalarValue
from ..security.redaction import sanitize_error
from .base import single_scalar
from .failures import connection_failure_message, is_timeout_failure
from .settings import ConnectionSettings

__all__ = ["OracleExecutor"]


class OracleExecutor:
    """A :class:`~.base.QueryExecutor` backed by one live ``oracledb`` connection."""

    def __init__(self, settings: ConnectionSettings, *, driver: object | None = None) -> None:
        self._settings = settings
        #: Injected in tests. ``None`` means "import oracledb when first needed".
        self._driver = driver
        self._connection: Any | None = None
        self._closed = False

    # -- connection ------------------------------------------------------

    def connect(self) -> None:
        """Open the connection. Safe to call more than once."""
        if self._connection is not None:
            return
        module = self._load_driver()
        settings = self._settings
        try:
            self._connection = module.connect(
                user=settings.username,
                password=settings.password,
                dsn=self.dsn,
                tcp_connect_timeout=settings.connect_timeout,
            )
        except Exception as exc:
            raise ConnectionFailedError(connection_failure_message(exc, settings)) from None

    @property
    def dsn(self) -> str:
        """EasyConnect descriptor. Carries the service name, never a password."""
        settings = self._settings
        return f"{settings.server}:{settings.port}/{settings.database}"

    def test_connection(self) -> ConnectionIdentity:
        """Confirm the connection works and describe it without secrets."""
        self.connect()
        return ConnectionIdentity(
            connection_name=self._settings.connection_name,
            database_type=self._settings.database_type,
            server_description=f"{self._settings.server}:{self._settings.port}",
            database_name=self._settings.database,
            account_name=self._settings.username,
            product_version=self._product_version(),
        )

    # -- execution -------------------------------------------------------

    def execute_scalar(self, sql: str, timeout_seconds: int) -> ScalarValue:
        """Run one read-only query and return its single scalar value.

        ``sql`` is passed to the driver verbatim — Oracle SQL is never
        translated. At most two rows are fetched, so a query that wrongly
        matches a whole table never pulls that table into this process.
        """
        if timeout_seconds <= 0:
            raise DatabaseExecutionError(
                f"Timeout must be greater than 0 seconds (got {timeout_seconds})"
            )
        self.connect()
        connection = self._connection
        assert connection is not None  # connect() raises otherwise
        label = f"{self._settings.side.value.capitalize()} query"

        previous_timeout = getattr(connection, "call_timeout", 0)
        cursor = connection.cursor()
        try:
            connection.call_timeout = timeout_seconds * 1000
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
            _restore_call_timeout(connection, previous_timeout)

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
            import oracledb
        except ImportError as exc:
            raise DatabaseExecutionError(
                "oracledb is not installed, so no Oracle connection can be opened. "
                f"Install it with 'uv add oracledb'. ({exc})"
            ) from None
        self._driver = oracledb
        return oracledb

    def _product_version(self) -> str:
        """The server version the driver already knows. Absence is not an error."""
        try:
            return str(getattr(self._connection, "version", "") or "")
        except Exception:
            return ""


def _restore_call_timeout(connection: Any, previous: Any) -> None:
    try:
        connection.call_timeout = previous
    except Exception:
        return


def _quietly(action: Any) -> None:
    """Run a cleanup callable, swallowing whatever it raises."""
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
