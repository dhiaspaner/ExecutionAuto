"""The pyodbc-backed SQL Server adapter, driven by a fake driver."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from migration_reconciliation.database.base import QuerySide
from migration_reconciliation.database.settings import ConnectionSettings
from migration_reconciliation.database.sqlserver import PREFERRED_DRIVERS, SqlServerExecutor
from migration_reconciliation.errors import DatabaseExecutionError
from migration_reconciliation.models import DatabaseType
from tests.drivers import FakeConnection, FakePyodbc, odbc_error

SECRET = "hunter2-not-real"


def settings(**overrides: Any) -> ConnectionSettings:
    base: dict[str, Any] = {
        "side": QuerySide.TARGET,
        "database_type": DatabaseType.SQLSERVER,
        "server": "sql-01.internal",
        "port": 1433,
        "database": "MigratedDb",
        "username": "svc_recon",
        "password": SECRET,
    }
    base.update(overrides)
    return ConnectionSettings(**base)


def test_connect_builds_a_connection_string_from_the_answers() -> None:
    driver = FakePyodbc()
    executor = SqlServerExecutor(settings(trust_server_certificate=True), driver=driver)

    executor.connect()

    assert driver.connection is not None
    connection_string = driver.connection.connection_string
    assert "SERVER=sql-01.internal,1433" in connection_string
    assert "DATABASE=MigratedDb" in connection_string
    assert "UID=svc_recon" in connection_string
    assert "TrustServerCertificate=yes" in connection_string
    assert "Encrypt=yes" in connection_string


def test_certificate_validation_stays_on_unless_asked() -> None:
    driver = FakePyodbc()
    SqlServerExecutor(settings(trust_server_certificate=False), driver=driver).connect()

    assert driver.connection is not None
    assert "TrustServerCertificate=no" in driver.connection.connection_string


def test_login_timeout_is_applied_so_an_unreachable_host_fails_fast() -> None:
    driver = FakePyodbc()
    SqlServerExecutor(settings(connect_timeout=15), driver=driver).connect()

    assert driver.connection is not None
    assert driver.connection.connect_kwargs["timeout"] == 15


def test_connect_is_idempotent() -> None:
    driver = FakePyodbc()
    executor = SqlServerExecutor(settings(), driver=driver)

    executor.connect()
    executor.connect()

    assert len(driver.connections) == 1


def test_newest_installed_driver_wins() -> None:
    driver = FakePyodbc(installed_drivers=["SQL Server", "ODBC Driver 17 for SQL Server"])
    SqlServerExecutor(settings(), driver=driver).connect()

    assert driver.connection is not None
    assert "DRIVER={ODBC Driver 17 for SQL Server}" in driver.connection.connection_string


def test_driver_enumeration_failure_falls_back_to_the_newest_name() -> None:
    driver = FakePyodbc(drivers_error=OSError("no odbc installed"))
    SqlServerExecutor(settings(), driver=driver).connect()

    assert driver.connection is not None
    assert f"DRIVER={{{PREFERRED_DRIVERS[0]}}}" in driver.connection.connection_string


def test_execute_scalar_returns_the_single_value() -> None:
    driver = FakePyodbc(connection=FakeConnection(rows=[[4201]]))
    executor = SqlServerExecutor(settings(), driver=driver)

    assert executor.execute_scalar("SELECT COUNT(*) FROM dbo.Payments", 30) == 4201


def test_sql_reaches_the_driver_verbatim() -> None:
    driver = FakePyodbc(connection=FakeConnection(rows=[[1]]))
    executor = SqlServerExecutor(settings(), driver=driver)
    sql = "WITH x AS (SELECT 1 AS n) SELECT SUM(n) FROM x"

    executor.execute_scalar(sql, 30)

    assert driver.connection is not None
    assert driver.connection.executed_sql == [sql]


def test_query_timeout_is_set_on_the_cursor() -> None:
    driver = FakePyodbc(connection=FakeConnection(rows=[[1]]))
    executor = SqlServerExecutor(settings(), driver=driver)

    executor.execute_scalar("SELECT 1", 45)

    assert driver.connection is not None
    assert driver.connection.cursors[0].timeout == 45


def test_a_non_positive_timeout_is_rejected_before_connecting() -> None:
    driver = FakePyodbc()
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match="Timeout must be greater than 0"):
        executor.execute_scalar("SELECT 1", 0)

    assert driver.connections == []


def test_more_than_one_row_is_rejected() -> None:
    driver = FakePyodbc(connection=FakeConnection(rows=[[1], [2], [3]]))
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match="more than one row"):
        executor.execute_scalar("SELECT amount FROM dbo.Payments", 30)


def test_at_most_two_rows_are_ever_fetched() -> None:
    """A mistaken query must never pull a table into this process."""
    driver = FakePyodbc(connection=FakeConnection(rows=[[i] for i in range(1000)]))
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError):
        executor.execute_scalar("SELECT id FROM dbo.Payments", 30)

    assert driver.connection is not None
    assert driver.connection.fetch_sizes == [2]


def test_more_than_one_column_is_rejected() -> None:
    driver = FakePyodbc(connection=FakeConnection(rows=[[1, 2]], column_count=2))
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match="2 columns"):
        executor.execute_scalar("SELECT a, b FROM dbo.T", 30)


def test_no_rows_is_rejected() -> None:
    driver = FakePyodbc(connection=FakeConnection(rows=[]))
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match="no rows"):
        executor.execute_scalar("SELECT COUNT(*) FROM dbo.Empty WHERE 1 = 0", 30)


def test_a_statement_with_no_result_set_is_rejected() -> None:
    driver = FakePyodbc(connection=FakeConnection(no_result_set=True))
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match="no result set"):
        executor.execute_scalar("SELECT 1", 30)


def test_the_cursor_is_closed_even_when_the_query_fails() -> None:
    connection = FakeConnection(execute_error=odbc_error("42S02", "Invalid object name 'dbo.Nope'"))
    driver = FakePyodbc(connection=connection)
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError):
        executor.execute_scalar("SELECT COUNT(*) FROM dbo.Nope", 30)

    assert len(connection.closed_cursors) == 1


def test_close_is_idempotent_and_never_raises() -> None:
    connection = FakeConnection(close_error=OSError("socket already gone"))
    driver = FakePyodbc(connection=connection)
    executor = SqlServerExecutor(settings(), driver=driver)
    executor.connect()

    executor.close()
    executor.close()

    assert connection.close_count == 1


def test_test_connection_describes_the_session_without_secrets() -> None:
    driver = FakePyodbc()
    executor = SqlServerExecutor(settings(), driver=driver)

    identity = executor.test_connection()

    assert identity.connection_name == "TARGET"
    assert identity.database_name == "MigratedDb"
    assert identity.account_name == "svc_recon"
    assert identity.server_description == "sql-01.internal:1433"
    assert SECRET not in identity.describe()


def test_test_connection_runs_no_sql() -> None:
    driver = FakePyodbc()
    SqlServerExecutor(settings(), driver=driver).test_connection()

    assert driver.connection is not None
    assert driver.connection.executed_sql == []


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("08001", "server could not be reached"),
        ("HYT00", "server could not be reached"),
        ("28000", "rejected the credentials"),
        ("42000", "database or service could not be opened"),
        ("IM002", "database driver is missing"),
    ],
)
def test_connection_failures_name_a_likely_cause(state: str, expected: str) -> None:
    driver = FakePyodbc(connect_error=odbc_error(state, "driver detail here"))
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match=expected):
        executor.connect()


def test_a_connection_failure_never_echoes_the_password() -> None:
    error = odbc_error("28000", f"Login failed. PWD={SECRET};UID=svc_recon")
    driver = FakePyodbc(connect_error=error)
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError) as caught:
        executor.connect()

    assert SECRET not in str(caught.value)


def test_a_query_failure_never_echoes_the_password() -> None:
    connection = FakeConnection(execute_error=odbc_error("42000", f"failed; PWD={SECRET}"))
    driver = FakePyodbc(connection=connection)
    executor = SqlServerExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError) as caught:
        executor.execute_scalar("SELECT 1", 30)

    assert SECRET not in str(caught.value)


def test_a_missing_pyodbc_is_explained_rather_than_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pyodbc", None)
    executor = SqlServerExecutor(settings())

    with pytest.raises(DatabaseExecutionError, match="pyodbc is not installed"):
        executor.connect()
