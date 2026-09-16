"""The offline fake executor, its response book and the factory seam."""

from __future__ import annotations

from typing import Any

import pytest

from migration_reconciliation.database.base import QueryExecutor, QuerySide, single_scalar
from migration_reconciliation.database.factory import (
    FakeExecutorFactory,
    create_executor_factory,
    real_adapters_available,
)
from migration_reconciliation.database.fake import (
    FakeQueryExecutor,
    FakeResponse,
    load_fake_results,
    parse_fake_results,
)
from migration_reconciliation.errors import (
    DatabaseExecutionError,
    ReconciliationError,
    SqlSyntaxError,
)
from migration_reconciliation.models import DatabaseType
from tests.conftest import EXAMPLE_FIXTURE_PATH


def _executor(response: FakeResponse, side: QuerySide = QuerySide.SOURCE) -> FakeQueryExecutor:
    return FakeQueryExecutor(
        connection_name="LEGACY_ORACLE",
        database_type=DatabaseType.ORACLE,
        side=side,
        test_case_id="TC-001",
        response=response,
    )


def test_fake_executor_satisfies_the_protocol() -> None:
    assert isinstance(_executor(FakeResponse(value=1)), QueryExecutor)


@pytest.mark.parametrize("value", [1500, 0, -3, 98765.43, "POSTED", True, None])
def test_returns_the_scripted_scalar(value: Any) -> None:
    assert _executor(FakeResponse(value=value)).execute_scalar("SELECT 1", 30) == value


def test_executes_source_and_target_independently() -> None:
    source = _executor(FakeResponse(value=1500), QuerySide.SOURCE)
    target = _executor(FakeResponse(value=1498), QuerySide.TARGET)

    assert source.execute_scalar("SELECT COUNT(*) FROM PAYMENTS", 30) == 1500
    assert target.execute_scalar("SELECT COUNT(*) FROM dbo.Payments", 30) == 1498


def test_records_the_sql_it_was_given_verbatim() -> None:
    executor = _executor(FakeResponse(value=1))
    sql = "SELECT /* dialect-specific */ NVL(SUM(AMOUNT), 0) FROM PAYMENTS"

    executor.execute_scalar(sql, 30)

    assert executor.executed_sql == [sql], "SQL must never be translated or rewritten"


def test_raises_the_scripted_error() -> None:
    executor = _executor(FakeResponse(error="ORA-00942: table or view does not exist"))

    with pytest.raises(DatabaseExecutionError, match="ORA-00942"):
        executor.execute_scalar("SELECT 1", 30)


def test_error_wins_over_value() -> None:
    executor = _executor(FakeResponse(value=1, error="connection reset"))

    with pytest.raises(DatabaseExecutionError, match="connection reset"):
        executor.execute_scalar("SELECT 1", 30)


@pytest.mark.parametrize(
    ("rows", "columns", "message"),
    [
        (0, 1, "returned no rows"),
        (2, 1, "returned 2 rows"),
        (5, 1, "returned 5 rows"),
        (1, 2, "returned 2 columns"),
        (1, 0, "returned 0 columns"),
    ],
)
def test_rejects_result_sets_that_are_not_one_row_by_one_column(
    rows: int, columns: int, message: str
) -> None:
    executor = _executor(FakeResponse(value=1, row_count=rows, column_count=columns))

    with pytest.raises(DatabaseExecutionError, match=message):
        executor.execute_scalar("SELECT 1", 30)


def test_single_scalar_helper_enforces_the_shape() -> None:
    assert single_scalar([[42]]) == 42
    with pytest.raises(DatabaseExecutionError):
        single_scalar([])
    with pytest.raises(DatabaseExecutionError):
        single_scalar([[1, 2]])


def test_a_non_positive_timeout_means_no_limit() -> None:
    """The fake mirrors the real adapters: zero is "no limit", not an error."""
    assert _executor(FakeResponse(value=1)).execute_scalar("SELECT 1", 0) == 1
    assert _executor(FakeResponse(value=1)).execute_scalar("SELECT 1", -5) == 1


def test_test_connection_describes_without_secrets() -> None:
    identity = _executor(FakeResponse(value=1)).test_connection()

    assert identity.connection_name == "LEGACY_ORACLE"
    assert identity.database_type is DatabaseType.ORACLE
    assert "password" not in identity.describe().casefold()


def test_close_is_idempotent_and_blocks_further_use() -> None:
    executor = _executor(FakeResponse(value=1))

    executor.close()
    executor.close()

    assert executor.closed is True
    assert executor.close_count == 2
    with pytest.raises(DatabaseExecutionError, match="already closed"):
        executor.execute_scalar("SELECT 1", 30)


def test_loads_the_shipped_fixture() -> None:
    book = load_fake_results(EXAMPLE_FIXTURE_PATH)

    assert book.response_for("TC-PAY-001", QuerySide.SOURCE).value == 1500
    assert book.response_for("TC-PAY-007", QuerySide.SOURCE).error is not None
    assert book.response_for("TC-PAY-009", QuerySide.TARGET).value is None


def test_unknown_case_is_an_explicit_error_by_default() -> None:
    book = parse_fake_results({"version": "1.0", "case": []})

    with pytest.raises(DatabaseExecutionError, match="No fake result defined"):
        book.response_for("TC-MISSING", QuerySide.SOURCE)


def test_unknown_case_can_be_scripted_as_null() -> None:
    book = parse_fake_results(
        {"version": "1.0", "defaults": {"on_unknown_case": "null"}, "case": []}
    )

    assert book.response_for("TC-MISSING", QuerySide.SOURCE).value is None


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "version must be one of"),
        ({"version": "9.9"}, "version must be one of"),
        ({"version": "1.0", "shell": "cmd.exe"}, "unknown key"),
        ({"version": "1.0", "case": [{}]}, "requires a non-empty test_case_id"),
        (
            {"version": "1.0", "case": [{"test_case_id": "A"}, {"test_case_id": "A"}]},
            "duplicate test_case_id",
        ),
        (
            {"version": "1.0", "case": [{"test_case_id": "A", "source_result": [1, 2]}]},
            "must be a scalar",
        ),
        (
            {"version": "1.0", "case": [{"test_case_id": "A", "source_row_count": -1}]},
            "must be an integer of 0 or more",
        ),
        ({"version": "1.0", "defaults": {"on_unknown_case": "guess"}}, "must be one of"),
    ],
)
def test_rejects_invalid_fixtures(document: dict[str, Any], message: str) -> None:
    with pytest.raises(ReconciliationError, match=message):
        parse_fake_results(document)


def test_factory_caches_one_executor_per_connection_case_and_side(make_results: Any) -> None:
    factory = FakeExecutorFactory(make_results({"test_case_id": "TC-001", "source_result": 1}))

    first = factory.get_executor(
        connection_name="SRC",
        database_type=DatabaseType.ORACLE,
        test_case_id="TC-001",
        side=QuerySide.SOURCE,
    )
    again = factory.get_executor(
        connection_name="SRC",
        database_type=DatabaseType.ORACLE,
        test_case_id="TC-001",
        side=QuerySide.SOURCE,
    )
    other_side = factory.get_executor(
        connection_name="TGT",
        database_type=DatabaseType.SQLSERVER,
        test_case_id="TC-001",
        side=QuerySide.TARGET,
    )

    assert first is again
    assert first is not other_side


def test_close_all_closes_every_executor(make_results: Any) -> None:
    factory = FakeExecutorFactory(make_results({"test_case_id": "TC-001", "source_result": 1}))
    for side in QuerySide:
        factory.get_executor(
            connection_name="C",
            database_type=DatabaseType.ORACLE,
            test_case_id="TC-001",
            side=side,
        )

    factory.close_all()

    assert factory.created
    assert all(executor.closed for executor in factory.created)


def test_no_real_adapter_is_registered_in_this_milestone() -> None:
    assert real_adapters_available() is False


def test_factory_refuses_to_run_without_scripted_results() -> None:
    with pytest.raises(ReconciliationError, match="Milestone 1 runs offline only"):
        create_executor_factory()


def test_runner_module_imports_no_database_driver() -> None:
    """The runner must depend on the protocol, never on pyodbc or oracledb."""
    import sys

    from migration_reconciliation import runner  # noqa: F401

    assert "pyodbc" not in sys.modules
    assert "oracledb" not in sys.modules


# -- the scripted validation pass ---------------------------------------------


def test_validation_passes_by_default_and_executes_nothing() -> None:
    executor = _executor(FakeResponse(value=1500))

    executor.validate_syntax("SELECT COUNT(*) FROM PAYMENTS", 30)

    assert executor.validated_sql == ["SELECT COUNT(*) FROM PAYMENTS"]
    assert executor.executed_sql == []


def test_a_scripted_syntax_error_is_raised_before_execution() -> None:
    executor = _executor(FakeResponse(value=1, syntax_error="ORA-00904: invalid identifier"))

    with pytest.raises(SqlSyntaxError, match="ORA-00904"):
        executor.validate_syntax("SELECT NO_SUCH_COLUMN FROM PAYMENTS", 30)

    assert executor.executed_sql == []


def test_validation_accepts_a_non_positive_timeout_as_no_limit() -> None:
    _executor(FakeResponse(value=1)).validate_syntax("SELECT 1", 0)


def test_a_fixture_can_script_a_syntax_error_per_side() -> None:
    book = parse_fake_results(
        {
            "version": "1.0",
            "case": [
                {
                    "test_case_id": "TC-001",
                    "source_syntax_error": "ORA-00942",
                    "target_result": 5,
                }
            ],
        }
    )

    assert book.response_for("TC-001", QuerySide.SOURCE).syntax_error == "ORA-00942"
    assert book.response_for("TC-001", QuerySide.TARGET).syntax_error is None


def test_a_syntax_error_that_is_not_text_is_rejected() -> None:
    with pytest.raises(ReconciliationError, match="source_syntax_error must be a string"):
        parse_fake_results(
            {
                "version": "1.0",
                "case": [{"test_case_id": "TC-001", "source_syntax_error": 42}],
            }
        )
