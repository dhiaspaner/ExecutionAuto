"""The oracledb-backed Oracle adapter, driven by a fake driver."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from migration_reconciliation.database.base import QuerySide
from migration_reconciliation.database.oracle import OracleExecutor
from migration_reconciliation.database.settings import ConnectionSettings
from migration_reconciliation.errors import DatabaseExecutionError
from migration_reconciliation.models import DatabaseType
from tests.drivers import FakeConnection, FakeOracleDb, oracle_error

SECRET = "oracle-secret-not-real"


def settings(**overrides: Any) -> ConnectionSettings:
    base: dict[str, Any] = {
        "side": QuerySide.SOURCE,
        "database_type": DatabaseType.ORACLE,
        "server": "legacy-ora.internal",
        "port": 1521,
        "database": "LEGACYSVC",
        "username": "recon_ro",
        "password": SECRET,
    }
    base.update(overrides)
    return ConnectionSettings(**base)


def test_connect_uses_an_easyconnect_dsn() -> None:
    driver = FakeOracleDb()
    executor = OracleExecutor(settings(), driver=driver)

    executor.connect()

    assert driver.connect_kwargs["dsn"] == "legacy-ora.internal:1521/LEGACYSVC"
    assert driver.connect_kwargs["user"] == "recon_ro"


def test_tcp_connect_timeout_is_applied() -> None:
    driver = FakeOracleDb()
    OracleExecutor(settings(connect_timeout=15), driver=driver).connect()

    assert driver.connect_kwargs["tcp_connect_timeout"] == 15


def test_connect_is_idempotent() -> None:
    driver = FakeOracleDb()
    executor = OracleExecutor(settings(), driver=driver)

    executor.connect()
    executor.connect()

    assert len(driver.connections) == 1


def test_execute_scalar_returns_the_single_value() -> None:
    driver = FakeOracleDb(connection=FakeConnection(rows=[[98765]]))
    executor = OracleExecutor(settings(), driver=driver)

    assert executor.execute_scalar("SELECT COUNT(*) FROM PAYMENTS", 30) == 98765


def test_oracle_sql_is_never_translated() -> None:
    driver = FakeOracleDb(connection=FakeConnection(rows=[[1]]))
    executor = OracleExecutor(settings(), driver=driver)
    sql = "SELECT COUNT(*) FROM PAYMENTS WHERE ROWNUM <= 10 AND created > SYSDATE - 1"

    executor.execute_scalar(sql, 30)

    assert driver.connection is not None
    assert driver.connection.executed_sql == [sql]


def test_call_timeout_is_set_in_milliseconds_and_then_restored() -> None:
    connection = FakeConnection(rows=[[1]])
    driver = FakeOracleDb(connection=connection)
    executor = OracleExecutor(settings(), driver=driver)

    executor.execute_scalar("SELECT 1 FROM dual", 20)

    assert 20_000 in connection.call_timeouts_seen
    assert connection.call_timeout == 0


def test_a_non_positive_timeout_is_rejected_before_connecting() -> None:
    driver = FakeOracleDb()
    executor = OracleExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match="Timeout must be greater than 0"):
        executor.execute_scalar("SELECT 1 FROM dual", 0)

    assert driver.connections == []


def test_more_than_one_row_is_rejected() -> None:
    driver = FakeOracleDb(connection=FakeConnection(rows=[[1], [2]]))
    executor = OracleExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match="more than one row"):
        executor.execute_scalar("SELECT amount FROM PAYMENTS", 30)


def test_at_most_two_rows_are_ever_fetched() -> None:
    connection = FakeConnection(rows=[[i] for i in range(500)])
    driver = FakeOracleDb(connection=connection)
    executor = OracleExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError):
        executor.execute_scalar("SELECT id FROM PAYMENTS", 30)

    assert connection.fetch_sizes == [2]


def test_more_than_one_column_is_rejected() -> None:
    driver = FakeOracleDb(connection=FakeConnection(rows=[[1, 2]], column_count=2))
    executor = OracleExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match="2 columns"):
        executor.execute_scalar("SELECT a, b FROM t", 30)


def test_no_rows_is_rejected() -> None:
    driver = FakeOracleDb(connection=FakeConnection(rows=[]))
    executor = OracleExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match="no rows"):
        executor.execute_scalar("SELECT 1 FROM dual WHERE 1 = 0", 30)


def test_the_cursor_is_closed_even_when_the_query_fails() -> None:
    connection = FakeConnection(execute_error=oracle_error(942, "table or view does not exist"))
    driver = FakeOracleDb(connection=connection)
    executor = OracleExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError):
        executor.execute_scalar("SELECT COUNT(*) FROM MISSING", 30)

    assert len(connection.closed_cursors) == 1


def test_close_is_idempotent_and_never_raises() -> None:
    connection = FakeConnection(close_error=OSError("listener gone"))
    driver = FakeOracleDb(connection=connection)
    executor = OracleExecutor(settings(), driver=driver)
    executor.connect()

    executor.close()
    executor.close()

    assert connection.close_count == 1


def test_test_connection_describes_the_session_without_secrets() -> None:
    driver = FakeOracleDb(connection=FakeConnection(version="19.3.0.0.0"))
    executor = OracleExecutor(settings(), driver=driver)

    identity = executor.test_connection()

    assert identity.connection_name == "SOURCE"
    assert identity.database_name == "LEGACYSVC"
    assert identity.product_version == "19.3.0.0.0"
    assert SECRET not in identity.describe()


@pytest.mark.parametrize(
    ("code", "prefix", "expected"),
    [
        (1017, "ORA", "rejected the credentials"),
        (28000, "ORA", "rejected the credentials"),
        (12541, "ORA", "server could not be reached"),
        (12170, "ORA", "server could not be reached"),
        (12514, "ORA", "database or service could not be opened"),
        (12154, "ORA", "database or service could not be opened"),
        (6005, "DPY", "server could not be reached"),
    ],
)
def test_connection_failures_name_a_likely_cause(code: int, prefix: str, expected: str) -> None:
    driver = FakeOracleDb(connect_error=oracle_error(code, "driver detail", prefix=prefix))
    executor = OracleExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError, match=expected):
        executor.connect()


def test_a_connection_failure_never_echoes_the_password() -> None:
    error = oracle_error(1017, f"invalid credential recon_ro/{SECRET}@legacy-ora")
    driver = FakeOracleDb(connect_error=error)
    executor = OracleExecutor(settings(), driver=driver)

    with pytest.raises(DatabaseExecutionError) as caught:
        executor.connect()

    assert SECRET not in str(caught.value)


def test_a_missing_oracledb_is_explained_rather_than_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "oracledb", None)
    executor = OracleExecutor(settings())

    with pytest.raises(DatabaseExecutionError, match="oracledb is not installed"):
        executor.connect()
