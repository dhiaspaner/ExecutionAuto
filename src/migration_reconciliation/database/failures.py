"""Turning driver exceptions into something a person can act on.

A raw ``pyodbc`` or ``oracledb`` exception is a poor thing to show someone: it
routinely echoes the connection string that produced it, which carries the
password, and it says ``08001`` where the useful information is "that host is
not answering".

Every message produced here names the *likely cause*, names the server and
account so the person knows which attempt failed, and passes the driver's own
words through :func:`~..security.redaction.sanitize_error` before including a
short tail of them.
"""

from __future__ import annotations

from enum import StrEnum

from ..models import DatabaseType
from ..security.redaction import sanitize_error
from .settings import ConnectionSettings

__all__ = [
    "FailureCause",
    "classify_failure",
    "connection_failure_message",
    "describe_object_access_failure",
    "is_timeout_failure",
]


class FailureCause(StrEnum):
    """Why a connection attempt failed, in terms the person can act on."""

    NETWORK = "network"
    LOGIN = "login"
    DATABASE = "database"
    DRIVER = "driver"
    UNKNOWN = "unknown"


#: ODBC SQLSTATEs. The five-character state is the portable part of any ODBC
#: error; the vendor text after it is not.
_ODBC_STATES: dict[str, FailureCause] = {
    "08001": FailureCause.NETWORK,  # client unable to establish connection
    "08S01": FailureCause.NETWORK,  # communication link failure
    "08004": FailureCause.DATABASE,  # server rejected the connection
    "HYT00": FailureCause.NETWORK,  # timeout expired
    "HYT01": FailureCause.NETWORK,  # connection timeout expired
    "28000": FailureCause.LOGIN,  # invalid authorization specification
    "42000": FailureCause.DATABASE,  # cannot open database / access violation
    "IM002": FailureCause.DRIVER,  # data source name not found, no default driver
    "IM003": FailureCause.DRIVER,  # specified driver could not be loaded
}

#: Oracle error codes, thick and thin mode alike.
_ORACLE_CODES: dict[int, FailureCause] = {
    1017: FailureCause.LOGIN,  # invalid username/password
    1005: FailureCause.LOGIN,  # null password given
    28000: FailureCause.LOGIN,  # account is locked
    28001: FailureCause.LOGIN,  # password expired
    12154: FailureCause.DATABASE,  # could not resolve connect identifier
    12514: FailureCause.DATABASE,  # listener does not know of this service
    12505: FailureCause.DATABASE,  # listener does not know of this SID
    12541: FailureCause.NETWORK,  # no listener
    12170: FailureCause.NETWORK,  # connect timeout
    12545: FailureCause.NETWORK,  # host or object does not exist
}

#: ``oracledb`` thin-mode driver errors, which carry a DPY- prefix.
_ORACLE_DPY_CODES: dict[int, FailureCause] = {
    6005: FailureCause.NETWORK,  # cannot connect to database
    4011: FailureCause.NETWORK,  # the database closed the connection
    4027: FailureCause.NETWORK,  # no configured address for the database
}

_ADVICE: dict[FailureCause, str] = {
    FailureCause.NETWORK: (
        "the server could not be reached in time. Check the hostname and port, "
        "whether the VPN is connected, and whether a firewall allows the port"
    ),
    FailureCause.LOGIN: (
        "the server was reached but rejected the credentials. Check the username "
        "and password, and whether the account is locked or expired"
    ),
    FailureCause.DATABASE: (
        "the server was reached and the login succeeded, but the database or "
        "service could not be opened. Check the name and the account's permissions"
    ),
    FailureCause.DRIVER: (
        "the database driver is missing on this machine. Install 'ODBC Driver 18 "
        "for SQL Server' and try again"
    ),
    FailureCause.UNKNOWN: "the driver rejected the connection",
}


def classify_failure(exc: BaseException) -> FailureCause:
    """Best-effort reading of a driver exception. Never raises."""
    state = _odbc_state(exc)
    if state is not None:
        cause = _ODBC_STATES.get(state)
        if cause is not None:
            return cause

    code, prefix = _oracle_code(exc)
    if code is not None:
        table = _ORACLE_DPY_CODES if prefix == "DPY" else _ORACLE_CODES
        cause = table.get(code)
        if cause is not None:
            return cause

    if isinstance(exc, TimeoutError):
        return FailureCause.NETWORK
    if isinstance(exc, ImportError):
        return FailureCause.DRIVER
    return FailureCause.UNKNOWN


def connection_failure_message(exc: BaseException, settings: ConnectionSettings) -> str:
    """One sanitized line explaining a failed connection attempt."""
    cause = classify_failure(exc)
    detail = sanitize_error(exc, max_length=160)
    return (
        f"Cannot connect to the {settings.side.value} database "
        f"({settings.describe()}): {_ADVICE[cause]}{_port_hint(cause, settings)}. "
        f"Driver reported: {detail}"
    )


def _port_hint(cause: FailureCause, settings: ConnectionSettings) -> str:
    """The extra thing to check when no port was configured.

    Without a port the driver finds one by asking the SQL Server Browser
    service, so an unreachable server has one more possible cause than the
    hostname and the firewall — and it is the one nobody thinks of.
    """
    if cause is not FailureCause.NETWORK or settings.port is not None:
        return ""
    if settings.database_type is not DatabaseType.SQLSERVER:
        return ""
    return (
        ". No port is configured, so the port is being looked up through the "
        "SQL Server Browser service: check that it is running and that UDP 1434 "
        "is allowed, or set a port in the profile"
    )


def _odbc_state(exc: BaseException) -> str | None:
    """The five-character SQLSTATE pyodbc puts in ``args[0]``."""
    args = getattr(exc, "args", ())
    if not args:
        return None
    first = args[0]
    if isinstance(first, str) and len(first) == 5 and first.isalnum():
        return first.upper()
    return None


def _oracle_code(exc: BaseException) -> tuple[int | None, str]:
    """The numeric code and prefix from an ``oracledb`` error object."""
    args = getattr(exc, "args", ())
    if not args:
        return None, ""
    error = args[0]
    code = getattr(error, "code", None)
    full = getattr(error, "full_code", "") or ""
    prefix = full.split("-", 1)[0].upper() if "-" in full else ""
    if isinstance(code, int):
        return code, prefix
    return None, prefix


#: SQLSTATEs and vendor codes that mean "the query ran too long", as opposed to
#: "the server could not be reached". Only the query-level ones belong here.
_TIMEOUT_STATES = frozenset({"HYT00", "HYT01"})
_TIMEOUT_ORACLE_CODES = frozenset({1013})  # ORA-01013 user requested cancel
_TIMEOUT_DPY_CODES = frozenset({4024, 4011})  # call timeout, connection closed by timeout
_TIMEOUT_PHRASES = ("timeout expired", "query timeout", "call timeout", "timed out")


def is_timeout_failure(exc: BaseException) -> bool:
    """True when a driver exception means the query exceeded its time limit.

    Read conservatively: a false negative reports ``QUERY_EXECUTION_FAILED``
    with the driver's own words, which is still diagnosable. A false positive
    would hide a real error behind "it was slow".
    """
    if isinstance(exc, TimeoutError):
        return True
    state = _odbc_state(exc)
    if state is not None and state in _TIMEOUT_STATES:
        return True
    code, prefix = _oracle_code(exc)
    if code is not None:
        table = _TIMEOUT_DPY_CODES if prefix == "DPY" else _TIMEOUT_ORACLE_CODES
        if code in table:
            return True
    text = str(exc).casefold()
    return any(phrase in text for phrase in _TIMEOUT_PHRASES)


#: Fragments that mean "the object is there, but not through this session".
#: Cross-database and linked-server references are the common cause in the
#: Payments workbook, where SQL names `etables.dbo` and `DXBPRODSQL02.dbo`.
_OBJECT_ACCESS_PHRASES = (
    "invalid object name",
    "could not find server",
    "linked server",
    "is not configured for",
    "cannot open database",
    "the server principal",
    "permission was denied",
    "select permission",
    "table or view does not exist",
)


def describe_object_access_failure(message: str) -> str:
    """Advice appended when a query names an object the session cannot reach.

    The framework never rewrites the identifiers a workbook uses, so an
    unreachable ``other_db.dbo.Thing`` is a configuration answer, not a query
    to be edited: the login needs rights in that database, or the linked
    server needs defining.
    """
    lowered = message.casefold()
    if not any(phrase in lowered for phrase in _OBJECT_ACCESS_PHRASES):
        return ""
    return (
        "The query names an object this session cannot reach. Cross-database or "
        "linked-server access may need configuring: grant the login read access in "
        "the other database, or define the linked server. The SQL is executed "
        "verbatim and its identifiers are never rewritten."
    )
