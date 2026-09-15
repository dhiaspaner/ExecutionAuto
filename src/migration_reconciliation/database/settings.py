"""Connection settings gathered from the interactive wizard.

These objects exist only for the life of one run. They are built from answers
typed at the keyboard, never from a file, an environment variable or a command
line argument, and they are never written anywhere.

The password lives in :attr:`ConnectionSettings.password` and nowhere else. The
dataclass is deliberately *not* printable: ``repr`` is overridden so that a
stray ``print(settings)``, a traceback frame or a debugger cannot echo it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..errors import ReconciliationError
from ..models import DatabaseType
from .base import QuerySide

__all__ = [
    "DEFAULT_ORACLE_THICK_MODE",
    "DEFAULT_PORTS",
    "AuthMode",
    "ConnectionSettings",
    "default_port_for",
]


class AuthMode(StrEnum):
    """How a connection proves who it is.

    ``PASSWORD`` asks for a username and a password at runtime. ``WINDOWS`` uses
    the account already signed in to Windows, so no credential is typed, stored
    or transmitted by this program at all — which makes it the safer choice
    wherever the servers accept it.
    """

    PASSWORD = "password"
    WINDOWS = "windows"


#: The port each engine listens on unless the person says otherwise.
DEFAULT_PORTS: dict[DatabaseType, int] = {
    DatabaseType.SQLSERVER: 1433,
    DatabaseType.ORACLE: 1521,
}

#: Whether an Oracle connection loads the Oracle Client library when nothing
#: says otherwise. Thin mode needs no install but reaches only Oracle Database
#: 12.1 and later; the servers this framework is pointed at are older, so thick
#: is the default and a profile opts out with oracle_client_mode = "thin".
DEFAULT_ORACLE_THICK_MODE = True

#: Seconds to wait for a connection before giving up. Long enough for a slow
#: VPN handshake, short enough that an unreachable host fails while the person
#: is still watching the terminal.
CONNECT_TIMEOUT_SECONDS = 15


def default_port_for(database_type: DatabaseType) -> int:
    return DEFAULT_PORTS[database_type]


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    """Everything needed to open one connection, including the password."""

    side: QuerySide
    database_type: DatabaseType
    server: str
    port: int
    #: SQL Server database name, or the Oracle service name.
    database: str
    #: Empty under Windows authentication: the signed-in account is used.
    username: str = ""
    #: Empty under Windows authentication. Never read from a file or argument.
    password: str = ""
    auth_mode: AuthMode = AuthMode.PASSWORD
    #: SQL Server only. Skips validation of the server's TLS certificate.
    trust_server_certificate: bool = False
    connect_timeout: int = CONNECT_TIMEOUT_SECONDS
    #: Oracle only. ``None`` means "use the engine default", which is thick —
    #: see :data:`DEFAULT_ORACLE_THICK_MODE`. ``False`` forces thin mode, which
    #: needs no Oracle Client but reaches only Oracle Database 12.1 and later.
    #: Read through :attr:`uses_thick_client`, never directly.
    use_thick_client: bool | None = None
    #: Oracle thick mode only. Where the Oracle Client library lives. Empty
    #: means "let the driver find it" via PATH, LD_LIBRARY_PATH or the macOS
    #: equivalent. A directory name is not a secret and is safe to print.
    oracle_client_dir: str = ""

    def __post_init__(self) -> None:
        if self.use_thick_client is True and self.database_type is not DatabaseType.ORACLE:
            raise ReconciliationError(
                "Thick-client mode applies to Oracle connections only; "
                f"a {self.database_type.value} connection has no Oracle Client library."
            )
        if self.auth_mode is AuthMode.WINDOWS:
            if self.database_type is not DatabaseType.SQLSERVER:
                raise ReconciliationError(
                    "Windows authentication is available for SQL Server only; "
                    "an Oracle connection needs a username and password."
                )
            return
        if not self.username or not self.password:
            raise ReconciliationError(
                f"The {self.side.value} connection needs a username and a password."
            )

    @property
    def uses_windows_authentication(self) -> bool:
        return self.auth_mode is AuthMode.WINDOWS

    @property
    def uses_thick_client(self) -> bool:
        """Whether this connection loads the Oracle Client library.

        SQL Server never does. An Oracle connection does unless the profile
        asked for thin mode, so an unconfigured run reaches an old server
        rather than failing on it.
        """
        if self.database_type is not DatabaseType.ORACLE:
            return False
        if self.use_thick_client is None:
            return DEFAULT_ORACLE_THICK_MODE
        return self.use_thick_client

    @property
    def oracle_client_mode(self) -> str:
        """``"thick"`` or ``"thin"``, for messages and reports."""
        return "thick" if self.uses_thick_client else "thin"

    def __repr__(self) -> str:
        """Never render the password, not even under a debugger."""
        return (
            f"ConnectionSettings(side={self.side.value}, "
            f"database_type={self.database_type.value}, target={self.describe()})"
        )

    __str__ = __repr__

    @property
    def connection_name(self) -> str:
        """Logical name used in reports. Carries no host and no account."""
        return self.side.value.upper()

    def describe(self) -> str:
        """A one-line description safe to print, log or put in a workbook cell.

        Includes the host so a person can tell which server answered, and how it
        authenticated so they can tell which login was used. Never the password.
        """
        who = (
            "using Windows authentication"
            if self.uses_windows_authentication
            else f"as {self.username}"
        )
        return f"{self.server}:{self.port}/{self.database} {who}"
