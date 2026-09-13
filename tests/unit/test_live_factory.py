"""The factory that gives the runner exactly two live connections."""

from __future__ import annotations

from typing import Any

import pytest

from migration_reconciliation.database.base import QuerySide
from migration_reconciliation.database.live import LiveExecutorFactory, build_executor
from migration_reconciliation.database.oracle import OracleExecutor
from migration_reconciliation.database.settings import ConnectionSettings
from migration_reconciliation.database.sqlserver import SqlServerExecutor
from migration_reconciliation.errors import DatabaseExecutionError, ReconciliationError
from migration_reconciliation.models import DatabaseType
from tests.drivers import FakeConnection, FakeOracleDb, FakePyodbc, odbc_error


def settings(side: QuerySide, database_type: DatabaseType, **overrides: Any) -> ConnectionSettings:
    base: dict[str, Any] = {
        "side": side,
        "database_type": database_type,
        "server": f"{side.value}-host",
        "port": 1433 if database_type is DatabaseType.SQLSERVER else 1521,
        "database": f"{side.value}Db",
        "username": f"{side.value}_reader",
        "password": "not-a-real-password",
    }
    base.update(overrides)
    return ConnectionSettings(**base)


def sqlserver_source() -> ConnectionSettings:
    return settings(QuerySide.SOURCE, DatabaseType.SQLSERVER)


def oracle_source() -> ConnectionSettings:
    return settings(QuerySide.SOURCE, DatabaseType.ORACLE)


def sqlserver_target() -> ConnectionSettings:
    return settings(QuerySide.TARGET, DatabaseType.SQLSERVER)


def test_the_source_engine_decides_the_adapter() -> None:
    assert isinstance(build_executor(oracle_source()), OracleExecutor)
    assert isinstance(build_executor(sqlserver_source()), SqlServerExecutor)


def test_a_non_sqlserver_target_is_refused() -> None:
    target = settings(QuerySide.TARGET, DatabaseType.ORACLE)

    with pytest.raises(ReconciliationError, match="target database must be SQL Server"):
        LiveExecutorFactory(oracle_source(), target)


def test_every_test_case_reuses_the_same_two_connections() -> None:
    source_driver = FakeOracleDb()
    target_driver = FakePyodbc()
    factory = LiveExecutorFactory(
        oracle_source(),
        sqlserver_target(),
        source_driver=source_driver,
        target_driver=target_driver,
    )

    first = factory.get_executor(
        connection_name="ignored",
        database_type=DatabaseType.ORACLE,
        test_case_id="TC-1",
        side=QuerySide.SOURCE,
    )
    second = factory.get_executor(
        connection_name="ignored",
        database_type=DatabaseType.ORACLE,
        test_case_id="TC-2",
        side=QuerySide.SOURCE,
    )
    target = factory.get_executor(
        connection_name="ignored",
        database_type=DatabaseType.SQLSERVER,
        test_case_id="TC-1",
        side=QuerySide.TARGET,
    )

    assert first is second
    assert target is not first


def test_connect_all_opens_both_sides_and_describes_them() -> None:
    source_driver = FakeOracleDb(connection=FakeConnection(version="19.3.0.0.0"))
    target_driver = FakePyodbc()
    factory = LiveExecutorFactory(
        oracle_source(),
        sqlserver_target(),
        source_driver=source_driver,
        target_driver=target_driver,
    )

    identities = factory.connect_all()

    assert identities[QuerySide.SOURCE].database_type is DatabaseType.ORACLE
    assert identities[QuerySide.SOURCE].database_name == "sourceDb"
    assert identities[QuerySide.TARGET].database_type is DatabaseType.SQLSERVER
    assert identities[QuerySide.TARGET].database_name == "targetDb"


def test_a_failing_target_closes_the_source_that_already_opened() -> None:
    source_connection = FakeConnection()
    source_driver = FakeOracleDb(connection=source_connection)
    target_driver = FakePyodbc(connect_error=odbc_error("08001", "host unreachable"))
    factory = LiveExecutorFactory(
        oracle_source(),
        sqlserver_target(),
        source_driver=source_driver,
        target_driver=target_driver,
    )

    with pytest.raises(DatabaseExecutionError, match="server could not be reached"):
        factory.connect_all()

    assert source_connection.closed is True


def test_close_all_closes_both_connections() -> None:
    source_connection = FakeConnection()
    target_connection = FakeConnection()
    factory = LiveExecutorFactory(
        oracle_source(),
        sqlserver_target(),
        source_driver=FakeOracleDb(connection=source_connection),
        target_driver=FakePyodbc(connection=target_connection),
    )
    factory.connect_all()

    factory.close_all()

    assert source_connection.closed is True
    assert target_connection.closed is True


def test_one_failing_close_never_skips_the_other() -> None:
    source_connection = FakeConnection(close_error=OSError("already gone"))
    target_connection = FakeConnection()
    factory = LiveExecutorFactory(
        oracle_source(),
        sqlserver_target(),
        source_driver=FakeOracleDb(connection=source_connection),
        target_driver=FakePyodbc(connection=target_connection),
    )
    factory.connect_all()

    factory.close_all()

    assert target_connection.closed is True
