"""Executing a plan: query, normalize, compare, decide, describe.

The order is fixed and the boundaries are deliberate. Queries produce raw
scalars; normalization turns them into the declared type; a registered Python
function compares them; the status and error code come out of that comparison;
and only then is an observation worded. Nothing later in that chain can change
anything earlier in it — in particular, no observation rule can turn a FAIL
into a PASS.

Failure isolation is per test. A source that will not connect blocks the tests
that need it and leaves the rest alone. One query that times out is one
``ERROR`` row. ``Continue_On_Test_Error = No`` is the only thing that stops a
run early, and what it stops is recorded as ``NOT EXECUTED`` rather than
quietly omitted, because a run with unexecuted tests is never a pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from ..database.base import QueryExecutor
from ..database.failures import describe_object_access_failure
from ..errors import (
    ComparisonError,
    ConnectionFailedError,
    NonScalarResultError,
    QueryTimeoutError,
    ReconciliationError,
    TypeConversionError,
    WorkbookError,
)
from ..evaluation.comparisons import ComparisonRequest, compare
from ..evaluation.normalization import NormalizedValue, display, normalize
from ..models import (
    ErrorCode,
    ExecutionScope,
    Platform,
    ScalarValue,
    TestStatus,
)
from ..security.redaction import sanitize_error, sanitize_text
from ..workbook.observations import ObservationRules, render_template
from ..workbook.output import ResultWriter
from ..workbook.run_control import RunControl
from ..workbook.testcases import DefinitionProblem, TestDefinition
from .plan import ExecutionPlan

__all__ = [
    "EXECUTOR_VERSION",
    "Executors",
    "RunReport",
    "TestOutcome",
    "execute_plan",
    "new_run_id",
]

#: Recorded in ``Run History`` so a result can be traced to the code that made it.
EXECUTOR_VERSION = "2.0"

_PLATFORM_OF_SIDE: Mapping[str, Platform] = {
    "source": Platform.SOURCE,
    "target": Platform.TARGET,
}


def new_run_id() -> str:
    """One identifier for the whole run."""
    return str(uuid.uuid4())


class Executors(Protocol):
    """What the engine needs from whatever holds the open connections."""

    @property
    def sections(self) -> tuple[str, ...]: ...

    def open_all(self) -> tuple[dict[str, Any], dict[str, str]]: ...

    def executor_for(self, section: str) -> QueryExecutor: ...

    def database_name(self, section: str) -> str: ...

    def close_all(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TestOutcome:
    """Everything one test produced, ready to be written to its row."""

    row_number: int
    test_id: str
    status: TestStatus
    platform: Platform = Platform.NONE
    error_code: str = ""
    error_detail: str = ""
    observation: str = ""
    source_result: ScalarValue = None
    target_result: ScalarValue = None
    actual_value: ScalarValue = None
    variance: Decimal | None = None
    variance_percentage: Decimal | None = None
    source_duration_ms: int | None = None
    target_duration_ms: int | None = None
    executed_at: datetime | None = None
    run_id: str = ""

    def output_values(self) -> dict[str, Any]:
        """The row's execution-output columns, keyed by header name."""
        return {
            "Source_Result": self.source_result,
            "Target_Result": self.target_result,
            "Actual_Value": self.actual_value,
            "Variance": self.variance,
            "Variance_Percentage": self.variance_percentage,
            "Status": self.status.value,
            "Observation": self.observation,
            "Source_Duration_ms": self.source_duration_ms,
            "Target_Duration_ms": self.target_duration_ms,
            "Executed_At_UTC": self.executed_at,
            "Run_ID": self.run_id,
            "Error_Code": self.error_code,
            "Error_Detail": self.error_detail,
            "Evidence_Path": None,
            "Platform": self.platform.value,
        }


@dataclass(frozen=True, slots=True)
class RunReport:
    """The whole run, summarised. Carries no credential and no row data."""

    run_id: str
    started_at: datetime
    completed_at: datetime
    workbook_path: Path
    outcomes: tuple[TestOutcome, ...]
    template_version: str = ""
    executor_version: str = EXECUTOR_VERSION
    output_path: Path | None = None
    dry_run: bool = False
    warnings: tuple[str, ...] = ()
    connections: tuple[str, ...] = ()

    def _count(self, *statuses: TestStatus) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status in statuses)

    @property
    def enabled_tests(self) -> int:
        """Executable tests. Disabled rows are never part of a total."""
        return sum(1 for o in self.outcomes if o.status is not TestStatus.DISABLED)

    @property
    def passed(self) -> int:
        return self._count(TestStatus.PASS)

    @property
    def failed(self) -> int:
        return self._count(TestStatus.FAIL)

    @property
    def profiled(self) -> int:
        return self._count(TestStatus.PROFILED)

    @property
    def blocked_error(self) -> int:
        return self._count(TestStatus.ERROR, TestStatus.BLOCKED, TestStatus.CONFIG_ERROR)

    @property
    def not_executed(self) -> int:
        return self._count(TestStatus.NOT_EXECUTED)

    @property
    def disabled(self) -> int:
        return self._count(TestStatus.DISABLED)

    @property
    def overall_status(self) -> str:
        """Never PASS while an enabled test went unexecuted."""
        if self.enabled_tests == 0:
            return "NO TESTS"
        if self.failed:
            return "FAIL"
        if self.blocked_error:
            return "ERROR"
        if self.not_executed:
            return "INCOMPLETE"
        return "PASS"

    @property
    def is_clean(self) -> bool:
        return self.overall_status == "PASS"

    def history_row(self) -> dict[str, Any]:
        return {
            "Run_ID": self.run_id,
            "Started_At_UTC": self.started_at,
            "Completed_At_UTC": self.completed_at,
            "Workbook_Name": self.workbook_path.name,
            "Template_Version": self.template_version,
            "Executor_Version": self.executor_version,
            "Enabled_Tests": self.enabled_tests,
            "Passed": self.passed,
            "Failed": self.failed,
            "Profiled": self.profiled,
            "Blocked_Error": self.blocked_error,
            "Not_Executed": self.not_executed,
            "Overall_Status": self.overall_status,
            "Output_File": self.output_path.name if self.output_path else "",
        }


@dataclass
class _Outcomes:
    """Accumulates outcomes in row order, whatever order they were produced in."""

    items: list[TestOutcome] = field(default_factory=list)

    def add(self, outcome: TestOutcome) -> None:
        self.items.append(outcome)

    def sorted(self) -> tuple[TestOutcome, ...]:
        return tuple(sorted(self.items, key=lambda o: o.row_number))


def execute_plan(
    plan: ExecutionPlan,
    *,
    executors: Executors | None = None,
    run_id: str | None = None,
    started_at: datetime | None = None,
    output_dir: Path | None = None,
    write_output: bool = True,
) -> RunReport:
    """Run a plan and return what happened. Always closes every connection."""
    control = plan.control
    identifier = run_id or new_run_id()
    started = started_at or datetime.now(UTC).replace(microsecond=0)
    outcomes = _Outcomes()
    warnings = list(plan.warnings)

    for problem in plan.problems:
        outcomes.add(_problem_outcome(problem, plan.rules, control, identifier, started))
    for definition in plan.disabled:
        outcomes.add(_disabled_outcome(definition, plan.rules, control, identifier))

    stop_before_databases = bool(plan.problems) and control.stop_on_critical_config_error
    if stop_before_databases:
        warnings.append(
            f"{len(plan.problems)} configuration error(s) and "
            f"Stop_On_Critical_Config_Error = Yes, so no database was opened."
        )

    connections: tuple[str, ...] = ()
    try:
        if control.dry_run:
            reason = "Dry run: the test was validated but no SQL was executed."
            for definition in plan.executable:
                outcomes.add(
                    _not_executed(
                        definition, reason, plan.rules, control, identifier, ErrorCode.DRY_RUN
                    )
                )
        elif stop_before_databases:
            reason = "Stopped before any database was opened by a critical configuration error."
            for definition in plan.executable:
                outcomes.add(_not_executed(definition, reason, plan.rules, control, identifier))
        elif executors is None:
            raise ReconciliationError(
                "No connections were provided for a run that is not a dry run."
            )
        else:
            connections = executors.sections
            _execute_tests(plan, executors, outcomes, control, identifier, started)
    finally:
        if executors is not None:
            executors.close_all()

    report = RunReport(
        run_id=identifier,
        started_at=started,
        completed_at=datetime.now(UTC).replace(microsecond=0),
        workbook_path=plan.workbook_path,
        outcomes=outcomes.sorted(),
        template_version=control.template_version,
        dry_run=control.dry_run,
        warnings=tuple(warnings),
        connections=connections,
    )

    if control.dry_run or not write_output:
        # A dry run validates; it never touches the workbook, and in particular
        # never clears the results of the last real run.
        return report
    return _write_results(plan, report, output_dir=output_dir)


# -- execution ---------------------------------------------------------------


def _execute_tests(
    plan: ExecutionPlan,
    executors: Executors,
    outcomes: _Outcomes,
    control: RunControl,
    run_id: str,
    started: datetime,
) -> None:
    _identities, failures = executors.open_all()
    stopped_reason = ""

    for definition in plan.executable:
        if stopped_reason:
            outcomes.add(_not_executed(definition, stopped_reason, plan.rules, control, run_id))
            continue

        blocked_section = next(
            (section for section in definition.sections() if section in failures), None
        )
        if blocked_section is not None:
            outcomes.add(
                _blocked(
                    definition,
                    blocked_section,
                    failures[blocked_section],
                    plan.rules,
                    control,
                    run_id,
                )
            )
            continue

        outcome = _execute_one(definition, executors, plan.rules, control, run_id)
        outcomes.add(outcome)
        if not control.continue_on_test_error and outcome.status in {
            TestStatus.ERROR,
            TestStatus.BLOCKED,
        }:
            stopped_reason = (
                f"Stopped after {definition.test_id} errored, because Continue_On_Test_Error = No."
            )


def _execute_one(
    definition: TestDefinition,
    executors: Executors,
    rules: ObservationRules,
    control: RunControl,
    run_id: str,
) -> TestOutcome:
    executed_at = datetime.now(UTC).replace(microsecond=0)
    raw: dict[str, ScalarValue] = {}
    durations: dict[str, int] = {}
    databases: dict[str, str] = {}

    for side in _sides_of(definition.scope):
        section = definition.source_section if side == "source" else definition.target_section
        sql = definition.source_sql if side == "source" else definition.target_sql
        databases[side] = executors.database_name(section)
        started = perf_counter()
        try:
            value = executors.executor_for(section).execute_scalar(
                sql, control.query_timeout_seconds
            )
        except Exception as exc:
            durations[side] = _elapsed_ms(started)
            return _query_failure(
                definition,
                side,
                exc,
                durations,
                raw,
                rules,
                control,
                run_id,
                executed_at,
                databases,
            )
        durations[side] = _elapsed_ms(started)
        raw[side] = value

    normalized: dict[str, NormalizedValue] = {}
    for side, value in raw.items():
        try:
            normalized[side] = normalize(value, definition.result_type, label=f"The {side} result")
        except TypeConversionError as exc:
            code = (
                ErrorCode.NULL_RESULT
                if value is None or (isinstance(value, str) and not value.strip())
                else ErrorCode.TYPE_CONVERSION_ERROR
            )
            return _outcome(
                definition,
                TestStatus.ERROR,
                _PLATFORM_OF_SIDE[side],
                code,
                str(exc),
                rules,
                control,
                run_id,
                executed_at=executed_at,
                raw=raw,
                durations=durations,
                databases=databases,
            )

    request = ComparisonRequest(
        comparison=definition.comparison,
        scope=definition.scope,
        result_type=definition.result_type,
        source=normalized.get("source"),
        target=normalized.get("target"),
        expected=definition.expected_value,
        absolute_tolerance=definition.absolute_tolerance,
        percentage_tolerance=definition.percentage_tolerance,
    )
    try:
        result = compare(request)
    except ComparisonError as exc:
        return _outcome(
            definition,
            TestStatus.ERROR,
            Platform.COMPARISON,
            ErrorCode.TYPE_CONVERSION_ERROR,
            str(exc),
            rules,
            control,
            run_id,
            executed_at=executed_at,
            raw=raw,
            durations=durations,
            databases=databases,
        )

    return _outcome(
        definition,
        result.status,
        Platform.COMPARISON if result.status is TestStatus.FAIL else Platform.NONE,
        result.error_code,
        result.detail,
        rules,
        control,
        run_id,
        executed_at=executed_at,
        raw=raw,
        normalized=normalized,
        durations=durations,
        databases=databases,
        variance=result.variance,
        variance_percentage=result.variance_percentage,
        actual=request.actual,
    )


def _query_failure(
    definition: TestDefinition,
    side: str,
    exc: BaseException,
    durations: dict[str, int],
    raw: dict[str, ScalarValue],
    rules: ObservationRules,
    control: RunControl,
    run_id: str,
    executed_at: datetime,
    databases: dict[str, str],
) -> TestOutcome:
    """Classify a driver failure into one of the stable query error codes."""
    if isinstance(exc, QueryTimeoutError):
        code = ErrorCode.QUERY_TIMEOUT
    elif isinstance(exc, NonScalarResultError):
        code = ErrorCode.NON_SCALAR_RESULT
    elif isinstance(exc, ConnectionFailedError):
        code = ErrorCode.CONNECTION_FAILED
    elif isinstance(exc, ReconciliationError):
        code = ErrorCode.QUERY_EXECUTION_FAILED
    else:
        code = ErrorCode.UNEXPECTED_ERROR

    detail = sanitize_error(exc, max_length=control.error_detail_max_length)
    advice = describe_object_access_failure(detail)
    if advice:
        detail = f"{detail} {advice}"
    status = TestStatus.BLOCKED if code is ErrorCode.CONNECTION_FAILED else TestStatus.ERROR
    return _outcome(
        definition,
        status,
        _PLATFORM_OF_SIDE[side],
        code,
        detail,
        rules,
        control,
        run_id,
        executed_at=executed_at,
        raw=raw,
        durations=durations,
        databases=databases,
    )


def _sides_of(scope: ExecutionScope) -> tuple[str, ...]:
    """Exactly the sides this scope executes, in the order they are run."""
    sides: list[str] = []
    if scope.uses_source:
        sides.append("source")
    if scope.uses_target:
        sides.append("target")
    return tuple(sides)


# -- outcome construction ----------------------------------------------------


def _outcome(
    definition: TestDefinition,
    status: TestStatus,
    platform: Platform,
    code: ErrorCode | None,
    detail: str,
    rules: ObservationRules,
    control: RunControl,
    run_id: str,
    *,
    executed_at: datetime | None = None,
    raw: Mapping[str, ScalarValue] | None = None,
    normalized: Mapping[str, NormalizedValue] | None = None,
    durations: Mapping[str, int] | None = None,
    databases: Mapping[str, str] | None = None,
    variance: Decimal | None = None,
    variance_percentage: Decimal | None = None,
    actual: NormalizedValue | None = None,
) -> TestOutcome:
    raw = raw or {}
    normalized = normalized or {}
    durations = durations or {}
    error_code = code.value if code is not None else ""
    detail_text = sanitize_text(detail, max_length=control.error_detail_max_length)

    source_result = normalized.get("source", raw.get("source"))
    target_result = normalized.get("target", raw.get("target"))
    if actual is None:
        actual = target_result if definition.scope.uses_target else source_result

    values = {
        "test_id": definition.test_id,
        "test_name": definition.name,
        "domain": definition.domain,
        "status": status.value,
        "platform": platform.value,
        "error_code": error_code,
        "error_detail": detail_text,
        "source_result": display(source_result),
        "target_result": display(target_result),
        "actual_value": display(actual),
        "expected_value": display(definition.expected_value),
        "variance": display(variance),
        "variance_percentage": display(variance_percentage),
        "absolute_tolerance": display(definition.absolute_tolerance),
        "percentage_tolerance": display(definition.percentage_tolerance),
        "timeout_seconds": control.query_timeout_seconds,
        "execution_scope": definition.scope.value,
        "database": ", ".join(sorted({name for name in (databases or {}).values() if name})),
    }
    template = rules.template_for(status, platform, error_code or None)
    observation = render_template(template, values, max_length=control.observation_max_length)

    return TestOutcome(
        row_number=definition.row_number,
        test_id=definition.test_id,
        status=status,
        platform=platform,
        error_code=error_code,
        error_detail=detail_text,
        observation=observation,
        source_result=source_result,
        target_result=target_result,
        actual_value=actual,
        variance=variance,
        variance_percentage=variance_percentage,
        source_duration_ms=durations.get("source"),
        target_duration_ms=durations.get("target"),
        executed_at=executed_at,
        run_id=run_id,
    )


def _disabled_outcome(
    definition: TestDefinition, rules: ObservationRules, control: RunControl, run_id: str
) -> TestOutcome:
    """A disabled row is recorded and never executed, connected or counted."""
    return _outcome(
        definition,
        TestStatus.DISABLED,
        Platform.WORKBOOK,
        None,
        "Enabled = No.",
        rules,
        control,
        run_id,
    )


def _not_executed(
    definition: TestDefinition,
    reason: str,
    rules: ObservationRules,
    control: RunControl,
    run_id: str,
    code: ErrorCode = ErrorCode.RUN_STOPPED,
) -> TestOutcome:
    return _outcome(
        definition,
        TestStatus.NOT_EXECUTED,
        Platform.EXECUTOR,
        code,
        reason,
        rules,
        control,
        run_id,
    )


def _blocked(
    definition: TestDefinition,
    section: str,
    reason: str,
    rules: ObservationRules,
    control: RunControl,
    run_id: str,
) -> TestOutcome:
    return _outcome(
        definition,
        TestStatus.BLOCKED,
        _PLATFORM_OF_SIDE.get(section, Platform.EXECUTOR),
        ErrorCode.CONNECTION_FAILED,
        f"The [{section}] connection could not be opened. {reason}",
        rules,
        control,
        run_id,
    )


def _problem_outcome(
    problem: DefinitionProblem,
    rules: ObservationRules,
    control: RunControl,
    run_id: str,
    executed_at: datetime,
) -> TestOutcome:
    """A misconfigured row, reported against itself and nothing else."""
    detail = sanitize_text(problem.message, max_length=control.error_detail_max_length)
    values = {
        "test_id": problem.test_id or f"row {problem.row_number}",
        "status": problem.status.value,
        "platform": problem.platform.value,
        "error_code": problem.code.value,
        "error_detail": detail,
    }
    template = rules.template_for(problem.status, problem.platform, problem.code.value)
    return TestOutcome(
        row_number=problem.row_number,
        test_id=problem.test_id,
        status=problem.status,
        platform=problem.platform,
        error_code=problem.code.value,
        error_detail=detail,
        observation=render_template(template, values, max_length=control.observation_max_length),
        executed_at=executed_at,
        run_id=run_id,
    )


# -- output ------------------------------------------------------------------


def _write_results(plan: ExecutionPlan, report: RunReport, *, output_dir: Path | None) -> RunReport:
    """Update output columns, append history, and save atomically."""
    from ..workbook.output import resolve_directory_env

    control = plan.control
    writer = ResultWriter(
        plan.workbook_path,
        sheet_name=plan.sheet.sheet_name,
        header_row=plan.sheet.header_row,
    )
    try:
        directory = output_dir or resolve_directory_env(control.output_directory_env)
        destination = writer.plan_destination(
            mode=control.output_mode,
            timestamp=report.started_at,
            run_id=report.run_id,
            output_dir=directory,
        )
        writer.clear_outputs(
            definition.row_number for definition in (*plan.executable, *plan.disabled)
        )
        for outcome in report.outcomes:
            writer.write_row(outcome.row_number, outcome.output_values())
        final = RunReport(
            run_id=report.run_id,
            started_at=report.started_at,
            completed_at=report.completed_at,
            workbook_path=report.workbook_path,
            outcomes=report.outcomes,
            template_version=report.template_version,
            output_path=destination,
            dry_run=report.dry_run,
            warnings=report.warnings,
            connections=report.connections,
        )
        writer.append_run_history(final.history_row())
        writer.save(destination)
    except WorkbookError as exc:
        raise WorkbookError(f"{exc} [{ErrorCode.OUTPUT_WRITE_FAILED.value}]") from None
    finally:
        writer.close()

    return final


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))
