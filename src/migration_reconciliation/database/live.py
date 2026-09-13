"""The factory that connects the runner to two real databases.

Unlike the workbook-driven registry the offline milestone anticipated, this
factory holds exactly two connections — one source, one target — established
from what the person typed into the wizard. Every test case reuses them, so a
run of two hundred cases opens two sessions and asks for each password once.

The runner asks for an executor by connection name and database type; this
factory dispatches on :class:`~.base.QuerySide` alone, because the wizard, not
the workbook, decides what each side connects to.
"""

from __future__ import annotations

from ..errors import ReconciliationError
from ..models import ConnectionIdentity, DatabaseType
from .base import QueryExecutor, QuerySide
from .oracle import OracleExecutor
from .settings import ConnectionSettings
from .sqlserver import SqlServerExecutor

__all__ = ["LiveExecutorFactory", "build_executor"]


def build_executor(settings: ConnectionSettings, *, driver: object | None = None) -> QueryExecutor:
    """Create the right adapter for ``settings``, without connecting yet."""
    if settings.database_type is DatabaseType.ORACLE:
        return OracleExecutor(settings, driver=driver)
    return SqlServerExecutor(settings, driver=driver)


class LiveExecutorFactory:
    """Owns one source connection and one target connection for a whole run."""

    def __init__(
        self,
        source: ConnectionSettings,
        target: ConnectionSettings,
        *,
        source_driver: object | None = None,
        target_driver: object | None = None,
    ) -> None:
        if target.database_type is not DatabaseType.SQLSERVER:
            raise ReconciliationError("The target database must be SQL Server.")
        self._settings = {QuerySide.SOURCE: source, QuerySide.TARGET: target}
        self._executors: dict[QuerySide, QueryExecutor] = {
            QuerySide.SOURCE: build_executor(source, driver=source_driver),
            QuerySide.TARGET: build_executor(target, driver=target_driver),
        }

    def settings_for(self, side: QuerySide) -> ConnectionSettings:
        return self._settings[side]

    def connect_all(self) -> dict[QuerySide, ConnectionIdentity]:
        """Open both connections up front and describe them.

        Called before the workbook is read, so an unreachable server or a bad
        password is reported in seconds rather than discovered on the first
        test case. The source is attempted first, matching the order the
        questions were asked.
        """
        identities: dict[QuerySide, ConnectionIdentity] = {}
        for side in (QuerySide.SOURCE, QuerySide.TARGET):
            executor = self._executors[side]
            try:
                identities[side] = executor.test_connection()
            except Exception:
                self.close_all()  # never leave the other side open
                raise
        return identities

    def get_executor(
        self,
        *,
        connection_name: str,
        database_type: DatabaseType,
        test_case_id: str,
        side: QuerySide,
    ) -> QueryExecutor:
        """Return the one executor for ``side``. Every case shares it."""
        return self._executors[side]

    def close_all(self) -> None:
        """Close both connections. One failing close never skips the other."""
        for executor in self._executors.values():
            try:
                executor.close()
            except Exception:  # cleanup must not mask the error that triggered it
                continue
