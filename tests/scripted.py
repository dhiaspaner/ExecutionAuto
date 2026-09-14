"""A scripted stand-in for the open connections of a run.

Implements the :class:`~migration_reconciliation.execution.engine.Executors`
protocol against answers written in the test, so the whole engine — scope
handling, normalization, comparison, status, observation, output — is exercised
with no database, no driver and no credential.

Every call is recorded, which is what lets a test assert the property that
matters most here: that a side a test does not use is never queried, and a
section no enabled test needs is never even opened.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from migration_reconciliation.errors import DatabaseExecutionError
from migration_reconciliation.models import ConnectionIdentity, DatabaseType

__all__ = ["ScriptedCall", "ScriptedExecutor", "ScriptedExecutors"]


@dataclass(frozen=True, slots=True)
class ScriptedCall:
    """One ``execute_scalar`` the engine made."""

    section: str
    sql: str
    timeout_seconds: int


class ScriptedExecutor:
    """One section's connection. Answers from a script, records every call."""

    def __init__(
        self,
        section: str,
        *,
        default: Any = None,
        by_sql: Mapping[str, Any] | None = None,
        calls: list[ScriptedCall],
        opened: list[str],
    ) -> None:
        self.section = section
        self._default = default
        self._by_sql = dict(by_sql or {})
        self._calls = calls
        self._opened = opened
        self.closed = False
        self.close_count = 0

    def test_connection(self) -> ConnectionIdentity:
        self._opened.append(self.section)
        return ConnectionIdentity(
            connection_name=self.section.upper(),
            database_type=DatabaseType.SQLSERVER,
            server_description="scripted",
            database_name=f"{self.section}_db",
        )

    def execute_scalar(self, sql: str, timeout_seconds: int) -> Any:
        if self.closed:
            raise DatabaseExecutionError(f"Connection '{self.section}' is already closed")
        self._calls.append(ScriptedCall(self.section, sql, timeout_seconds))
        answer = self._by_sql.get(sql.strip(), self._default)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self) -> None:
        self.closed = True
        self.close_count += 1


@dataclass
class ScriptedExecutors:
    """The scripted connections for a whole run.

    ``results`` gives each section's default answer; ``by_sql`` overrides it for
    an exact query. An :class:`Exception` in either position is raised instead
    of returned, which is how timeouts, non-scalar results and driver failures
    are reproduced.
    """

    results: Mapping[str, Any] = field(default_factory=dict)
    by_sql: Mapping[tuple[str, str], Any] = field(default_factory=dict)
    connect_failures: Mapping[str, str] = field(default_factory=dict)
    calls: list[ScriptedCall] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    closed: bool = False
    _executors: dict[str, ScriptedExecutor] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for section in self.results:
            self._executors[section] = ScriptedExecutor(
                section,
                default=self.results[section],
                by_sql={
                    sql: answer for (owner, sql), answer in self.by_sql.items() if owner == section
                },
                calls=self.calls,
                opened=self.opened,
            )

    @property
    def sections(self) -> tuple[str, ...]:
        return tuple(self._executors)

    def open_all(self) -> tuple[dict[str, ConnectionIdentity], dict[str, str]]:
        identities: dict[str, ConnectionIdentity] = {}
        failures: dict[str, str] = {}
        for section, executor in self._executors.items():
            if section in self.connect_failures:
                failures[section] = self.connect_failures[section]
                continue
            identities[section] = executor.test_connection()
        return identities, failures

    def executor_for(self, section: str) -> ScriptedExecutor:
        return self._executors[section]

    def database_name(self, section: str) -> str:
        return f"{section}_db"

    def close_all(self) -> None:
        self.closed = True
        for executor in self._executors.values():
            executor.close()

    # -- assertions the tests read ---------------------------------------

    def sql_for(self, section: str) -> list[str]:
        return [call.sql for call in self.calls if call.section == section]

    def queried(self, section: str) -> bool:
        return any(call.section == section for call in self.calls)
