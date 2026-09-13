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

from ..models import DatabaseType
from .base import QuerySide

__all__ = ["DEFAULT_PORTS", "ConnectionSettings", "default_port_for"]

#: The port each engine listens on unless the person says otherwise.
DEFAULT_PORTS: dict[DatabaseType, int] = {
    DatabaseType.SQLSERVER: 1433,
    DatabaseType.ORACLE: 1521,
}

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
    username: str
    password: str
    #: SQL Server only. Skips validation of the server's TLS certificate.
    trust_server_certificate: bool = False
    connect_timeout: int = CONNECT_TIMEOUT_SECONDS

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

        Includes the host so a person can tell which server answered, and the
        account so they can tell which login was used. Never the password.
        """
        return f"{self.server}:{self.port}/{self.database} as {self.username}"
