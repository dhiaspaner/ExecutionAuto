"""Executing a plan: validate everything, then query, normalize, compare, decide.

A run makes two passes over the enabled tests, and the first one executes
nothing at all. Pass 1 asks each database to *compile* every query it is about
to be given — ``SET NOEXEC ON`` on SQL Server, a parse-only call on Oracle — and
writes the workbook with what it found. If anything failed to compile the run
stops there: no reconciliation SQL is executed, and no result column is filled
in for any row. Only a completely clean validation pass reaches pass 2, which is
the execute-compare-decide pipeline described below.

The gate exists because a half-executed reconciliation is the expensive kind of
wrong. Finding a typo in the last of two hundred rows after the first hundred
and ninety-nine have already run against production leaves a workbook that is
part results, part damage report, and no way to tell at a glance which half is
which.

Within pass 2 the order is fixed and the boundaries are deliberate. Queries produce raw
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
from collections.abc import Callable, Mapping, Sequence
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
    SqlSyntaxError,
    SyntaxCheckUnavailableError,
    TypeConversionError,
    WorkbookError,
)
from ..evaluation.comparisons import ComparisonRequest, compare
from ..evaluation.normalization import NormalizedValue, display, normalize
from ..models import (
    ErrorCode,
    ExecutionScope,
    Platform,
    RunMode,
    ScalarValue,
    TestStatus,
)
from ..security.redaction import sanitize_error, sanitize_text
from ..workbook.columns import VALIDATION_ERRORS_SHEET
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
EXECUTOR_VERSION = "2.1"

#: How many failing test ids a gate message names before it stops listing them.
_NAMED_IN_GATE_MESSAGE = 5

_PLATFORM_OF_SIDE: Mapping[str, Platform] = {
    "source": Platform.SOURCE,
    "target": Platform.TARGET,
}


#: Called with one already-sanitized progress line. The engine never formats a
#: credential, a connection string or any SQL into these, so whatever a caller
#: does with them — console, file, both — is safe.
ProgressLog = Callable[[str], None]


#: How much of a sanitized error to show on one progress line. The whole of it
#: is written to the ``Validation Errors`` sheet, so the console stays scannable
#: at two hundred rows instead of scrolling one failure off the top.
_PROGRESS_DETAIL_CHARS = 110


def _short(detail: str) -> str:
    """One line's worth of an already-sanitized message."""
    collapsed = " ".join(detail.split())
    if len(collapsed) <= _PROGRESS_DETAIL_CHARS:
        return collapsed
    return f"{collapsed[:_PROGRESS_DETAIL_CHARS].rstrip()}..."


def _progress(log: ProgressLog | None) -> ProgressLog:
    """A reporter that does nothing when the caller asked for no logging."""
    if log is None:
        return lambda _message: None
    return log


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
    #: True when pass 1 found SQL that would not compile, so pass 2 never ran
    #: and no reconciliation query was executed.
    stopped_by_validation: bool = False
    warnings: tuple[str, ...] = ()
    connections: tuple[str, ...] = ()
    #: One ``Syntax Validation`` row per test the pre-execution check looked at.
    validation_rows: tuple[dict[str, Any], ...] = ()

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
    def syntax_errors(self) -> int:
        """Tests whose SQL the database refused to compile, before any ran."""
        return self._count(TestStatus.SYNTAX_ERROR)

    @property
    def blocked_error(self) -> int:
        return self._count(
            TestStatus.ERROR,
            TestStatus.SYNTAX_ERROR,
            TestStatus.BLOCKED,
            TestStatus.CONFIG_ERROR,
        )

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

    def validation_failures(self) -> tuple[TestOutcome, ...]:
        """The outcomes pass 1 produced: queries the database would not compile.

        Identified by error code rather than by status, because a rejected
        query and a check that could not be performed are different statuses
        but the same problem for whoever has to fix the sheet.
        """
        codes = {
            ErrorCode.SYNTAX_ERROR.value,
            ErrorCode.SYNTAX_CHECK_FAILED.value,
        }
        return tuple(outcome for outcome in self.outcomes if outcome.error_code in codes)

    def validation_error_rows(self) -> tuple[dict[str, Any], ...]:
        """One ``Validation Errors`` row per query that failed pass 1."""
        return tuple(
            {
                "Run_ID": self.run_id,
                "Checked_At_UTC": outcome.executed_at,
                "Test_ID": outcome.test_id,
                "Row": outcome.row_number,
                "Status": outcome.status.value,
                "Platform": outcome.platform.value,
                "Error_Code": outcome.error_code,
                "Error_Detail": outcome.error_detail,
            }
            for outcome in self.validation_failures()
        )

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
    on_progress: ProgressLog | None = None,
    mode: RunMode = RunMode.EXECUTE,
) -> RunReport:
    """Run a plan and return what happened. Always closes every connection.

    ``on_progress`` receives one sanitized line per step, so a caller can show
    the pre-execution check happening and see exactly where a run stopped.
    """
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
    gate_closed = False
    validation_rows: tuple[dict[str, Any], ...] = ()
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
            passes = _execute_tests(
                plan, executors, outcomes, control, identifier, _progress(on_progress), mode
            )
            warnings.extend(passes.warnings)
            gate_closed = passes.gate_closed
            validation_rows = passes.validation_rows
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
        stopped_by_validation=gate_closed,
        warnings=tuple(warnings),
        connections=connections,
        validation_rows=validation_rows,
    )

    if control.dry_run or not write_output:
        # A dry run validates; it never touches the workbook, and in particular
        # never clears the results of the last real run.
        return report
    return _write_results(plan, report, output_dir=output_dir)


# -- execution ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Passes:
    """What the two passes had to say to the run as a whole."""

    warnings: tuple[str, ...] = ()
    gate_closed: bool = False
    validation_rows: tuple[dict[str, Any], ...] = ()


def _execute_tests(
    plan: ExecutionPlan,
    executors: Executors,
    outcomes: _Outcomes,
    control: RunControl,
    run_id: str,
    log: ProgressLog,
    mode: RunMode = RunMode.EXECUTE,
) -> _Passes:
    """Validate every enabled test, then execute them only if all of them passed."""
    _identities, failures = executors.open_all()

    runnable: list[TestDefinition] = []
    for definition in plan.executable:
        blocked_section = next(
            (section for section in definition.sections() if section in failures), None
        )
        if blocked_section is None:
            runnable.append(definition)
            continue
        # A connection that never opened is not evidence about anyone's SQL, so
        # these rows are set aside rather than counted against the gate: a dead
        # source must not stop the target-only tests from running.
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

    validation = _validate_tests(runnable, executors, plan.rules, control, run_id, log)
    for outcome in validation.failures:
        outcomes.add(outcome)

    if validation.failures:
        reason = _gate_reason(validation.failures)
        log("")
        log(
            f"VALIDATION FAILED: {len(validation.failures)} of {len(runnable)} "
            f"queries were rejected by the database."
        )
        for outcome in validation.failures:
            log(
                f"    row {outcome.row_number}  {outcome.test_id or '(no id)'}  "
                f"[{outcome.error_code}] {_short(outcome.error_detail)}"
            )
        log("")
        log(
            f"  Nothing was executed. {len(validation.passed)} query(s) that did compile "
            f"were left unrun."
        )
        log(
            f"  The '{VALIDATION_ERRORS_SHEET}' sheet of the result workbook lists every one, "
            f"with the database's full message."
        )
        for definition in validation.passed:
            outcomes.add(_not_executed(definition, reason, plan.rules, control, run_id))
        return _Passes(
            warnings=(*validation.warnings, reason),
            gate_closed=True,
            validation_rows=validation.rows,
        )

    log(f"  All {len(validation.passed)} queries compiled. Nothing was rejected.")
    if mode is RunMode.VALIDATE:
        log("")
        log(
            f"Validation run: stopping here by request. "
            f"{len(validation.passed)} query(s) compiled and none were executed."
        )
        for definition in validation.passed:
            outcomes.add(_validated_outcome(definition, plan.rules, control, run_id))
        return _Passes(warnings=validation.warnings, validation_rows=validation.rows)

    _run_tests(validation.passed, executors, outcomes, plan, control, run_id, log)
    return _Passes(warnings=validation.warnings, validation_rows=validation.rows)


# -- pass 1: validation ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Validation:
    """The result of pass 1: who may run, who may not, and what to warn about."""

    passed: tuple[TestDefinition, ...] = ()
    failures: tuple[TestOutcome, ...] = ()
    warnings: tuple[str, ...] = ()
    #: One ``Syntax Validation`` row per test checked, in row order.
    rows: tuple[dict[str, Any], ...] = ()


def _validate_tests(
    definitions: Sequence[TestDefinition],
    executors: Executors,
    rules: ObservationRules,
    control: RunControl,
    run_id: str,
    log: ProgressLog,
) -> _Validation:
    """Ask the databases to compile every query, and execute none of them.

    Every test is checked even once one has failed. The point of this pass is a
    workbook that names *all* the broken rows at once, so they can be fixed in
    one sitting rather than one run each.
    """
    checked_at = datetime.now(UTC).replace(microsecond=0)
    passed: list[TestDefinition] = []
    failures: list[TestOutcome] = []
    warnings: list[str] = []
    unavailable: set[str] = set()

    total = len(definitions)
    rows: list[dict[str, Any]] = []
    log("")
    log(f"Validation phase: compiling {total} query(s). No SQL is executed in this pass.")
    for index, definition in enumerate(definitions, start=1):
        unchecked: set[str] = set()
        failure = _validate_one(
            definition,
            executors,
            rules,
            control,
            run_id,
            checked_at,
            unavailable,
            warnings,
            unchecked,
        )
        if failure is None:
            passed.append(definition)
            # "Not checked" is not "fine": the database could not be asked, so
            # the row says so rather than implying the SQL was proven sound.
            result = "NOT CHECKED" if unchecked else "OK"
            log(f"  [{index:>4}/{total}] {definition.test_id}  {result.lower()}")
            rows.append(
                _validation_row(definition, run_id, checked_at, result, Platform.NONE, "", "")
            )
        else:
            failures.append(failure)
            log(
                f"  [{index:>4}/{total}] {definition.test_id}  "
                f"{failure.status.value}  [{failure.error_code}] {_short(failure.error_detail)}"
            )
            rows.append(
                _validation_row(
                    definition,
                    run_id,
                    checked_at,
                    failure.status.value,
                    failure.platform,
                    failure.error_code,
                    failure.error_detail,
                )
            )

    return _Validation(tuple(passed), tuple(failures), tuple(warnings), tuple(rows))


def _validate_one(
    definition: TestDefinition,
    executors: Executors,
    rules: ObservationRules,
    control: RunControl,
    run_id: str,
    checked_at: datetime,
    unavailable: set[str],
    warnings: list[str],
    unchecked: set[str],
) -> TestOutcome | None:
    """Check one test's queries. ``None`` means every side compiled.

    The first side that fails decides the row: one row carries one status, and
    a test with a broken source query is broken whatever its target query says.
    """
    for side in _sides_of(definition.scope):
        section = definition.source_section if side == "source" else definition.target_section
        sql = definition.source_sql if side == "source" else definition.target_sql
        try:
            executors.executor_for(section).validate_syntax(sql, control.query_timeout_seconds)
        except SyntaxCheckUnavailableError as exc:
            # Nothing was proven about this SQL either way, so it is not a
            # failure — but the run says out loud that it is going in unchecked.
            unchecked.add(definition.test_id)
            if section not in unavailable:
                unavailable.add(section)
                warnings.append(
                    f"[{section}] could not be asked to check SQL before running it, so its "
                    f"queries were not validated: "
                    f"{sanitize_error(exc, max_length=control.error_detail_max_length)}"
                )
            continue
        except Exception as exc:
            return _validation_failure(
                definition, side, exc, rules, control, run_id, checked_at, executors
            )
    return None


def _validation_failure(
    definition: TestDefinition,
    side: str,
    exc: BaseException,
    rules: ObservationRules,
    control: RunControl,
    run_id: str,
    checked_at: datetime,
    executors: Executors,
) -> TestOutcome:
    """Turn a failed check into a row, without ever claiming a result."""
    if isinstance(exc, SqlSyntaxError):
        status, code = TestStatus.SYNTAX_ERROR, ErrorCode.SYNTAX_ERROR
    elif isinstance(exc, QueryTimeoutError):
        status, code = TestStatus.ERROR, ErrorCode.QUERY_TIMEOUT
    elif isinstance(exc, ConnectionFailedError):
        status, code = TestStatus.BLOCKED, ErrorCode.CONNECTION_FAILED
    else:
        status, code = TestStatus.ERROR, ErrorCode.SYNTAX_CHECK_FAILED

    detail = sanitize_error(exc, max_length=control.error_detail_max_length)
    advice = describe_object_access_failure(detail)
    if advice:
        detail = f"{detail} {advice}"

    section = definition.source_section if side == "source" else definition.target_section
    return _outcome(
        definition,
        status,
        _PLATFORM_OF_SIDE[side],
        code,
        detail,
        rules,
        control,
        run_id,
        executed_at=checked_at,
        databases={side: executors.database_name(section)},
    )


def _validation_row(
    definition: TestDefinition,
    run_id: str,
    checked_at: datetime,
    result: str,
    platform: Platform,
    error_code: str,
    detail: str,
) -> dict[str, Any]:
    """One ``Syntax Validation`` row. Carries no SQL and no row data."""
    return {
        "Run_ID": run_id,
        "Checked_At_UTC": checked_at,
        "Test_ID": definition.test_id,
        "Row": definition.row_number,
        "Result": result,
        "Platform": platform.value,
        "Error_Code": error_code,
        "Error_Detail": detail,
    }


def _gate_reason(failures: Sequence[TestOutcome]) -> str:
    """Why nothing ran, naming the rows that have to be fixed first."""
    named = ", ".join(
        outcome.test_id or f"row {outcome.row_number}"
        for outcome in failures[:_NAMED_IN_GATE_MESSAGE]
    )
    remainder = len(failures) - _NAMED_IN_GATE_MESSAGE
    more = f" and {remainder} more" if remainder > 0 else ""
    return (
        f"Nothing was executed: {len(failures)} test(s) failed the pre-execution SQL check "
        f"({named}{more}). Fix that SQL and run again."
    )


# -- pass 2: execution -------------------------------------------------------


def _run_tests(
    definitions: Sequence[TestDefinition],
    executors: Executors,
    outcomes: _Outcomes,
    plan: ExecutionPlan,
    control: RunControl,
    run_id: str,
    log: ProgressLog,
) -> None:
    """Execute the tests pass 1 cleared, in row order."""
    stopped_reason = ""
    total = len(definitions)
    log("")
    log(f"Execution phase: running {total} test(s), one at a time.")
    for index, definition in enumerate(definitions, start=1):
        if stopped_reason:
            outcomes.add(_not_executed(definition, stopped_reason, plan.rules, control, run_id))
            continue

        outcome = _execute_one(definition, executors, plan.rules, control, run_id)
        outcomes.add(outcome)
        detail = (
            f"  [{outcome.error_code}] {_short(outcome.error_detail)}" if outcome.error_code else ""
        )
        log(f"  [{index:>4}/{total}] {definition.test_id}  {outcome.status.value}{detail}")
        if not control.continue_on_test_error and outcome.status in {
            TestStatus.ERROR,
            TestStatus.BLOCKED,
        }:
            stopped_reason = (
                f"Stopped after {definition.test_id} errored, because Continue_On_Test_Error = No."
            )
            log(f"  {stopped_reason}")


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


def _validated_outcome(
    definition: TestDefinition, rules: ObservationRules, control: RunControl, run_id: str
) -> TestOutcome:
    """A query that compiled in a validate-only run: an outcome, not a gap."""
    return _outcome(
        definition,
        TestStatus.VALIDATED,
        Platform.NONE,
        None,
        "Query compiled. Not executed: this was a validation run.",
        rules,
        control,
        run_id,
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
            stopped_by_validation=report.stopped_by_validation,
            warnings=report.warnings,
            connections=report.connections,
            validation_rows=report.validation_rows,
        )
        writer.write_validation_report(final.validation_rows)
        writer.write_validation_errors(final.validation_error_rows())
        writer.append_run_history(final.history_row())
        writer.save(destination)
    except WorkbookError as exc:
        raise WorkbookError(f"{exc} [{ErrorCode.OUTPUT_WRITE_FAILED.value}]") from None
    finally:
        writer.close()

    return final


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))
