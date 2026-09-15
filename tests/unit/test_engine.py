"""Executing a plan: scopes, failures, statuses and what is never opened."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from migration_reconciliation.errors import (
    DatabaseExecutionError,
    NonScalarResultError,
    QueryTimeoutError,
    SqlSyntaxError,
    SyntaxCheckUnavailableError,
)
from migration_reconciliation.execution.engine import RunReport, execute_plan
from migration_reconciliation.execution.plan import ExecutionPlan, build_plan
from migration_reconciliation.models import ErrorCode, Platform, TestStatus
from tests.scripted import ScriptedExecutors

SOURCE_SQL = "SELECT COUNT(*) FROM webservice.dbo.Payments"
TARGET_SQL = "SELECT COUNT(*) FROM dbo.Payments"


def plan_for(path: Path, *, dry_run: bool = False) -> ExecutionPlan:
    plan = build_plan(path)
    if not dry_run:
        return plan
    return ExecutionPlan(
        workbook_path=plan.workbook_path,
        sheet=plan.sheet,
        control=plan.control.with_overrides(dry_run=True),
        rules=plan.rules,
        warnings=plan.warnings,
        ignored_sheets=plan.ignored_sheets,
    )


def run(
    path: Path,
    executors: ScriptedExecutors | None = None,
    *,
    dry_run: bool = False,
    write_output: bool = False,
) -> RunReport:
    return execute_plan(
        plan_for(path, dry_run=dry_run),
        executors=executors,
        run_id="run-under-test",
        write_output=write_output,
    )


def outcome_of(report: RunReport, test_id: str) -> Any:
    return next(o for o in report.outcomes if o.test_id == test_id)


def source_only_row(test_row: Any, **overrides: Any) -> Mapping[str, Any]:
    row = test_row(
        "TC-SRC",
        Execution_Scope="SOURCE_ONLY",
        Comparison_Type="EXPECTED_ZERO",
        Target_Profile_Section=None,
        Target_SQL=None,
    )
    return {**row, **overrides}


def target_only_row(test_row: Any, **overrides: Any) -> Mapping[str, Any]:
    row = test_row(
        "TC-TGT",
        Execution_Scope="TARGET_ONLY",
        Comparison_Type="EXPECTED_ZERO",
        Source_Profile_Section=None,
        Source_SQL=None,
    )
    return {**row, **overrides}


# -- the three scopes --------------------------------------------------------


def test_a_source_target_test_runs_both_queries_and_compares_them(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(results={"source": 100, "target": 100})

    report = run(path, executors)

    assert outcome_of(report, "TC-001").status is TestStatus.PASS
    assert executors.sql_for("source") == [SOURCE_SQL]
    assert executors.sql_for("target") == [TARGET_SQL]


def test_a_mismatch_fails_with_the_variance_recorded(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])

    report = run(path, ScriptedExecutors(results={"source": 100, "target": 97}))

    outcome = outcome_of(report, "TC-001")
    assert outcome.status is TestStatus.FAIL
    assert outcome.error_code == ErrorCode.VALUE_MISMATCH.value
    assert outcome.platform is Platform.COMPARISON
    assert outcome.variance == -3


def test_a_source_only_test_never_touches_the_target(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([source_only_row(test_row)])
    executors = ScriptedExecutors(results={"source": 0, "target": 999})

    report = run(path, executors)

    assert outcome_of(report, "TC-SRC").status is TestStatus.PASS
    assert executors.queried("source")
    assert not executors.queried("target")


def test_a_target_only_test_never_touches_the_source(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([target_only_row(test_row)])
    executors = ScriptedExecutors(results={"source": 999, "target": 0})

    report = run(path, executors)

    assert outcome_of(report, "TC-TGT").status is TestStatus.PASS
    assert not executors.queried("source")


def test_a_one_sided_workbook_needs_only_one_connection(
    make_recon_workbook: Any, test_row: Any
) -> None:
    """The unused side is never opened, so it is never asked for a password."""
    path = make_recon_workbook([target_only_row(test_row)])

    assert build_plan(path).required_sections == ("target",)


# -- disabled ----------------------------------------------------------------


def test_a_disabled_test_is_marked_disabled_and_never_executed(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-OFF", Enabled="No"), test_row("TC-ON")])
    executors = ScriptedExecutors(results={"source": 1, "target": 1})

    report = run(path, executors)

    assert outcome_of(report, "TC-OFF").status is TestStatus.DISABLED
    assert executors.sql_for("source") == [SOURCE_SQL]  # only the enabled test ran


def test_disabled_tests_are_not_part_of_any_total(make_recon_workbook: Any, test_row: Any) -> None:
    path = make_recon_workbook([test_row("TC-OFF", Enabled="No"), test_row("TC-ON")])

    report = run(path, ScriptedExecutors(results={"source": 1, "target": 1}))

    assert report.enabled_tests == 1
    assert report.disabled == 1
    assert report.overall_status == "PASS"


# -- query failures ----------------------------------------------------------


def test_a_timeout_is_reported_as_its_own_error_code(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(
        results={
            "source": 5,
            "target": QueryTimeoutError("Target query exceeded the 120s timeout."),
        }
    )

    outcome = outcome_of(run(path, executors), "TC-001")

    assert outcome.status is TestStatus.ERROR
    assert outcome.error_code == ErrorCode.QUERY_TIMEOUT.value
    assert outcome.platform is Platform.TARGET


def test_a_non_scalar_result_stops_its_test(make_recon_workbook: Any, test_row: Any) -> None:
    """More than one row would mean client data entering this process."""
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(
        results={
            "source": NonScalarResultError("Source query returned 42 rows; one is required"),
            "target": 1,
        }
    )

    outcome = outcome_of(run(path, executors), "TC-001")

    assert outcome.status is TestStatus.ERROR
    assert outcome.error_code == ErrorCode.NON_SCALAR_RESULT.value
    assert outcome.platform is Platform.SOURCE


def test_a_failing_query_stops_before_the_other_side_runs(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(
        results={"source": DatabaseExecutionError("Source query failed"), "target": 1}
    )

    run(path, executors)

    assert not executors.queried("target")


def test_a_driver_error_is_sanitized_before_it_reaches_a_cell(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    leaky = DatabaseExecutionError(
        "Login failed. DRIVER={ODBC Driver 18 for SQL Server};SERVER=sql01;"
        "UID=svc_recon;PWD=hunter2;DATABASE=webservice"
    )
    executors = ScriptedExecutors(results={"source": leaky, "target": 1})

    outcome = outcome_of(run(path, executors), "TC-001")

    assert "hunter2" not in outcome.error_detail
    assert "hunter2" not in outcome.observation
    assert outcome.error_code == ErrorCode.QUERY_EXECUTION_FAILED.value


def test_an_unreachable_cross_database_object_explains_itself(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook(
        [test_row("TC-001", Source_SQL="SELECT COUNT(*) FROM etables.dbo.Fees")]
    )
    executors = ScriptedExecutors(
        results={
            "source": DatabaseExecutionError("Invalid object name 'etables.dbo.Fees'."),
            "target": 1,
        }
    )

    outcome = outcome_of(run(path, executors), "TC-001")

    assert "linked-server" in outcome.error_detail or "linked server" in outcome.error_detail
    assert "etables.dbo.Fees" in outcome.error_detail


def test_the_configured_timeout_is_applied_to_every_query(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    path = make_recon_workbook(
        [test_row("TC-001")], run_control=run_control_settings(Query_Timeout_Seconds=45)
    )
    executors = ScriptedExecutors(results={"source": 1, "target": 1})

    run(path, executors)

    assert [call.timeout_seconds for call in executors.calls] == [45, 45]


# -- type conversion ---------------------------------------------------------


def test_a_value_that_is_not_the_declared_type_errors_rather_than_reconciles(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(results={"source": "not a number", "target": 1})

    outcome = outcome_of(run(path, executors), "TC-001")

    assert outcome.status is TestStatus.ERROR
    assert outcome.error_code == ErrorCode.TYPE_CONVERSION_ERROR.value
    assert outcome.platform is Platform.SOURCE


def test_a_null_result_is_named_as_such(make_recon_workbook: Any, test_row: Any) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(results={"source": None, "target": 1})

    outcome = outcome_of(run(path, executors), "TC-001")

    assert outcome.status is TestStatus.ERROR
    assert outcome.error_code == ErrorCode.NULL_RESULT.value


# -- connections -------------------------------------------------------------


def test_a_connection_that_will_not_open_blocks_only_what_needs_it(
    make_recon_workbook: Any, test_row: Any
) -> None:
    rows: Sequence[Mapping[str, Any]] = [test_row("TC-BOTH"), target_only_row(test_row)]
    path = make_recon_workbook(rows)
    executors = ScriptedExecutors(
        results={"source": 1, "target": 0},
        connect_failures={"source": "Cannot connect to the source database."},
    )

    report = run(path, executors)

    assert outcome_of(report, "TC-BOTH").status is TestStatus.BLOCKED
    assert outcome_of(report, "TC-BOTH").error_code == ErrorCode.CONNECTION_FAILED.value
    assert outcome_of(report, "TC-TGT").status is TestStatus.PASS


def test_every_connection_is_closed_even_when_a_test_explodes(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(results={"source": RuntimeError("driver exploded"), "target": 1})

    run(path, executors)

    assert executors.closed


# -- run control behaviour ---------------------------------------------------


def test_a_run_continues_past_an_error_by_default(make_recon_workbook: Any, test_row: Any) -> None:
    failing_sql = "SELECT COUNT(*) FROM dbo.PaymentsB"
    path = make_recon_workbook(
        [
            test_row("TC-001"),
            test_row("TC-002", Target_SQL=failing_sql),
            test_row("TC-003"),
        ]
    )
    executors = ScriptedExecutors(
        results={"source": 1, "target": 1},
        by_sql={("target", failing_sql): DatabaseExecutionError("boom")},
    )

    report = run(path, executors)

    assert outcome_of(report, "TC-001").status is TestStatus.PASS
    assert outcome_of(report, "TC-002").status is TestStatus.ERROR
    assert outcome_of(report, "TC-003").status is TestStatus.PASS


def test_continue_on_test_error_no_stops_and_records_what_did_not_run(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    rows = [test_row("TC-001"), test_row("TC-002")]
    path = make_recon_workbook(rows, run_control=run_control_settings(Continue_On_Test_Error="No"))
    executors = ScriptedExecutors(results={"source": DatabaseExecutionError("boom"), "target": 1})

    report = run(path, executors)

    assert outcome_of(report, "TC-001").status is TestStatus.ERROR
    assert outcome_of(report, "TC-002").status is TestStatus.NOT_EXECUTED
    assert report.overall_status == "ERROR"


def test_a_run_with_an_unexecuted_test_is_never_a_pass(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    rows = [test_row("TC-001"), test_row("TC-002")]
    path = make_recon_workbook(rows, run_control=run_control_settings(Continue_On_Test_Error="No"))
    executors = ScriptedExecutors(results={"source": NonScalarResultError("two rows"), "target": 1})

    report = run(path, executors)

    assert report.not_executed == 1
    assert report.overall_status != "PASS"


def test_a_critical_config_error_stops_before_any_database_is_opened(
    make_recon_workbook: Any, test_row: Any
) -> None:
    rows = [test_row("TC-GOOD"), test_row("TC-BAD", Execution_Scope="SIDEWAYS")]
    path = make_recon_workbook(rows)
    executors = ScriptedExecutors(results={"source": 1, "target": 1})

    report = run(path, executors)

    assert outcome_of(report, "TC-BAD").status is TestStatus.CONFIG_ERROR
    assert outcome_of(report, "TC-GOOD").status is TestStatus.NOT_EXECUTED
    assert executors.opened == []
    assert executors.calls == []


def test_config_errors_do_not_stop_a_run_when_configured_not_to(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    rows = [test_row("TC-GOOD"), test_row("TC-BAD", Execution_Scope="SIDEWAYS")]
    path = make_recon_workbook(
        rows, run_control=run_control_settings(Stop_On_Critical_Config_Error="No")
    )
    executors = ScriptedExecutors(results={"source": 1, "target": 1})

    report = run(path, executors)

    assert outcome_of(report, "TC-GOOD").status is TestStatus.PASS
    assert outcome_of(report, "TC-BAD").status is TestStatus.CONFIG_ERROR


# -- dry run -----------------------------------------------------------------


def test_a_dry_run_validates_without_opening_or_executing_anything(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])

    report = execute_plan(
        plan_for(path, dry_run=True), executors=None, run_id="dry", write_output=True
    )

    assert report.dry_run is True
    assert report.output_path is None
    assert outcome_of(report, "TC-001").status is TestStatus.NOT_EXECUTED
    assert outcome_of(report, "TC-001").error_code == ErrorCode.DRY_RUN.value


# -- observations ------------------------------------------------------------


def test_the_observation_comes_from_the_matching_workbook_rule(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])

    report = run(path, ScriptedExecutors(results={"source": 100, "target": 97}))
    outcome = outcome_of(report, "TC-001")

    assert outcome.observation.startswith("TC-001 mismatch: source 100 vs target 97")


def test_an_observation_rule_cannot_change_a_verdict(
    make_recon_workbook: Any, test_row: Any
) -> None:
    """Wording describes a decision Python already made."""
    misleading = [
        {
            "Enabled": "Yes",
            "Status": "FAIL",
            "Platform": "ANY",
            "Error_Code": "ANY",
            "Observation_Template": "{test_id} looks fine to me",
        }
    ]
    path = make_recon_workbook([test_row("TC-001")], observation_rules=misleading)

    report = run(path, ScriptedExecutors(results={"source": 1, "target": 2}))
    outcome = outcome_of(report, "TC-001")

    assert outcome.status is TestStatus.FAIL
    assert outcome.observation == "TC-001 looks fine to me"


# -- profiling ---------------------------------------------------------------


def test_no_comparison_records_the_value_as_profiled(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([target_only_row(test_row, Comparison_Type="NO_COMPARISON")])

    report = run(path, ScriptedExecutors(results={"target": 4321}))

    outcome = outcome_of(report, "TC-TGT")
    assert outcome.status is TestStatus.PROFILED
    assert outcome.actual_value == 4321
    assert report.passed == 0


def test_percentage_variance_is_recorded_for_a_tolerance_test(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook(
        [
            test_row(
                "TC-001",
                Comparison_Type="EQUAL_PCT_TOLERANCE",
                Result_Type="NUMBER",
                Percentage_Tolerance="1",
            )
        ]
    )

    outcome = outcome_of(
        run(path, ScriptedExecutors(results={"source": "1000", "target": "1005"})), "TC-001"
    )

    assert outcome.status is TestStatus.PASS
    assert outcome.variance == Decimal(5)
    assert outcome.variance_percentage == Decimal("0.5")


def test_a_run_without_connections_is_refused_rather_than_silently_empty(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])

    with pytest.raises(Exception, match="No connections"):
        run(path, None)


def test_only_the_sections_the_plan_needs_are_ever_opened(
    make_recon_workbook: Any, test_row: Any
) -> None:
    """What the CLI does: build connections from the plan, not from the file."""
    path = make_recon_workbook([target_only_row(test_row), test_row("TC-OFF", Enabled="No")])
    plan = build_plan(path)
    executors = ScriptedExecutors(results=dict.fromkeys(plan.required_sections, 0))

    execute_plan(plan, executors=executors, run_id="run-1", write_output=False)

    assert plan.required_sections == ("target",)
    assert executors.opened == ["target"]
    assert executors.sections == ("target",)


# -- pass 1: validation, and the gate it guards ------------------------------


def syntax_error(message: str) -> SqlSyntaxError:
    return SqlSyntaxError(message)


def test_every_enabled_query_is_validated_before_any_of_them_is_executed(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001"), test_row("TC-002")])
    executors = ScriptedExecutors(results={"source": 1, "target": 1})

    run(path, executors)

    # Four checks, then four executions: not one query runs while another is
    # still unvalidated.
    assert len(executors.checks) == 4
    assert len(executors.calls) == 4
    assert executors.checks[-1].timeout_seconds == executors.calls[0].timeout_seconds
    assert [check.sql for check in executors.checks] == [call.sql for call in executors.calls]


def test_the_validation_pass_uses_the_configured_timeout(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    path = make_recon_workbook(
        [test_row("TC-001")], run_control=run_control_settings(Query_Timeout_Seconds=45)
    )
    executors = ScriptedExecutors(results={"source": 1, "target": 1})

    run(path, executors)

    assert {check.timeout_seconds for check in executors.checks} == {45}


def test_sql_the_database_refuses_to_compile_is_reported_as_a_syntax_error(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(
        results={"source": 1, "target": 1},
        syntax_failures={("source", SOURCE_SQL): syntax_error("ORA-00904: invalid identifier")},
    )

    report = run(path, executors)

    outcome = outcome_of(report, "TC-001")
    assert outcome.status is TestStatus.SYNTAX_ERROR
    assert outcome.error_code == ErrorCode.SYNTAX_ERROR.value
    assert outcome.platform is Platform.SOURCE
    assert "invalid identifier" in outcome.error_detail


def test_a_syntax_error_leaves_the_result_columns_empty(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(
        results={"source": 1, "target": 1},
        syntax_failures={
            ("target", TARGET_SQL): syntax_error("Invalid object name 'dbo.Paymnets'")
        },
    )

    outcome = outcome_of(run(path, executors), "TC-001")

    assert outcome.source_result is None
    assert outcome.target_result is None
    assert outcome.actual_value is None
    assert outcome.variance is None


def test_one_broken_query_stops_every_other_test_from_executing(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001"), test_row("TC-002"), test_row("TC-003")])
    executors = ScriptedExecutors(
        results={"source": 1, "target": 1},
        syntax_failures={("source", SOURCE_SQL): syntax_error("ORA-00942: table does not exist")},
    )

    report = run(path, executors)

    assert not executors.executed_anything()
    assert report.stopped_by_validation
    # Every row was validated; the broken SQL is shared, so all three are named.
    assert report.syntax_errors == 3


def test_the_rows_that_validated_are_recorded_as_not_executed(
    make_recon_workbook: Any, test_row: Any
) -> None:
    broken = test_row("TC-BAD", Source_SQL="SELECT COUNT(*) FROM Paymnets")
    path = make_recon_workbook([test_row("TC-OK"), broken])
    executors = ScriptedExecutors(
        results={"source": 1, "target": 1},
        syntax_failures={
            ("source", "SELECT COUNT(*) FROM Paymnets"): syntax_error("Invalid object name")
        },
    )

    report = run(path, executors)

    assert outcome_of(report, "TC-BAD").status is TestStatus.SYNTAX_ERROR
    good = outcome_of(report, "TC-OK")
    assert good.status is TestStatus.NOT_EXECUTED
    assert good.error_code == ErrorCode.RUN_STOPPED.value
    assert "pre-execution SQL check" in good.error_detail
    assert "TC-BAD" in good.error_detail
    assert not executors.executed_anything()


def test_a_validation_failure_is_never_an_overall_pass(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(
        results={"source": 1, "target": 1},
        syntax_failures={("source", SOURCE_SQL): syntax_error("ORA-00933")},
    )

    report = run(path, executors)

    assert report.overall_status == "ERROR"
    assert not report.is_clean
    assert report.blocked_error == 1


def test_the_second_side_is_not_checked_once_the_first_has_failed(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(
        results={"source": 1, "target": 1},
        syntax_failures={("source", SOURCE_SQL): syntax_error("ORA-00904")},
    )

    run(path, executors)

    assert executors.checked_sql_for("source") == [SOURCE_SQL]
    assert executors.checked_sql_for("target") == []


def test_a_clean_validation_pass_runs_everything(make_recon_workbook: Any, test_row: Any) -> None:
    path = make_recon_workbook([test_row("TC-001"), test_row("TC-002")])
    executors = ScriptedExecutors(results={"source": 7, "target": 7})

    report = run(path, executors)

    assert not report.stopped_by_validation
    assert report.passed == 2
    assert len(executors.calls) == 4


def test_a_dead_connection_blocks_its_own_rows_without_closing_the_gate(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-BOTH"), target_only_row(test_row)])
    executors = ScriptedExecutors(
        results={"source": 1, "target": 0},
        connect_failures={"source": "login failed"},
    )

    report = run(path, executors)

    assert outcome_of(report, "TC-BOTH").status is TestStatus.BLOCKED
    # The target-only test was validated and then executed: a source that never
    # opened says nothing about the target's SQL.
    assert outcome_of(report, "TC-TGT").status is TestStatus.PASS
    assert executors.checked_sql_for("source") == []
    assert executors.checked_sql_for("target") == [TARGET_SQL]


def test_a_check_that_cannot_be_made_warns_instead_of_condemning_the_sql(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001"), test_row("TC-002")])
    executors = ScriptedExecutors(
        results={"source": 5, "target": 5},
        syntax_failures={
            ("source", SOURCE_SQL): SyntaxCheckUnavailableError("the server refused SET NOEXEC ON")
        },
    )

    report = run(path, executors)

    assert report.passed == 2
    assert not report.stopped_by_validation
    warnings = [w for w in report.warnings if "not validated" in w]
    # One warning for the section, however many of its queries went unchecked.
    assert len(warnings) == 1
    assert "[source]" in warnings[0]


def test_a_timeout_during_validation_stops_the_run_without_claiming_a_syntax_error(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(
        results={"source": 1, "target": 1},
        syntax_failures={("source", SOURCE_SQL): QueryTimeoutError("took too long")},
    )

    report = run(path, executors)

    outcome = outcome_of(report, "TC-001")
    assert outcome.status is TestStatus.ERROR
    assert outcome.error_code == ErrorCode.QUERY_TIMEOUT.value
    assert report.stopped_by_validation
    assert not executors.executed_anything()


def test_an_unexpected_failure_during_validation_is_named_as_such(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(
        results={"source": 1, "target": 1},
        syntax_failures={("source", SOURCE_SQL): DatabaseExecutionError("driver gave up")},
    )

    outcome = outcome_of(run(path, executors), "TC-001")

    assert outcome.status is TestStatus.ERROR
    assert outcome.error_code == ErrorCode.SYNTAX_CHECK_FAILED.value


def test_a_dry_run_validates_nothing_against_a_database(
    make_recon_workbook: Any, test_row: Any
) -> None:
    path = make_recon_workbook([test_row("TC-001")])
    executors = ScriptedExecutors(results={"source": 1, "target": 1})

    report = run(path, executors, dry_run=True)

    assert executors.checks == []
    assert executors.calls == []
    assert executors.opened == []
    assert report.dry_run


def test_a_disabled_row_is_never_validated(make_recon_workbook: Any, test_row: Any) -> None:
    path = make_recon_workbook([test_row("TC-ON"), test_row("TC-OFF", Enabled="No")])
    executors = ScriptedExecutors(results={"source": 1, "target": 1})

    run(path, executors)

    assert all("TC-OFF" not in check.sql for check in executors.checks)
    assert len(executors.checks) == 2
