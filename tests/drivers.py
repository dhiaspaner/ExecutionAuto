"""Fake database drivers.

These stand in for ``pyodbc`` and ``oracledb`` so that every adapter behaviour —
connecting, timeouts, wrong result shapes, login failures, cleanup — is tested
without a database, a driver install or a credential.

Each fake records what it was asked to do, so a test can assert that the
adapter applied a timeout, closed its cursor, or passed SQL through untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FakeOracleDb",
    "FakeOracleError",
    "FakePyodbc",
    "FakePyodbcError",
    "OracleErrorObject",
    "odbc_error",
    "oracle_error",
]


class FakePyodbcError(Exception):
    """Shaped like ``pyodbc.Error``: ``args[0]`` is the five-character SQLSTATE."""


class FakeOracleError(Exception):
    """Shaped like ``oracledb.DatabaseError``: ``args[0]`` carries the code."""


@dataclass
class OracleErrorObject:
    code: int
    full_code: str
    message: str = ""

    def __str__(self) -> str:
        return f"{self.full_code}: {self.message}"


def odbc_error(state: str, message: str) -> FakePyodbcError:
    return FakePyodbcError(state, f"[{state}] {message}")


def oracle_error(code: int, message: str, *, prefix: str = "ORA") -> FakeOracleError:
    return FakeOracleError(OracleErrorObject(code, f"{prefix}-{code:05d}", message))


@dataclass
class FakeCursor:
    """One cursor. Records the SQL and the timeout it was given."""

    connection: Any
    rows: list[list[Any]]
    column_count: int
    execute_error: Exception | None = None
    no_result_set: bool = False
    timeout: int | None = None
    closed: bool = False
    description: Any = None

    def execute(self, sql: str) -> None:
        if self.connection.closed:
            raise RuntimeError("cursor used after the connection was closed")
        self.connection.executed_sql.append(sql)
        scripted = self.connection.values_by_sql.get(sql.strip())
        if isinstance(scripted, Exception):
            raise scripted
        if scripted is not None:
            self.rows = [[scripted]]
        if self.execute_error is not None:
            raise self.execute_error
        self.description = (
            None if self.no_result_set else [(f"col{i}",) for i in range(self.column_count)]
        )

    def fetchmany(self, size: int) -> list[list[Any]]:
        self.connection.fetch_sizes.append(size)
        return [list(row) for row in self.rows[:size]]

    def close(self) -> None:
        self.closed = True
        self.connection.closed_cursors.append(self)


@dataclass
class FakeConnection:
    """One connection. Records everything the adapter did to it."""

    connection_string: str = ""
    connect_kwargs: dict[str, Any] = field(default_factory=dict)
    rows: list[list[Any]] = field(default_factory=lambda: [[1]])
    #: Per-query answers keyed by exact SQL text. A value becomes the single
    #: scalar the query returns; an Exception is raised instead.
    values_by_sql: dict[str, Any] = field(default_factory=dict)
    column_count: int = 1
    execute_error: Exception | None = None
    no_result_set: bool = False
    close_error: Exception | None = None
    version: str = "19.0.0.0.0"
    call_timeout: int = 0
    closed: bool = False
    close_count: int = 0
    executed_sql: list[str] = field(default_factory=list)
    fetch_sizes: list[int] = field(default_factory=list)
    cursors: list[FakeCursor] = field(default_factory=list)
    closed_cursors: list[FakeCursor] = field(default_factory=list)
    call_timeouts_seen: list[int] = field(default_factory=list)

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(
            connection=self,
            rows=list(self.rows),
            column_count=self.column_count,
            execute_error=self.execute_error,
            no_result_set=self.no_result_set,
        )
        self.cursors.append(cursor)
        return cursor

    def getinfo(self, code: int) -> str:
        return "16.00.4125"

    def close(self) -> None:
        self.close_count += 1
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def __setattr__(self, name: str, value: Any) -> None:
        # Record every call_timeout the adapter sets, so a test can prove it was
        # applied per query and then restored.
        if name == "call_timeout" and "call_timeouts_seen" in self.__dict__:
            self.__dict__["call_timeouts_seen"].append(value)
        object.__setattr__(self, name, value)


@dataclass
class FakePyodbc:
    """Stands in for the ``pyodbc`` module."""

    connect_error: Exception | None = None
    installed_drivers: list[str] = field(
        default_factory=lambda: ["ODBC Driver 18 for SQL Server", "SQL Server"]
    )
    connection: FakeConnection | None = None
    connections: list[FakeConnection] = field(default_factory=list)
    drivers_error: Exception | None = None

    SQL_DBMS_VER = 18
    Error = FakePyodbcError

    def connect(self, connection_string: str, **kwargs: Any) -> FakeConnection:
        if self.connect_error is not None:
            raise self.connect_error
        connection = self.connection or FakeConnection()
        connection.connection_string = connection_string
        connection.connect_kwargs = kwargs
        self.connection = connection
        self.connections.append(connection)
        return connection

    def drivers(self) -> list[str]:
        if self.drivers_error is not None:
            raise self.drivers_error
        return list(self.installed_drivers)


@dataclass
class FakeOracleDb:
    """Stands in for the ``oracledb`` module."""

    connect_error: Exception | None = None
    connection: FakeConnection | None = None
    connections: list[FakeConnection] = field(default_factory=list)
    connect_kwargs: dict[str, Any] = field(default_factory=dict)
    #: Every init_oracle_client call, so a test can prove thick mode was
    #: started once, with the directory the profile asked for.
    init_calls: list[dict[str, Any]] = field(default_factory=list)
    init_error: Exception | None = None

    DatabaseError = FakeOracleError

    def init_oracle_client(self, **kwargs: Any) -> None:
        self.init_calls.append(kwargs)
        if self.init_error is not None:
            raise self.init_error

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error
        connection = self.connection or FakeConnection()
        connection.connect_kwargs = kwargs
        self.connection = connection
        self.connections.append(connection)
        return connection
