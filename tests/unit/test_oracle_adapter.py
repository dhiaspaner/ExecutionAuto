"""The oracledb-backed Oracle adapter, driven by a fake driver."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from migration_reconciliation.database.base import QuerySide
from migration_reconciliation.database.oracle import OracleExecutor
from migration_reconciliation.database.settings import ConnectionSettings
from migration_reconciliation.errors import (
    DatabaseExecutionError,
    SqlSyntaxError,
    SyntaxCheckUnavailableError,
)
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


# -- thick mode: for servers older than thin mode supports --------------------


@pytest.fixture(autouse=True)
def _reset_thick_mode() -> Any:
    """Thick mode is process-wide, so each test starts from a clean slate."""
    from migration_reconciliation.database import oracle as oracle_module

    oracle_module._thick_mode_started = False
    yield
    oracle_module._thick_mode_started = False


def test_oracle_loads_the_client_by_default() -> None:
    """Thick is the engine default: an unconfigured run reaches an old server."""
    driver = FakeOracleDb()

    OracleExecutor(settings(), driver=driver).connect()

    assert driver.init_calls == [{}]


def test_thin_mode_can_still_be_asked_for() -> None:
    driver = FakeOracleDb()

    OracleExecutor(settings(use_thick_client=False), driver=driver).connect()

    assert driver.init_calls == []


def test_sql_server_never_loads_the_oracle_client() -> None:
    """The default is per-engine, so it must not leak into SQL Server."""
    from migration_reconciliation.database.settings import ConnectionSettings

    sqlserver = ConnectionSettings(
        side=QuerySide.SOURCE,
        database_type=DatabaseType.SQLSERVER,
        server="sql.internal",
        port=1433,
        database="Db",
        username="u",
        password=SECRET,
    )

    assert sqlserver.uses_thick_client is False
    assert sqlserver.oracle_client_mode == "thin"


def test_thick_mode_loads_the_client_from_the_configured_directory() -> None:
    driver = FakeOracleDb()
    executor = OracleExecutor(
        settings(use_thick_client=True, oracle_client_dir="/opt/oracle/instantclient_19_8"),
        driver=driver,
    )

    executor.connect()

    assert driver.init_calls == [{"lib_dir": "/opt/oracle/instantclient_19_8"}]
    assert driver.connect_kwargs["dsn"] == "legacy-ora.internal:1521/LEGACYSVC"


def test_thick_mode_without_a_directory_lets_the_driver_find_the_client() -> None:
    driver = FakeOracleDb()

    OracleExecutor(settings(use_thick_client=True), driver=driver).connect()

    assert driver.init_calls == [{}]


def test_the_client_is_loaded_only_once_per_process() -> None:
    first, second = FakeOracleDb(), FakeOracleDb()

    OracleExecutor(settings(use_thick_client=True), driver=first).connect()
    OracleExecutor(settings(use_thick_client=True), driver=second).connect()

    assert first.init_calls == [{}]
    assert second.init_calls == []  # the process-wide switch was already on


def test_a_missing_oracle_client_is_reported_before_connecting() -> None:
    driver = FakeOracleDb(init_error=RuntimeError("DPI-1047: cannot locate libclntsh.so"))
    executor = OracleExecutor(
        settings(use_thick_client=True, oracle_client_dir="/wrong/path"), driver=driver
    )

    with pytest.raises(DatabaseExecutionError, match="Oracle Client library could not be loaded"):
        executor.connect()

    assert driver.connections == []  # nothing was attempted


def test_the_failure_message_names_the_directory_but_never_the_password() -> None:
    driver = FakeOracleDb(init_error=RuntimeError("DPI-1047: cannot locate libclntsh.so"))
    executor = OracleExecutor(
        settings(use_thick_client=True, oracle_client_dir="/wrong/path"), driver=driver
    )

    with pytest.raises(DatabaseExecutionError) as caught:
        executor.connect()

    assert "/wrong/path" in str(caught.value)
    assert SECRET not in str(caught.value)


def test_an_already_enabled_client_is_not_an_error() -> None:
    """A second process-wide init is tolerated, not turned into a failure."""
    from migration_reconciliation.database import oracle as oracle_module

    oracle_module._thick_mode_started = False
    driver = FakeOracleDb(
        init_error=RuntimeError("DPY-2019: python-oracledb thick mode has already been enabled")
    )

    OracleExecutor(settings(use_thick_client=True), driver=driver).connect()

    assert driver.connections != []


def test_thick_mode_is_rejected_for_sql_server() -> None:
    from migration_reconciliation.errors import ReconciliationError

    with pytest.raises(ReconciliationError, match="Oracle connections only"):
        settings(database_type=DatabaseType.SQLSERVER, use_thick_client=True)


# -- validating SQL without executing it --------------------------------------


def test_validation_parses_the_statement_and_executes_nothing() -> None:
    connection = FakeConnection()
    executor = OracleExecutor(settings(), driver=FakeOracleDb(connection=connection))

    executor.validate_syntax("SELECT COUNT(*) FROM PAYMENTS", 30)

    assert connection.parsed_sql == ["SELECT COUNT(*) FROM PAYMENTS"]
    assert connection.executed_sql == []
    assert connection.fetch_sizes == []


def test_validation_never_writes_a_plan_table() -> None:
    """EXPLAIN PLAN inserts rows; the read-only account this framework needs cannot."""
    connection = FakeConnection()
    executor = OracleExecutor(settings(), driver=FakeOracleDb(connection=connection))

    executor.validate_syntax("SELECT COUNT(*) FROM PAYMENTS", 30)

    statements = " ".join(connection.parsed_sql + connection.executed_sql).upper()
    assert "EXPLAIN PLAN" not in statements
    assert "PLAN_TABLE" not in statements


def test_oracle_sql_is_not_translated_before_it_is_parsed() -> None:
    connection = FakeConnection()
    sql = "SELECT COUNT(*) FROM legacy.payments WHERE ROWNUM <= 1"

    OracleExecutor(settings(), driver=FakeOracleDb(connection=connection)).validate_syntax(sql, 30)

    assert connection.parsed_sql == [sql]


def test_a_statement_oracle_rejects_is_a_syntax_error() -> None:
    connection = FakeConnection(
        parse_errors_by_sql={
            "SELECT COUNT(*) FROM PAYMNETS": oracle_error(942, "table or view does not exist")
        }
    )
    executor = OracleExecutor(settings(), driver=FakeOracleDb(connection=connection))

    with pytest.raises(SqlSyntaxError, match="does not exist"):
        executor.validate_syntax("SELECT COUNT(*) FROM PAYMNETS", 30)


def test_a_lost_connection_during_a_parse_is_not_reported_as_bad_sql() -> None:
    connection = FakeConnection(
        parse_errors_by_sql={
            "SELECT 1 FROM DUAL": oracle_error(6005, "cannot connect to database", prefix="DPY")
        }
    )
    executor = OracleExecutor(settings(), driver=FakeOracleDb(connection=connection))

    with pytest.raises(SyntaxCheckUnavailableError):
        executor.validate_syntax("SELECT 1 FROM DUAL", 30)


def test_validation_applies_the_call_timeout_and_restores_it() -> None:
    connection = FakeConnection(call_timeout=0)
    executor = OracleExecutor(settings(), driver=FakeOracleDb(connection=connection))

    executor.validate_syntax("SELECT 1 FROM DUAL", 20)

    assert 20_000 in connection.call_timeouts_seen
    assert connection.call_timeout == 0


def test_validation_closes_its_cursor() -> None:
    connection = FakeConnection()
    executor = OracleExecutor(settings(), driver=FakeOracleDb(connection=connection))

    executor.validate_syntax("SELECT 1 FROM DUAL", 30)

    assert all(cursor.closed for cursor in connection.cursors)


def test_validation_refuses_a_timeout_of_zero() -> None:
    executor = OracleExecutor(settings(), driver=FakeOracleDb())

    with pytest.raises(DatabaseExecutionError, match="greater than 0"):
        executor.validate_syntax("SELECT 1 FROM DUAL", 0)


def test_a_driver_without_a_parse_call_reports_an_unavailable_check() -> None:
    class CursorWithoutParse:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class ConnectionWithoutParse:
        call_timeout = 0

        def cursor(self) -> CursorWithoutParse:
            return CursorWithoutParse()

    class DriverWithoutParse:
        def init_oracle_client(self, **kwargs: Any) -> None:
            return None

        def connect(self, **kwargs: Any) -> ConnectionWithoutParse:
            return ConnectionWithoutParse()

    executor = OracleExecutor(settings(), driver=DriverWithoutParse())

    with pytest.raises(SyntaxCheckUnavailableError, match="parse-only"):
        executor.validate_syntax("SELECT 1 FROM DUAL", 30)


def test_the_password_never_reaches_a_validation_failure() -> None:
    connection = FakeConnection(
        parse_errors_by_sql={
            "SELECT 1 FROM DUAL": oracle_error(904, f"invalid identifier password={SECRET}")
        }
    )
    executor = OracleExecutor(settings(), driver=FakeOracleDb(connection=connection))

    with pytest.raises(SqlSyntaxError) as raised:
        executor.validate_syntax("SELECT 1 FROM DUAL", 30)

    assert SECRET not in str(raised.value)
