"""Oracle adapter built on ``oracledb``.

``oracledb`` is imported lazily, inside :meth:`OracleExecutor.connect`, so that
nobody has to install it — or an Oracle Client — unless they actually choose an
Oracle source.

The driver's default *thin* mode speaks the wire protocol directly and needs no
Oracle Instant Client on the machine, but it only reaches Oracle Database 12.1
and later. An older server answers a thin connection with ``DPY-3010``.

So thick mode is the default here: :attr:`ConnectionSettings.uses_thick_client`
loads the Oracle Client library instead, which reaches back to Oracle Database
9.2. A profile opts back out with ``oracle_client_mode = "thin"``. That library has to
be installed separately, and the Instant Client release must itself be old
enough to talk to the server: a 19c client reaches 11.2, and only an older
client reaches 9.2. Thick mode is a process-wide switch, so it is started once
and shared by every Oracle connection in the run.

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
        if settings.uses_thick_client:
            _start_thick_mode(module, settings.oracle_client_dir)
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

    # -- validation ------------------------------------------------------

    def validate_syntax(self, sql: str, timeout_seconds: int) -> None:
        """Parse ``sql`` on the server without executing it.

        ``Cursor.parse()`` asks Oracle to do only the parse step it would do
        anyway before executing: the statement is checked for syntax and for
        the objects and columns it names, no row is read, and no cursor is
        left open.

        ``EXPLAIN PLAN FOR`` would answer the same question, but it *inserts*
        into ``PLAN_TABLE``, which the read-only account this framework
        requires cannot do — the check would fail on exactly the accounts
        production uses. Parsing needs no privilege beyond the one the query
        itself needs.
        """
        self.connect()
        connection = self._connection
        assert connection is not None  # connect() raises otherwise
        label = f"{self._settings.side.value.capitalize()} query"

        previous_timeout = getattr(connection, "call_timeout", 0)
        cursor = connection.cursor()
        try:
            connection.call_timeout = normalize_timeout(timeout_seconds) * 1000
            parse = getattr(cursor, "parse", None)
            if not callable(parse):
                raise SyntaxCheckUnavailableError(
                    f"{label} could not be checked: this oracledb build offers no parse-only "
                    f"call, so nothing is known about the SQL either way."
                )
            try:
                parse(sql)
            except Exception as exc:
                raise _syntax_failure(exc, label, timeout_seconds) from None
        finally:
            _quietly(cursor.close)
            _restore_call_timeout(connection, previous_timeout)

    # -- execution -------------------------------------------------------

    def execute_scalar(self, sql: str, timeout_seconds: int) -> ScalarValue:
        """Run one read-only query and return its single scalar value.

        ``sql`` is passed to the driver verbatim — Oracle SQL is never
        translated. At most two rows are fetched, so a query that wrongly
        matches a whole table never pulls that table into this process.
        """
        self.connect()
        connection = self._connection
        assert connection is not None  # connect() raises otherwise
        label = f"{self._settings.side.value.capitalize()} query"

        previous_timeout = getattr(connection, "call_timeout", 0)
        cursor = connection.cursor()
        try:
            connection.call_timeout = normalize_timeout(timeout_seconds) * 1000
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


#: Thick mode can only be started once per process, so the first Oracle
#: connection that asks for it does the work and the rest inherit it.
_thick_mode_started = False


def _start_thick_mode(module: Any, client_dir: str) -> None:
    """Load the Oracle Client library, once, before the first connection.

    A failure here means the library is missing or unusable, so it is reported
    as a connection failure rather than left to surface as a puzzling error on
    ``connect``. The directory is named in the message because a wrong path is
    the usual cause; it is a path, never a credential.
    """
    global _thick_mode_started
    if _thick_mode_started:
        return
    try:
        if client_dir:
            module.init_oracle_client(lib_dir=client_dir)
        else:
            module.init_oracle_client()
    except Exception as exc:
        if not _already_started(exc):
            where = f"'{client_dir}'" if client_dir else "the system library path"
            raise ConnectionFailedError(
                "Thick mode was requested but the Oracle Client library could not be "
                f"loaded from {where}, so no connection was attempted. Install Oracle "
                "Instant Client and point oracle_client_dir at it. "
                f"Driver reported: {sanitize_error(exc, max_length=200)}"
            ) from None
    _thick_mode_started = True


def _already_started(exc: BaseException) -> bool:
    """True when thick mode was already enabled by an earlier connection."""
    text = str(exc).casefold()
    return "already" in text and ("enabled" in text or "initialized" in text)


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


def _syntax_failure(exc: BaseException, label: str, timeout_seconds: int) -> DatabaseExecutionError:
    """Separate "the SQL is wrong" from "the check could not be made".

    ORA-00904 and ORA-00942 are answers: the query does not compile. A lost
    connection or an expired timeout is not an answer at all, and reporting one
    as a syntax error would condemn a query the server never finished reading.
    """
    detail = sanitize_error(exc, max_length=200)
    if is_timeout_failure(exc):
        return QueryTimeoutError(
            f"{label} did not parse within the {timeout_seconds}s timeout, so nothing is "
            f"known about its syntax. {detail}"
        )
    if classify_failure(exc) is FailureCause.NETWORK:
        return SyntaxCheckUnavailableError(
            f"{label} could not be checked: the connection failed during the check. {detail}"
        )
    return SqlSyntaxError(f"{label} was rejected by the database before execution: {detail}")
