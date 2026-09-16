"""End-to-end offline runs: execution, isolation, cleanup and selection."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from migration_reconciliation.database.base import QuerySide
from migration_reconciliation.database.factory import FakeExecutorFactory
from migration_reconciliation.errors import WorkbookError
from migration_reconciliation.models import (
    ErrorSide,
    ExecutionStatus,
    OnSyntaxError,
    RunMode,
)
from migration_reconciliation.runner import ReconciliationRunner, RunOptions


def _run(schema: Any, workbook: Path, book: Any, **option_overrides: Any):
    factory = FakeExecutorFactory(book)
    runner = ReconciliationRunner(schema, factory)
    summary = runner.run(workbook, RunOptions(**option_overrides))
    return summary, factory


def _by_id(summary: Any) -> dict[str, Any]:
    return {result.test_case_id: result for result in summary.results}


def test_passing_case_is_executed_and_recorded(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1500, "target_result": 1500})

    summary, _ = _run(schema, path, book)

    result = _by_id(summary)["TC-001"]
    assert result.status is ExecutionStatus.PASS
    assert result.source_result == 1500
    assert result.target_result == 1500
    assert result.variance == Decimal(0)
    assert result.error_side is ErrorSide.NONE
    assert result.run_id == summary.run_id
    assert summary.is_clean is True


def test_source_and_target_queries_are_sent_to_their_own_connections(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    _, factory = _run(schema, path, book)

    sent = {(e.connection_name, e.side): e.executed_sql for e in factory.created}
    assert sent[("LEGACY_ORACLE", QuerySide.SOURCE)] == ["SELECT COUNT(*) FROM PAYMENTS"]
    assert sent[("MIGRATED_SQLSERVER", QuerySide.TARGET)] == ["SELECT COUNT(*) FROM dbo.Payments"]


def test_failing_comparison_is_reported_without_stopping_the_run(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001"), case_row("TC-002")])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 10, "target_result": 7},
        {"test_case_id": "TC-002", "source_result": 5, "target_result": 5},
    )

    summary, _ = _run(schema, path, book)

    results = _by_id(summary)
    assert results["TC-001"].status is ExecutionStatus.FAIL
    assert results["TC-001"].variance == Decimal(3)
    assert results["TC-002"].status is ExecutionStatus.PASS
    assert summary.is_clean is False


def test_source_execution_error_is_isolated(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001"), case_row("TC-002")])
    book = make_results(
        {"test_case_id": "TC-001", "source_error": "ORA-00942: table or view does not exist"},
        {"test_case_id": "TC-002", "source_result": 5, "target_result": 5},
    )

    summary, _ = _run(schema, path, book)

    failed = _by_id(summary)["TC-001"]
    assert failed.status is ExecutionStatus.ERROR
    assert failed.error_side is ErrorSide.SOURCE
    assert failed.error_code == "SOURCE_EXEC_FAILED"
    assert "ORA-00942" in failed.remarks
    assert _by_id(summary)["TC-002"].status is ExecutionStatus.PASS


def test_target_execution_error_is_attributed_to_the_target(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results(
        {
            "test_case_id": "TC-001",
            "source_result": 10,
            "target_error": "Login timeout expired",
        }
    )

    summary, _ = _run(schema, path, book)

    result = _by_id(summary)["TC-001"]
    assert result.status is ExecutionStatus.ERROR
    assert result.error_side is ErrorSide.TARGET
    assert result.error_code == "TARGET_EXEC_FAILED"
    assert result.source_result == 10, "the source result is still recorded"


def test_error_messages_are_sanitized_before_they_reach_a_result(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results(
        {
            "test_case_id": "TC-001",
            "source_error": (
                "Login failed. DRIVER={ODBC Driver 18};SERVER=prod-sql-01;"
                "UID=svc_recon;PWD=Sup3rSecret!;"
            ),
        }
    )

    summary, _ = _run(schema, path, book)

    remarks = _by_id(summary)["TC-001"].remarks
    assert "Sup3rSecret!" not in remarks
    assert "svc_recon" not in remarks
    assert "REDACTED" in remarks


def test_comparison_error_is_attributed_to_the_comparison(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 100})

    summary, _ = _run(schema, path, book)

    result = _by_id(summary)["TC-001"]
    assert result.status is ExecutionStatus.ERROR
    assert result.error_side is ErrorSide.COMPARISON
    assert result.error_code == "COMPARISON_FAILED"
    assert "NULL" in result.remarks


def test_unsafe_sql_is_rejected_before_any_executor_is_created(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001", source_sql="DELETE FROM PAYMENTS")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, factory = _run(schema, path, book)

    result = _by_id(summary)["TC-001"]
    # Caught by the pre-execution check, so it never reaches the execution pass.
    assert result.status is ExecutionStatus.ERROR
    assert result.error_code == "SQL_REJECTED"
    assert result.error_side is ErrorSide.SOURCE
    assert result.error_code == "SQL_REJECTED"
    assert factory.created == (), "no connection is opened for a rejected query"


def test_unsafe_target_sql_is_attributed_to_the_target(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001", target_sql="SELECT 1; DROP TABLE t")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, _ = _run(schema, path, book)

    assert _by_id(summary)["TC-001"].error_side is ErrorSide.TARGET


def test_invalid_row_becomes_an_error_result_with_the_workbook_side(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-BAD", source_type="postgres"), case_row("TC-OK")])
    book = make_results({"test_case_id": "TC-OK", "source_result": 1, "target_result": 1})

    summary, _ = _run(schema, path, book)

    bad = _by_id(summary)["TC-BAD"]
    assert bad.status is ExecutionStatus.ERROR
    assert bad.error_side is ErrorSide.WORKBOOK
    assert bad.error_code == "WORKBOOK_ROW_INVALID"
    assert _by_id(summary)["TC-OK"].status is ExecutionStatus.PASS


def test_disabled_cases_are_recorded_as_skipped_and_never_executed(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001"), case_row("TC-002", enabled=False)])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, factory = _run(schema, path, book)

    assert _by_id(summary)["TC-002"].status is ExecutionStatus.SKIPPED
    assert all(e.test_case_id != "TC-002" for e in factory.created)
    assert summary.skipped == 1


def test_executors_are_closed_after_a_successful_run(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    _, factory = _run(schema, path, book)

    assert factory.created
    assert all(e.closed for e in factory.created)


def test_executors_are_closed_after_a_failing_run(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_error": "boom"})

    _, factory = _run(schema, path, book)

    assert factory.created
    assert all(e.closed for e in factory.created)


def test_executors_are_closed_when_writing_fails(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Path
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})
    factory = FakeExecutorFactory(book)

    with pytest.raises(WorkbookError):
        ReconciliationRunner(schema, factory).run(path, RunOptions(output_dir=tmp_path / "missing"))

    assert all(e.closed for e in factory.created)


def test_case_selection_runs_only_the_named_case(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001"), case_row("TC-PAY-008"), case_row("TC-003")])
    book = make_results(
        {"test_case_id": "TC-PAY-008", "source_result": 1, "target_result": 1},
        on_unknown_case="error",
    )

    summary, factory = _run(schema, path, book, case_ids=("TC-PAY-008",))

    results = _by_id(summary)
    assert results["TC-PAY-008"].status is ExecutionStatus.PASS
    assert results["TC-001"].status is ExecutionStatus.SKIPPED
    assert results["TC-001"].remarks == "Not selected by --case"
    assert {e.test_case_id for e in factory.created} == {"TC-PAY-008"}


def test_case_selection_is_case_insensitive(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-PAY-008")])
    book = make_results({"test_case_id": "TC-PAY-008", "source_result": 1, "target_result": 1})

    summary, _ = _run(schema, path, book, case_ids=("tc-pay-008",))

    assert summary.passed == 1


def test_unknown_case_selection_is_an_error(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    with pytest.raises(WorkbookError, match="No enabled test case matches --case TC-NOPE"):
        _run(schema, path, book, case_ids=("TC-NOPE",))


def test_limit_runs_a_pilot_subset(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 6)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 6)
        ]
    )

    summary, factory = _run(schema, path, book, limit=2)

    assert summary.passed == 2
    assert summary.skipped == 3
    assert _by_id(summary)["TC-003"].remarks == "Beyond --limit"
    assert {e.test_case_id for e in factory.created} == {"TC-001", "TC-002"}


def test_limit_of_zero_executes_nothing(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, factory = _run(schema, path, book, limit=0)

    assert summary.executed == 0
    assert factory.created == ()


def test_negative_limit_is_rejected(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    with pytest.raises(WorkbookError, match="--limit must be 0 or greater"):
        _run(schema, path, book, limit=-1)


def test_fail_fast_stops_after_the_first_problem(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 5)])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-002", "source_result": 1, "target_result": 9},
        {"test_case_id": "TC-003", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-004", "source_result": 1, "target_result": 1},
    )

    summary, factory = _run(schema, path, book, fail_fast=True)

    results = _by_id(summary)
    assert results["TC-001"].status is ExecutionStatus.PASS
    assert results["TC-002"].status is ExecutionStatus.FAIL
    assert results["TC-003"].status is ExecutionStatus.SKIPPED
    assert results["TC-003"].remarks == "Stopped by --fail-fast"
    assert results["TC-004"].status is ExecutionStatus.SKIPPED
    # Every case is compiled first, so every case has an executor; what
    # --fail-fast stops is the *executing*, which only the first two reached.
    assert {e.test_case_id for e in factory.created if e.executed_sql} == {"TC-001", "TC-002"}


def test_without_fail_fast_every_case_runs(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 4)])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 9},
        {"test_case_id": "TC-002", "source_error": "boom"},
        {"test_case_id": "TC-003", "source_result": 1, "target_result": 1},
    )

    summary, _ = _run(schema, path, book)

    assert (summary.passed, summary.failed, summary.errored) == (1, 1, 1)
    assert summary.executed == 3


def test_results_are_written_to_a_new_timestamped_workbook(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = Path(make_workbook([case_row("TC-001")]))
    before = path.read_bytes()
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, _ = _run(schema, path, book)

    assert summary.output_path is not None
    assert summary.output_path.exists()
    assert summary.output_path != path
    assert "_results_" in summary.output_path.name
    assert path.read_bytes() == before, "the input workbook must never change"


def test_no_write_option_skips_the_output_workbook(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Path
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, _ = _run(schema, path, book, write_output=False)

    assert summary.output_path is None
    assert list(tmp_path.glob("*_results_*.xlsx")) == []


def test_durations_and_run_id_are_recorded(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, _ = _run(schema, path, book, run_id="fixedrunid001")

    result = _by_id(summary)["TC-001"]
    assert result.run_id == "fixedrunid001"
    assert result.duration_ms >= 0
    assert result.executed_at.tzinfo is not None
    assert result.executed_at.microsecond == 0


def test_results_are_ordered_by_workbook_row(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook(
        [case_row("TC-001"), case_row("TC-002", enabled=False), case_row("TC-003")]
    )
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-003", "source_result": 1, "target_result": 1},
    )

    summary, _ = _run(schema, path, book)

    assert [r.row_number for r in summary.results] == [2, 3, 4]


def test_multi_row_result_is_rejected_as_an_error(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """A query returning many rows must stop the case, never leak a result set."""
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "source_row_count": 25})

    summary, _ = _run(schema, path, book)

    result = _by_id(summary)["TC-001"]
    assert result.status is ExecutionStatus.ERROR
    assert result.error_side is ErrorSide.SOURCE
    assert "25 rows" in result.remarks


# ---------------------------------------------------------------------------
# Scope: a side the test does not name is never opened, let alone queried.
# ---------------------------------------------------------------------------


def test_target_only_case_never_opens_the_source(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook(
        [
            case_row(
                "TC-TGT-001",
                execution_scope="TARGET_ONLY",
                source_connection="",
                source_sql="",
                comparison_rule="expected_zero",
            )
        ]
    )
    book = make_results({"test_case_id": "TC-TGT-001", "target_result": 0})

    summary, factory = _run(schema, path, book)

    result = _by_id(summary)["TC-TGT-001"]
    assert result.status is ExecutionStatus.PASS
    assert result.target_result == 0
    assert result.source_result is None
    # The strongest guarantee: no source executor was ever created, so a
    # one-sided test cannot be blocked by a database it has no business opening.
    assert [executor.side for executor in factory.created] == [QuerySide.TARGET]


def test_source_only_case_never_opens_the_target(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook(
        [
            case_row(
                "TC-SRC-001",
                execution_scope="SOURCE_ONLY",
                target_connection="",
                target_sql="",
                comparison_rule="expected_zero",
            )
        ]
    )
    book = make_results({"test_case_id": "TC-SRC-001", "source_result": 0})

    summary, factory = _run(schema, path, book)

    result = _by_id(summary)["TC-SRC-001"]
    assert result.status is ExecutionStatus.PASS
    assert result.source_result == 0
    assert [executor.side for executor in factory.created] == [QuerySide.SOURCE]


def test_one_sided_expected_zero_still_fails_on_a_non_zero_count(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """Dropping the unused side must not make a real mismatch disappear."""
    path = make_workbook(
        [
            case_row(
                "TC-TGT-002",
                execution_scope="TARGET_ONLY",
                source_connection="",
                source_sql="",
                comparison_rule="expected_zero",
            )
        ]
    )
    book = make_results({"test_case_id": "TC-TGT-002", "target_result": 7})

    summary, _ = _run(schema, path, book)

    result = _by_id(summary)["TC-TGT-002"]
    assert result.status is ExecutionStatus.FAIL
    assert "target 7" in result.remarks


def test_no_comparison_records_a_value_without_passing_it(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """A profiling row is reported as PROFILED, never as a green test."""
    path = make_workbook(
        [
            case_row(
                "TC-TGT-003",
                execution_scope="TARGET_ONLY",
                source_connection="",
                source_sql="",
                comparison_rule="no_comparison",
            )
        ]
    )
    book = make_results({"test_case_id": "TC-TGT-003", "target_result": 42})

    summary, _ = _run(schema, path, book)

    result = _by_id(summary)["TC-TGT-003"]
    assert result.status is ExecutionStatus.PROFILED
    assert result.status is not ExecutionStatus.PASS
    assert result.target_result == 42
    assert summary.is_clean is True


def test_a_two_sided_rule_is_refused_on_a_one_sided_test(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """``equal`` has nothing to compare against, so it is an error, not a pass."""
    path = make_workbook(
        [
            case_row(
                "TC-TGT-004",
                execution_scope="TARGET_ONLY",
                source_connection="",
                source_sql="",
                comparison_rule="equal",
            )
        ]
    )
    book = make_results({"test_case_id": "TC-TGT-004", "target_result": 1})

    summary, _ = _run(schema, path, book)

    result = _by_id(summary)["TC-TGT-004"]
    assert result.status is ExecutionStatus.ERROR
    assert result.error_side is ErrorSide.COMPARISON
    assert "cannot be used with Execution_Scope TARGET_ONLY" in result.remarks


# ---------------------------------------------------------------------------
# The pre-execution gate: compile everything, execute nothing until it passes.
# ---------------------------------------------------------------------------


def test_one_broken_query_costs_one_result_not_all_of_them(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """An execute run finds a bad query by running it, and carries on."""
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 4)])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-002", "source_error": "Invalid object name 'Paymnets'"},
        {"test_case_id": "TC-003", "source_result": 1, "target_result": 1},
    )

    summary, _ = _run(schema, path, book)

    results = _by_id(summary)
    assert results["TC-002"].status is ExecutionStatus.ERROR
    assert "Invalid object name" in results["TC-002"].remarks
    # The sound queries still produced results.
    assert results["TC-001"].status is ExecutionStatus.PASS
    assert results["TC-003"].status is ExecutionStatus.PASS
    assert not summary.is_clean


def test_stop_policy_still_refuses_to_execute_anything(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """``--on-syntax-error stop``: a partial reconciliation is worse than none."""
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 4)])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-002", "source_syntax_error": "Invalid object name 'Paymnets'"},
        {"test_case_id": "TC-003", "source_result": 1, "target_result": 1},
    )

    summary, factory = _run(schema, path, book, on_syntax_error=OnSyntaxError.STOP)

    results = _by_id(summary)
    assert results["TC-002"].status is ExecutionStatus.ERROR
    assert results["TC-001"].status is ExecutionStatus.NOT_EXECUTED
    assert results["TC-003"].status is ExecutionStatus.NOT_EXECUTED
    # A row whose own SQL was fine carries no error and no observation.
    assert results["TC-001"].remarks == ""
    assert results["TC-001"].error_code == ""
    assert not any(executor.executed_sql for executor in factory.created)
    assert summary.stopped_by_validation
    assert not summary.is_clean


def test_a_clean_compile_pass_executes_every_test(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 4)])
    book = make_results(
        *(
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 4)
        )
    )

    summary, factory = _run(schema, path, book)

    assert [r.status for r in summary.results] == [ExecutionStatus.PASS] * 3
    assert summary.is_clean
    assert not summary.stopped_by_validation
    assert all(executor.executed_sql for executor in factory.created)


def test_an_execute_run_starts_executing_without_compiling_first(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001"), case_row("TC-002")])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-002", "source_syntax_error": "Invalid object name 'Paymnets'"},
    )
    lines: list[str] = []
    factory = FakeExecutorFactory(book)
    ReconciliationRunner(schema, factory).run(
        path, RunOptions(write_output=False), on_progress=lines.append
    )
    joined = "\n".join(lines)

    # An execute run does not compile first: it starts executing.
    assert "Validation phase" not in joined
    assert "Execution phase: running 2 test(s), one at a time." in joined
    assert "TC-001" in joined
    assert "-> PASS" in joined


def test_the_progress_log_shows_the_query_but_never_a_credential(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """Debugging needs the SQL on screen; it must never need the password too."""
    path = make_workbook([case_row("TC-001", source_sql="SELECT COUNT(*) FROM SOME_TABLE")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})
    lines: list[str] = []
    ReconciliationRunner(schema, FakeExecutorFactory(book)).run(
        path, RunOptions(write_output=False), on_progress=lines.append
    )
    joined = "\n".join(lines)

    # The point of the debug line: the query actually sent is visible.
    assert "SELECT COUNT(*) FROM SOME_TABLE" in joined
    assert "password" not in joined.casefold()
    assert "pwd" not in joined.casefold()


def test_the_validation_errors_sheet_names_every_rejected_query(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    from openpyxl import load_workbook

    path = make_workbook([case_row("TC-001"), case_row("TC-002")])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-002", "source_syntax_error": "Invalid object name 'Paymnets'"},
    )

    summary, _ = _run(
        schema, path, book, mode=RunMode.VALIDATE, write_output=True, output_dir=tmp_path
    )

    assert summary.output_path is not None
    result = load_workbook(summary.output_path)
    assert "Validation Errors" in result.sheetnames
    sheet = result["Validation Errors"]
    headers = [cell.value for cell in sheet[1]]
    body = [[cell.value for cell in row] for row in sheet.iter_rows(min_row=2)]
    assert len(body) == 1
    assert body[0][headers.index("Test_ID")] == "TC-002"
    assert body[0][headers.index("Status")] == "SYNTAX ERROR"
    assert "Invalid object name" in body[0][headers.index("Error_Detail")]
    # The original workbook never gains the sheet.
    assert "Validation Errors" not in load_workbook(path).sheetnames


def test_a_clean_run_leaves_no_validation_errors_sheet(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    from openpyxl import load_workbook

    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, _ = _run(schema, path, book, write_output=True, output_dir=tmp_path)

    assert summary.output_path is not None
    assert "Validation Errors" not in load_workbook(summary.output_path).sheetnames


def test_the_syntax_validation_sheet_records_every_test_that_was_checked(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    """The full record, not just the failures: evidence every query was compiled."""
    from openpyxl import load_workbook

    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 4)])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-002", "source_syntax_error": "Invalid object name 'Paymnets'"},
        {"test_case_id": "TC-003", "source_result": 1, "target_result": 1},
    )

    summary, _ = _run(
        schema, path, book, mode=RunMode.VALIDATE, write_output=True, output_dir=tmp_path
    )

    assert summary.output_path is not None
    sheet = load_workbook(summary.output_path)["Syntax Validation"]
    headers = [cell.value for cell in sheet[1]]
    body = [[cell.value for cell in row] for row in sheet.iter_rows(min_row=2)]
    by_id = {row[headers.index("Test_ID")]: row for row in body}

    assert set(by_id) == {"TC-001", "TC-002", "TC-003"}
    assert by_id["TC-001"][headers.index("Result")] == "OK"
    assert by_id["TC-003"][headers.index("Result")] == "OK"
    assert by_id["TC-002"][headers.index("Result")] == "SYNTAX ERROR"
    assert "Invalid object name" in by_id["TC-002"][headers.index("Error_Detail")]


def test_the_syntax_validation_sheet_is_written_even_when_everything_compiles(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    from openpyxl import load_workbook

    path = make_workbook([case_row("TC-001"), case_row("TC-002")])
    book = make_results(
        *({"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1} for i in (1, 2))
    )

    summary, _ = _run(
        schema, path, book, mode=RunMode.VALIDATE, write_output=True, output_dir=tmp_path
    )

    assert summary.output_path is not None
    result = load_workbook(summary.output_path)
    # The report is always there; the errors sheet only when something failed.
    assert "Syntax Validation" in result.sheetnames
    assert "Validation Errors" not in result.sheetnames
    sheet = result["Syntax Validation"]
    headers = [cell.value for cell in sheet[1]]
    body = [[cell.value for cell in row] for row in sheet.iter_rows(min_row=2)]
    assert len(body) == 2
    assert {row[headers.index("Result")] for row in body} == {"OK"}
    # The original workbook never gains either sheet.
    assert "Syntax Validation" not in load_workbook(path).sheetnames


# ---------------------------------------------------------------------------
# Run mode: validate only, or validate then execute.
# ---------------------------------------------------------------------------


def test_validate_mode_compiles_every_query_and_executes_none(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 4)])
    book = make_results(
        *(
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 4)
        )
    )

    summary, factory = _run(schema, path, book, mode=RunMode.VALIDATE)

    assert [r.status for r in summary.results] == [ExecutionStatus.VALIDATED] * 3
    assert all(executor.validated_sql for executor in factory.created)
    assert not any(executor.executed_sql for executor in factory.created)
    # Compiling everything successfully is a clean run, not an incomplete one.
    assert summary.is_clean
    assert summary.validated == 3


def test_validate_mode_still_reports_a_query_that_will_not_compile(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001"), case_row("TC-002")])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-002", "source_syntax_error": "Invalid object name 'Paymnets'"},
    )

    summary, factory = _run(schema, path, book, mode=RunMode.VALIDATE)

    results = _by_id(summary)
    assert results["TC-002"].status is ExecutionStatus.ERROR
    assert not summary.is_clean
    assert not any(executor.executed_sql for executor in factory.created)


def test_execute_mode_is_the_default_and_runs_the_queries(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, factory = _run(schema, path, book)

    assert RunOptions().mode is RunMode.EXECUTE
    assert _by_id(summary)["TC-001"].status is ExecutionStatus.PASS
    assert any(executor.executed_sql for executor in factory.created)


def test_validate_mode_still_writes_the_validation_sheet(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    from openpyxl import load_workbook

    path = make_workbook([case_row("TC-001"), case_row("TC-002")])
    book = make_results(
        *({"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1} for i in (1, 2))
    )

    summary, _ = _run(
        schema, path, book, mode=RunMode.VALIDATE, write_output=True, output_dir=tmp_path
    )

    assert summary.output_path is not None
    result = load_workbook(summary.output_path)
    assert "Syntax Validation" in result.sheetnames
    assert "Validation Errors" not in result.sheetnames
    sheet = result["Syntax Validation"]
    headers = [cell.value for cell in sheet[1]]
    body = [[cell.value for cell in row] for row in sheet.iter_rows(min_row=2)]
    assert {row[headers.index("Result")] for row in body} == {"OK"}


def test_validate_mode_says_it_stopped_on_purpose(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """A validation run that executes nothing must not look like a failure."""
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})
    lines: list[str] = []
    ReconciliationRunner(schema, FakeExecutorFactory(book)).run(
        path,
        RunOptions(write_output=False, mode=RunMode.VALIDATE),
        on_progress=lines.append,
    )
    joined = "\n".join(lines)

    assert "Validation phase" in joined
    assert "Validation run: stopping here by request." in joined
    assert "Execution phase" not in joined


def test_each_row_carries_only_its_own_validation_error(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """Two different broken queries must not end up sharing one message."""
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 5)])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 1},
        {"test_case_id": "TC-002", "source_syntax_error": "Invalid object name 'Paymnets'"},
        {"test_case_id": "TC-003", "source_syntax_error": "Incorrect syntax near 'FORM'"},
        {"test_case_id": "TC-004", "source_result": 1, "target_result": 1},
    )

    summary, _ = _run(schema, path, book, mode=RunMode.VALIDATE)
    results = _by_id(summary)

    assert "Invalid object name" in results["TC-002"].remarks
    assert "Incorrect syntax" not in results["TC-002"].remarks
    assert "Incorrect syntax" in results["TC-003"].remarks
    assert "Invalid object name" not in results["TC-003"].remarks
    # The two sound queries ran, and report their own comparison — never
    # someone else's error.
    for good in ("TC-001", "TC-004"):
        assert results[good].status is ExecutionStatus.VALIDATED
        assert results[good].error_code == ""
        assert "Invalid object name" not in results[good].remarks
        assert "Incorrect syntax" not in results[good].remarks


def test_a_validated_row_carries_no_error_and_no_observation(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001"), case_row("TC-002")])
    book = make_results(
        *({"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1} for i in (1, 2))
    )

    summary, _ = _run(schema, path, book, mode=RunMode.VALIDATE)

    for result in summary.results:
        assert result.status is ExecutionStatus.VALIDATED
        assert result.remarks == ""
        assert result.error_code == ""
        assert result.error_side is ErrorSide.NONE


# ---------------------------------------------------------------------------
# Dialect mismatches are caught offline, before any round trip.
# ---------------------------------------------------------------------------


def test_foreign_dialect_is_caught_without_asking_the_database(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """Oracle SQL bound for a SQL Server connection needs no round trip to refuse."""
    path = make_workbook(
        [
            case_row(
                "TC-ORA-001",
                source_type="sqlserver",
                source_sql="SELECT COUNT(*) FROM V WHERE CREATED >= DATE '2023-01-01'",
            )
        ]
    )
    book = make_results({"test_case_id": "TC-ORA-001", "source_result": 1, "target_result": 1})

    summary, factory = _run(schema, path, book)

    result = _by_id(summary)["TC-ORA-001"]
    assert result.status is ExecutionStatus.ERROR
    assert result.error_code == "SQL_DIALECT_MISMATCH"
    assert "Oracle syntax" in result.remarks
    assert result.error_side is ErrorSide.SOURCE
    # The point of the check: the database was never asked about this query.
    assert not any(executor.validated_sql for executor in factory.created)
    assert not any(executor.executed_sql for executor in factory.created)


def test_portable_sql_still_goes_to_the_database(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """The offline check never replaces the compiler; it only answers early."""
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    summary, factory = _run(schema, path, book, mode=RunMode.VALIDATE)

    assert _by_id(summary)["TC-001"].status is ExecutionStatus.VALIDATED
    assert any(executor.validated_sql for executor in factory.created)


def test_a_foreign_dialect_costs_one_result_not_the_run(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """Judged against the connection the row itself names.

    A workbook whose sources span two platforms is legal once each row names
    its own connection, so a wrongly-paired row is an error like any other and
    the correctly-paired rows still run.
    """
    path = make_workbook(
        [
            case_row("TC-001"),
            # The fixture's source is Oracle, so T-SQL is the mismatch here.
            case_row("TC-BAD", source_sql="SELECT COUNT_BIG(*) FROM dbo.T"),
            case_row("TC-002"),
        ]
    )
    book = make_results(
        *(
            {"test_case_id": t, "source_result": 1, "target_result": 1}
            for t in ("TC-001", "TC-BAD", "TC-002")
        )
    )

    summary, factory = _run(schema, path, book)

    results = _by_id(summary)
    assert results["TC-BAD"].status is ExecutionStatus.ERROR
    assert results["TC-BAD"].error_code == "SQL_DIALECT_MISMATCH"
    assert "SQL Server syntax" in results["TC-BAD"].remarks
    # The correctly-paired rows still ran.
    assert results["TC-001"].status is ExecutionStatus.PASS
    assert results["TC-002"].status is ExecutionStatus.PASS
    assert any(executor.executed_sql for executor in factory.created)
    assert not summary.is_clean


def test_a_dialect_mismatch_is_explained_and_names_the_rows(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001"), case_row("TC-BAD", source_sql="SELECT GETDATE()")])
    book = make_results(
        *({"test_case_id": t, "source_result": 1, "target_result": 1} for t in ("TC-001", "TC-BAD"))
    )
    lines: list[str] = []
    ReconciliationRunner(schema, FakeExecutorFactory(book)).run(
        path, RunOptions(write_output=False), on_progress=lines.append
    )
    joined = "\n".join(lines)

    assert "name a connection that speaks a different dialect" in joined
    assert "TC-BAD" in joined
    # The run carries on with the rows that are correctly paired.
    assert "Execution phase: running 1 test(s), one at a time." in joined


def test_a_mismatched_run_still_writes_its_evidence(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    from openpyxl import load_workbook

    path = make_workbook([case_row("TC-001"), case_row("TC-BAD", source_sql="SELECT GETDATE()")])
    book = make_results(
        *({"test_case_id": t, "source_result": 1, "target_result": 1} for t in ("TC-001", "TC-BAD"))
    )

    summary, _ = _run(schema, path, book, write_output=True, output_dir=tmp_path)

    assert summary.output_path is not None
    result = load_workbook(summary.output_path)
    sheet = result["Syntax Validation"]
    headers = [cell.value for cell in sheet[1]]
    body = [[cell.value for cell in row] for row in sheet.iter_rows(min_row=2)]
    by_id = {row[headers.index("Test_ID")]: row for row in body}

    assert by_id["TC-BAD"][headers.index("Result")] == "PLATFORM MISMATCH"
    # An execute run compiles nothing, so the correctly-paired row is absent
    # rather than claimed as checked.
    assert "TC-001" not in by_id


# ---------------------------------------------------------------------------
# --start-row / --end-row: run one slice of a large sheet.
# ---------------------------------------------------------------------------


def test_a_row_range_runs_only_that_slice(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """Rows are addressed the way Excel and every log line already number them."""
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 6)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 6)
        ]
    )

    summary, factory = _run(schema, path, book, start_row=3, end_row=4)

    results = _by_id(summary)
    ran = {e.test_case_id for e in factory.created}
    # The template puts the first case on row 2, so rows 3-4 are cases 2-3.
    assert ran == {"TC-002", "TC-003"}
    assert results["TC-001"].status is ExecutionStatus.SKIPPED
    assert results["TC-001"].remarks == "Outside --start-row 3/--end-row 4"
    assert results["TC-005"].status is ExecutionStatus.SKIPPED


def test_a_start_row_alone_runs_to_the_end(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 6)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 6)
        ]
    )

    summary, factory = _run(schema, path, book, start_row=5)

    assert {e.test_case_id for e in factory.created} == {"TC-004", "TC-005"}
    assert _by_id(summary)["TC-001"].remarks == "Outside --start-row 5/--end-row end"


def test_an_end_row_alone_runs_from_the_first(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 6)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 6)
        ]
    )

    summary, factory = _run(schema, path, book, end_row=3)

    assert {e.test_case_id for e in factory.created} == {"TC-001", "TC-002"}
    assert _by_id(summary)["TC-005"].remarks == "Outside --start-row 1/--end-row 3"


def test_an_end_row_before_the_start_row_is_refused(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """Better to say so than to run nothing and call it a clean sheet."""
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    with pytest.raises(WorkbookError, match="must be greater than or equal to"):
        _run(schema, path, book, start_row=9, end_row=4)


def test_a_start_row_below_one_is_refused(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})

    with pytest.raises(WorkbookError, match="--start-row must be 1 or greater"):
        _run(schema, path, book, start_row=0)


def test_a_row_range_narrows_an_explicit_case_selection(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """--case picks candidates; the range then narrows them, never widens."""
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 6)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 6)
        ]
    )

    _, factory = _run(
        schema, path, book, case_ids=("TC-001", "TC-002", "TC-005"), start_row=3, end_row=4
    )

    assert {e.test_case_id for e in factory.created} == {"TC-002"}


# ---------------------------------------------------------------------------
# The result workbook is written as the run goes, not at the end.
# ---------------------------------------------------------------------------


def _status_count(path: Any, schema: Any) -> int:
    """How many rows on the sheet already carry a Status."""
    from openpyxl import load_workbook

    sheet = load_workbook(path)[schema.sheet_name]
    header = {str(c.value).strip(): c.column for c in sheet[schema.header_row] if c.value}
    column = header["Status"]
    return sum(
        1
        for row in range(schema.first_data_row, sheet.max_row + 1)
        if sheet.cell(row=row, column=column).value
    )


def test_the_result_file_exists_before_the_first_query_runs(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    """A run killed on test one still leaves a real workbook, not nothing."""
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 4)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 4)
        ]
    )
    out = tmp_path / "out"
    out.mkdir()
    seen: list[int] = []

    def watch(message: str) -> None:
        if message.startswith("      -> "):
            produced = sorted(out.glob("*.xlsx"))
            seen.append(_status_count(produced[0], schema) if produced else -1)

    ReconciliationRunner(schema, FakeExecutorFactory(book)).run(
        path, RunOptions(output_dir=out), on_progress=watch
    )

    # One more row carries a result after each test than before it.
    assert seen == sorted(seen), "the file must only ever grow"
    assert seen[0] >= 1, "the first test's result is on disk before the second runs"
    assert seen[-1] > seen[0], "later tests keep landing in the same file"


def test_rows_outside_the_range_are_written_before_anything_runs(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    """Their outcome is already decided, so the file shows it from the start."""
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 6)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 6)
        ]
    )
    out = tmp_path / "out"
    out.mkdir()
    first_seen: list[int] = []

    def watch(message: str) -> None:
        if message.startswith("Execution phase") and not first_seen:
            produced = sorted(out.glob("*.xlsx"))
            first_seen.append(_status_count(produced[0], schema) if produced else -1)

    ReconciliationRunner(schema, FakeExecutorFactory(book)).run(
        path, RunOptions(output_dir=out, start_row=3, end_row=4), on_progress=watch
    )

    # Five enabled rows, two of them in range: the other three are already
    # decided and on disk before the first query runs.
    assert first_seen == [3]


def test_the_finished_file_matches_a_whole_run(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    """Writing row by row must leave exactly what one final write would have."""
    from openpyxl import load_workbook

    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 4)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 4)
        ]
    )

    summary, _ = _run(schema, path, book, write_output=True, output_dir=tmp_path)

    assert summary.output_path is not None
    sheet = load_workbook(summary.output_path)[schema.sheet_name]
    header = {str(c.value).strip(): c.column for c in sheet[schema.header_row] if c.value}
    statuses = [
        sheet.cell(row=row, column=header["Status"]).value
        for row in range(schema.first_data_row, schema.first_data_row + 3)
    ]
    assert statuses == ["PASS", "PASS", "PASS"]
    # The input workbook is still untouched, as it always was.
    assert _status_count(path, schema) == 0


def test_no_write_leaves_no_file_at_all(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    book = make_results({"test_case_id": "TC-001", "source_result": 1, "target_result": 1})
    out = tmp_path / "out"
    out.mkdir()

    summary = ReconciliationRunner(schema, FakeExecutorFactory(book)).run(
        path, RunOptions(output_dir=out, write_output=False)
    )

    assert summary.output_path is None
    assert list(out.glob("*.xlsx")) == []


# ---------------------------------------------------------------------------
# Every test case reaches the log, exactly once, however it turned out.
# ---------------------------------------------------------------------------


def _log_of_run(schema: Any, path: Any, book: Any, **option_overrides: Any) -> tuple[str, Any]:
    lines: list[str] = []
    summary = ReconciliationRunner(schema, FakeExecutorFactory(book)).run(
        path, RunOptions(write_output=False, **option_overrides), on_progress=lines.append
    )
    return "\n".join(lines), summary


def test_every_test_case_appears_in_the_log(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """A row missing from the log cannot be told from one nobody selected."""
    path = make_workbook(
        [case_row("TC-001"), case_row("TC-002"), case_row("TC-OFF", enabled=False)]
    )
    book = make_results(
        *[{"test_case_id": t, "source_result": 1, "target_result": 1} for t in ("TC-001", "TC-002")]
    )

    joined, summary = _log_of_run(schema, path, book, start_row=2, end_row=2)

    for result in summary.results:
        assert result.test_case_id in joined, f"{result.test_case_id} never reached the log"


def test_rows_stopped_by_fail_fast_are_logged_not_dropped(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 5)])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 9},
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(2, 5)
        ],
    )

    joined, summary = _log_of_run(schema, path, book, fail_fast=True)

    assert "Stopped by --fail-fast" in joined
    for result in summary.results:
        assert result.test_case_id in joined
    # The ones that never ran say so rather than vanishing.
    halted = [line for line in joined.splitlines() if "TC-004" in line]
    assert halted and all("SKIPPED" in line for line in halted)


def test_a_case_is_never_logged_or_recorded_twice(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """A dialect mismatch is found offline and must not be counted again."""
    path = make_workbook(
        [
            case_row("TC-001"),
            # The fixture's source is Oracle, so T-SQL is the mismatch here.
            case_row("TC-BAD", source_sql="SELECT COUNT_BIG(*) FROM dbo.T"),
        ]
    )
    book = make_results(
        *[{"test_case_id": t, "source_result": 1, "target_result": 1} for t in ("TC-001", "TC-BAD")]
    )

    joined, summary = _log_of_run(schema, path, book, mode=RunMode.VALIDATE)

    seen = Counter(r.test_case_id for r in summary.results)
    assert seen["TC-BAD"] == 1, "the mismatch was recorded twice"
    assert seen["TC-001"] == 1
    # One result line for it in the log, not two.
    rows = [line for line in joined.splitlines() if line.startswith("  row") and "TC-BAD" in line]
    assert len(rows) == 1, rows


def test_the_dialect_report_names_every_row_it_rejected(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any
) -> None:
    """More than five mismatches must still all be named, not summarised away."""
    rows = [
        case_row(f"TC-{i:03d}", source_sql="SELECT COUNT_BIG(*) FROM dbo.T") for i in range(1, 8)
    ]
    path = make_workbook(rows)
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 8)
        ]
    )

    joined, _ = _log_of_run(schema, path, book)

    for i in range(1, 8):
        assert f"TC-{i:03d}" in joined
    assert "and 2 more" not in joined


# ---------------------------------------------------------------------------
# Rows decided as a group are written as a group, not one save each.
# ---------------------------------------------------------------------------


def _count_saves(monkeypatch: Any) -> dict[str, int]:
    """Count how many times the result workbook is written to disk."""
    from migration_reconciliation.workbook import writer as writer_module

    saves = {"n": 0}
    original = writer_module.ResultWriter._save

    def counting(self: Any) -> None:
        saves["n"] += 1
        original(self)

    monkeypatch.setattr(writer_module.ResultWriter, "_save", counting)
    return saves


def test_rows_outside_the_range_cost_one_save_not_one_each(
    make_workbook: Any,
    case_row: Any,
    schema: Any,
    make_results: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """Their outcome was never in doubt; rewriting the file per row says nothing."""
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 11)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 11)
        ]
    )
    out = tmp_path / "out"
    out.mkdir()
    saves = _count_saves(monkeypatch)

    ReconciliationRunner(schema, FakeExecutorFactory(book)).run(
        path, RunOptions(output_dir=out, start_row=3, end_row=4)
    )

    # One to create the file, one for the eight skipped rows together, one per
    # executed test, one to finish. Not one per row.
    assert saves["n"] == 5


def test_each_executed_test_still_gets_its_own_save(
    make_workbook: Any,
    case_row: Any,
    schema: Any,
    make_results: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """The live file is the point: an executed row must land as it happens."""
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 5)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 5)
        ]
    )
    out = tmp_path / "out"
    out.mkdir()
    saves = _count_saves(monkeypatch)

    ReconciliationRunner(schema, FakeExecutorFactory(book)).run(path, RunOptions(output_dir=out))

    # Create, four executed tests, finish. Nothing was batched away.
    assert saves["n"] == 6


def test_fail_fast_writes_the_abandoned_rows_in_one_go(
    make_workbook: Any,
    case_row: Any,
    schema: Any,
    make_results: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 8)])
    book = make_results(
        {"test_case_id": "TC-001", "source_result": 1, "target_result": 9},
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(2, 8)
        ],
    )
    out = tmp_path / "out"
    out.mkdir()
    saves = _count_saves(monkeypatch)

    summary = ReconciliationRunner(schema, FakeExecutorFactory(book)).run(
        path, RunOptions(output_dir=out, fail_fast=True)
    )

    # Create, the one test that ran, the six abandoned rows together, finish.
    assert saves["n"] == 4
    skipped = [r for r in summary.results if r.status is ExecutionStatus.SKIPPED]
    assert len(skipped) == 6
    assert all(r.remarks == "Stopped by --fail-fast" for r in skipped)


def test_batching_writes_exactly_the_same_rows(
    make_workbook: Any, case_row: Any, schema: Any, make_results: Any, tmp_path: Any
) -> None:
    """Fewer saves must not mean fewer results on the sheet."""
    from openpyxl import load_workbook

    path = make_workbook([case_row(f"TC-{i:03d}") for i in range(1, 7)])
    book = make_results(
        *[
            {"test_case_id": f"TC-{i:03d}", "source_result": 1, "target_result": 1}
            for i in range(1, 7)
        ]
    )

    summary, _ = _run(
        schema, path, book, start_row=3, end_row=4, write_output=True, output_dir=tmp_path
    )

    assert summary.output_path is not None
    sheet = load_workbook(summary.output_path)[schema.sheet_name]
    header = {str(c.value).strip(): c.column for c in sheet[schema.header_row] if c.value}
    written = [
        sheet.cell(row=row, column=header["Status"]).value
        for row in range(schema.first_data_row, schema.first_data_row + 6)
    ]
    # Every row carries a status: the skipped ones too, batched or not.
    assert all(written), written
    assert written[1] == "PASS" and written[2] == "PASS"
    assert written[0] == "SKIPPED" and written[5] == "SKIPPED"
