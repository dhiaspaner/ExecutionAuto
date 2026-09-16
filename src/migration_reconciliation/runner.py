"""The reconciliation runner.

Orchestrates one execution: read, validate, execute, compare, write. It knows
nothing about domains, nothing about Excel headers, and nothing about database
drivers — only the semantic models, the executor protocols and the comparison
registry.

Isolation rule: one test case cannot stop the run. Every per-case failure is
captured as an ``ERROR`` result with a sanitized message, and execution moves
on — unless ``--fail-fast`` was requested.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from .database.base import ExecutorFactory, QueryExecutor, QuerySide
from .database.dialect import describe_incompatibility
from .errors import (
    ComparisonError,
    ReconciliationError,
    SqlValidationError,
    SyntaxCheckUnavailableError,
    WorkbookError,
)
from .evaluation.comparators import compare, variance_of
from .models import (
    SQL_SYNTAX_ERROR_CODE,
    DatabaseType,
    ErrorSide,
    ExecutionResult,
    ExecutionStatus,
    OnSyntaxError,
    RunMode,
    RunSummary,
    ScalarValue,
    TestCase,
    WorkbookSchema,
)
from .security.redaction import sanitize_error, sanitize_text
from .security.sql_guard import assert_read_only
from .workbook.columns import VALIDATION_ERRORS_SHEET
from .workbook.reader import InvalidRow, SkippedRow, read_workbook
from .workbook.writer import write_results

__all__ = ["ReconciliationRunner", "RunOptions", "new_run_id"]

# Short, stable codes so results can be filtered without parsing prose.
_CODE_SQL_REJECTED = "SQL_REJECTED"
_CODE_SOURCE_EXEC = "SOURCE_EXEC_FAILED"
_CODE_TARGET_EXEC = "TARGET_EXEC_FAILED"
_CODE_COMPARISON = "COMPARISON_FAILED"
_CODE_WORKBOOK_ROW = "WORKBOOK_ROW_INVALID"
_CODE_UNEXPECTED = "UNEXPECTED_ERROR"
_CODE_SYNTAX = SQL_SYNTAX_ERROR_CODE
_CODE_DIALECT = "SQL_DIALECT_MISMATCH"

#: How many mismatched tests the platform message names before "and N more".
_NAMED_IN_MISMATCH_MESSAGE = 5

#: How much of a sanitized message one progress line carries. The whole of it
#: reaches the ``Validation Errors`` sheet, so the console stays readable when
#: two hundred rows share one broken view.
_PROGRESS_DETAIL_CHARS = 110

#: The validation sheets describe compiling, not reconciling, so they keep
#: their own vocabulary: a rejected query reads SYNTAX ERROR there even though
#: the test's own Status column records the plain ERROR it produced.
_SHEET_RESULT_REJECTED = "SYNTAX ERROR"
_SHEET_RESULT_OK = "OK"
_SHEET_RESULT_UNCHECKED = "NOT CHECKED"
_SHEET_RESULT_MISMATCH = "PLATFORM MISMATCH"

#: Called with one already-sanitized progress line. No SQL, no connection
#: string and no credential is ever formatted into these.
ProgressLog = Callable[[str], None]


def _short(detail: str) -> str:
    collapsed = " ".join(detail.split())
    if len(collapsed) <= _PROGRESS_DETAIL_CHARS:
        return collapsed
    return f"{collapsed[:_PROGRESS_DETAIL_CHARS].rstrip()}..."


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """What the pre-execution check decided about the whole run."""

    passed: tuple[TestCase, ...] = ()
    failures: tuple[ExecutionResult, ...] = ()
    warnings: tuple[str, ...] = ()
    #: One ``Syntax Validation`` row per test checked, in row order.
    rows: tuple[dict[str, object], ...] = ()

    @property
    def gate_closed(self) -> bool:
        """True when something did not compile, so nothing may be executed."""
        return bool(self.failures)

    def report_rows(self) -> tuple[dict[str, object], ...]:
        """Every test the check looked at, passed and failed alike."""
        return self.rows

    def error_rows(self) -> tuple[dict[str, object], ...]:
        """One ``Validation Errors`` row per query the database refused."""
        return tuple(
            {
                "Run_ID": result.run_id,
                "Checked_At_UTC": result.executed_at,
                "Test_ID": result.test_case_id,
                "Row": result.row_number,
                "Status": (
                    _SHEET_RESULT_MISMATCH
                    if result.error_code == _CODE_DIALECT
                    else _SHEET_RESULT_REJECTED
                ),
                "Platform": result.error_side.value,
                "Error_Code": result.error_code,
                "Error_Detail": result.remarks,
            }
            for result in self.failures
        )


def new_run_id() -> str:
    """A short, collision-resistant identifier for one execution."""
    return uuid.uuid4().hex[:12]


def _validation_row(
    case: TestCase,
    run_id: str,
    checked_at: datetime,
    result: str,
    error_side: ErrorSide,
    error_code: str,
    detail: str,
) -> dict[str, object]:
    """One ``Syntax Validation`` row. Carries no SQL and no row data."""
    return {
        "Run_ID": run_id,
        "Checked_At_UTC": checked_at,
        "Test_ID": case.test_case_id,
        "Row": case.row_number,
        "Result": result,
        "Platform": error_side.value,
        "Error_Code": error_code,
        "Error_Detail": detail,
    }


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Everything the CLI can vary about a run."""

    case_ids: tuple[str, ...] = ()
    limit: int | None = None
    fail_fast: bool = False
    #: ``VALIDATE`` stops after compiling every query; ``EXECUTE`` goes on to
    #: run the ones that compiled.
    mode: RunMode = RunMode.EXECUTE
    #: What to do about a query the database will not compile. The default
    #: records it as ``ERROR`` and runs the rest.
    on_syntax_error: OnSyntaxError = OnSyntaxError.CONTINUE
    output_dir: Path | None = None
    write_output: bool = True
    run_id: str = field(default_factory=new_run_id)


class ReconciliationRunner:
    """Executes the test cases in one workbook against one executor factory."""

    def __init__(self, schema: WorkbookSchema, factory: ExecutorFactory) -> None:
        self._schema = schema
        self._factory = factory

    def check_dialects(
        self, cases: Sequence[TestCase], run_id: str, *, log: ProgressLog
    ) -> tuple[ExecutionResult, ...]:
        """Find rows written for a different engine than the one configured.

        Offline and instant: no connection is used and no query is sent, so
        this answers before there is anything to wait for.

        A foreign dialect is not one bad query — it is the wrong profile for
        these rows. The SQL may be perfectly correct on the database it was
        written for, so the run is stopped rather than the rows being failed
        one by one: a workbook whose sources span two platforms is meant to be
        run once per platform, not half-run against one of them.
        """
        checked_at = datetime.now(UTC).replace(microsecond=0)
        mismatches: list[ExecutionResult] = []
        for case in cases:
            scope = case.execution_scope
            sides = []
            if scope.uses_source:
                sides.append((case.source_sql, ErrorSide.SOURCE, case.source_type))
            if scope.uses_target:
                # The migration target is always SQL Server.
                sides.append((case.target_sql, ErrorSide.TARGET, DatabaseType.SQLSERVER))
            for sql, error_side, engine in sides:
                mismatch = describe_incompatibility(sql, engine)
                if mismatch is None:
                    continue
                label = f"{error_side.value.capitalize()} query"
                mismatches.append(
                    self._validation_failure(
                        case, run_id, checked_at, error_side, _CODE_DIALECT, f"{label}: {mismatch}"
                    )
                )
                break
        if mismatches:
            log("")
            log(
                f"PLATFORM MISMATCH: {len(mismatches)} of {len(cases)} test(s) are written "
                f"for a different database engine than this profile configures."
            )
            for result in mismatches[:_NAMED_IN_MISMATCH_MESSAGE]:
                log(f"    row {result.row_number}  {result.test_case_id}  {_short(result.remarks)}")
            remaining = len(mismatches) - _NAMED_IN_MISMATCH_MESSAGE
            if remaining > 0:
                log(f"    ... and {remaining} more")
            log("")
            log(
                "  Nothing was validated and nothing was executed: the profile is wrong for "
                "these rows, not the SQL."
            )
            log(
                "  A workbook whose sources span two platforms is run once per platform, "
                "with only that platform's rows enabled."
            )
        return tuple(mismatches)

    def validate_cases(
        self,
        cases: Sequence[TestCase],
        run_id: str,
        *,
        log: ProgressLog,
    ) -> ValidationOutcome:
        """Compile every query without executing any of them.

        Two checks per side: the offline read-only guard, then the database's
        own compiler. Every case is checked even after one has failed, so a
        single run names all the broken SQL rather than one row per run.
        """
        checked_at = datetime.now(UTC).replace(microsecond=0)
        passed: list[TestCase] = []
        failures: list[ExecutionResult] = []
        warnings: list[str] = []
        unavailable: set[str] = set()

        total = len(cases)
        rows: list[dict[str, object]] = []
        log("")
        log(f"Validation phase: compiling {total} test(s). No query is executed in this pass.")
        for index, case in enumerate(cases, start=1):
            unchecked: set[str] = set()
            failure = self._validate_one(case, run_id, checked_at, unavailable, warnings, unchecked)
            if failure is None:
                passed.append(case)
                # "Not checked" is not "fine": the database could not be asked,
                # so the row says so rather than implying the SQL was proven.
                result = _SHEET_RESULT_UNCHECKED if unchecked else _SHEET_RESULT_OK
                log(f"  [{index:>4}/{total}] {case.test_case_id}  {result.lower()}")
                rows.append(
                    _validation_row(case, run_id, checked_at, result, ErrorSide.NONE, "", "")
                )
            else:
                failures.append(failure)
                log(
                    f"  [{index:>4}/{total}] {case.test_case_id}  {failure.status.value}  "
                    f"[{failure.error_code}] {_short(failure.remarks)}"
                )
                rows.append(
                    _validation_row(
                        case,
                        run_id,
                        checked_at,
                        _SHEET_RESULT_REJECTED,
                        failure.error_side,
                        failure.error_code,
                        failure.remarks,
                    )
                )
        for warning in warnings:
            log(f"  warning: {warning}")
        return ValidationOutcome(tuple(passed), tuple(failures), tuple(warnings), tuple(rows))

    def _validate_one(
        self,
        case: TestCase,
        run_id: str,
        checked_at: datetime,
        unavailable: set[str],
        warnings: list[str],
        unchecked: set[str],
    ) -> ExecutionResult | None:
        """``None`` when every side this case uses compiled.

        ``unchecked`` gains this case's id when a side could not be checked at
        all, so the report can distinguish "compiled" from "never asked".
        """
        scope = case.execution_scope
        sides: list[tuple[str, str, ErrorSide, QuerySide]] = []
        if scope.uses_source:
            sides.append(
                (case.source_sql, case.source_connection, ErrorSide.SOURCE, QuerySide.SOURCE)
            )
        if scope.uses_target:
            sides.append(
                (case.target_sql, case.target_connection, ErrorSide.TARGET, QuerySide.TARGET)
            )

        for sql, connection, error_side, side in sides:
            label = f"{error_side.value.capitalize()} query"
            try:
                assert_read_only(sql, label=label)
            except SqlValidationError as exc:
                return self._validation_failure(
                    case, run_id, checked_at, error_side, _CODE_SQL_REJECTED, str(exc)
                )

            try:
                self._executor_for(case, side).validate_syntax(sql, case.timeout_seconds)
            except SyntaxCheckUnavailableError as exc:
                # Nothing was proven either way, so this is not a failure — but
                # the run says out loud that it is going in unchecked.
                unchecked.add(case.test_case_id)
                if connection not in unavailable:
                    unavailable.add(connection)
                    warnings.append(
                        f"[{connection}] could not be asked to check SQL before running it, "
                        f"so its queries were not validated: {sanitize_error(exc)}"
                    )
                continue
            except Exception as exc:
                return self._validation_failure(
                    case, run_id, checked_at, error_side, _CODE_SYNTAX, sanitize_error(exc)
                )
        return None

    def _validation_failure(
        self,
        case: TestCase,
        run_id: str,
        checked_at: datetime,
        error_side: ErrorSide,
        error_code: str,
        detail: str,
    ) -> ExecutionResult:
        """A rejected query, recorded without ever claiming a result."""
        return ExecutionResult(
            test_case_id=case.test_case_id,
            row_number=case.row_number,
            status=ExecutionStatus.ERROR,
            executed_at=checked_at,
            run_id=run_id,
            duration_ms=0,
            remarks=sanitize_text(detail),
            error_side=error_side,
            error_code=error_code,
        )

    def _validated(self, case: TestCase, run_id: str, checked_at: datetime) -> ExecutionResult:
        """A query that compiled in a validate-only run.

        Nothing went wrong with this row, so it carries no error and no
        observation: ``VALIDATED`` in the status column already says what
        happened, and one sentence repeated down two hundred rows only buries
        the handful that do have something to report.
        """
        return ExecutionResult(
            test_case_id=case.test_case_id,
            row_number=case.row_number,
            status=ExecutionStatus.VALIDATED,
            executed_at=checked_at,
            run_id=run_id,
            duration_ms=0,
            remarks="",
            error_side=ErrorSide.NONE,
            error_code="",
        )

    def _not_executed(self, case: TestCase, run_id: str, checked_at: datetime) -> ExecutionResult:
        """A query that compiled, in a run that another query stopped.

        Also blank: this row's SQL was fine. Why the run stopped is said once
        in the console and in the ``Validation Errors`` sheet, rather than
        copied onto every innocent row as if each had a fault of its own.
        """
        return ExecutionResult(
            test_case_id=case.test_case_id,
            row_number=case.row_number,
            status=ExecutionStatus.NOT_EXECUTED,
            executed_at=checked_at,
            run_id=run_id,
            duration_ms=0,
            remarks="",
            error_side=ErrorSide.NONE,
            error_code="",
        )

    def run(
        self,
        workbook_path: str | Path,
        options: RunOptions | None = None,
        *,
        on_progress: ProgressLog | None = None,
    ) -> RunSummary:
        """Run the workbook and return a summary. Always closes every executor.

        Every selected query is compiled first. If any one of them is rejected,
        nothing is executed at all: a run that would fail on row 200 must not
        spend 199 queries finding that out.
        """
        opts = options or RunOptions()
        log: ProgressLog = on_progress if on_progress is not None else (lambda _m: None)
        path = Path(workbook_path)
        started_at = datetime.now(UTC).replace(microsecond=0)

        read = read_workbook(path, self._schema)
        selected, deselected = _select(read.test_cases, opts)

        results: list[ExecutionResult] = [
            self._workbook_error_result(invalid, opts.run_id, started_at)
            for invalid in read.invalid
        ]
        results.extend(
            self._skipped_result(skipped, opts.run_id, started_at) for skipped in read.skipped
        )

        validation = ValidationOutcome(passed=tuple(selected))
        stopped_early = False
        try:
            # Before anything is opened, compiled or waited for: are these rows
            # even written for the databases this profile names?
            mismatches = self.check_dialects(selected, opts.run_id, log=log)
            if mismatches:
                mismatched_ids = {result.test_case_id for result in mismatches}
                results.extend(mismatches)
                checked_at = datetime.now(UTC).replace(microsecond=0)
                results.extend(
                    self._not_executed(case, opts.run_id, checked_at)
                    for case in selected
                    if case.test_case_id not in mismatched_ids
                )
                outcome = ValidationOutcome(
                    failures=mismatches,
                    rows=tuple(
                        _validation_row(
                            case,
                            opts.run_id,
                            checked_at,
                            _SHEET_RESULT_MISMATCH
                            if case.test_case_id in mismatched_ids
                            else _SHEET_RESULT_UNCHECKED,
                            ErrorSide.NONE,
                            "",
                            "",
                        )
                        for case in selected
                    ),
                )
                return self._finish(path, opts, started_at, results, deselected, outcome)

            validation = self.validate_cases(selected, opts.run_id, log=log)
            results.extend(validation.failures)
            if validation.gate_closed:
                log("")
                log(
                    f"  {len(validation.failures)} of {len(selected)} test(s) were rejected "
                    f"by the database and are recorded as ERROR."
                )
                for failure in validation.failures:
                    log(
                        f"    row {failure.row_number}  {failure.test_case_id or '(no id)'}  "
                        f"[{failure.error_code}] {_short(failure.remarks)}"
                    )
                log(
                    f"  The '{VALIDATION_ERRORS_SHEET}' sheet of the result workbook lists "
                    f"every one, with the database's full message."
                )
            else:
                log(f"  All {len(validation.passed)} test(s) compiled. Nothing was rejected.")

            if validation.gate_closed and opts.on_syntax_error is OnSyntaxError.STOP:
                log("")
                log(
                    f"  Nothing was executed: On_Syntax_Error = stop. "
                    f"{len(validation.passed)} test(s) that did compile were left unrun."
                )
                checked_at = datetime.now(UTC).replace(microsecond=0)
                results.extend(
                    self._not_executed(case, opts.run_id, checked_at) for case in validation.passed
                )
            elif opts.mode is RunMode.VALIDATE:
                log("")
                log(
                    f"Validation run: stopping here by request. "
                    f"{len(validation.passed)} test(s) compiled and none were executed."
                )
                checked_at = datetime.now(UTC).replace(microsecond=0)
                results.extend(
                    self._validated(case, opts.run_id, checked_at) for case in validation.passed
                )
            else:
                log("")
                log(f"Execution phase: running {len(validation.passed)} test(s), one at a time.")
                for index, case in enumerate(validation.passed, start=1):
                    if stopped_early:
                        halted = SkippedRow(
                            case.row_number, case.test_case_id, "Stopped by --fail-fast"
                        )
                        results.append(self._skipped_result(halted, opts.run_id, started_at))
                        continue
                    result = self.execute_case(case, opts.run_id)
                    results.append(result)
                    detail = (
                        f"  [{result.error_code}] {_short(result.remarks)}"
                        if result.error_code
                        else ""
                    )
                    log(
                        f"  [{index:>4}/{len(validation.passed)}] {case.test_case_id}  "
                        f"{result.status.value}{detail}"
                    )
                    if opts.fail_fast and result.status is not ExecutionStatus.PASS:
                        stopped_early = True
        finally:
            self._factory.close_all()

        return self._finish(path, opts, started_at, results, deselected, validation)

    def _finish(
        self,
        path: Path,
        opts: RunOptions,
        started_at: datetime,
        results: list[ExecutionResult],
        deselected: Sequence[tuple[TestCase, str]],
        validation: ValidationOutcome,
    ) -> RunSummary:
        """Record the deselected rows, write the workbook and summarise.

        Shared by every way a run can end, so a run stopped early still leaves
        the same evidence as one that finished.
        """
        results.extend(
            self._skipped_result(
                SkippedRow(case.row_number, case.test_case_id, reason), opts.run_id, started_at
            )
            for case, reason in deselected
        )
        results.sort(key=lambda r: r.row_number)

        output_path: Path | None = None
        if opts.write_output:
            output_path = write_results(
                path,
                self._schema,
                results,
                run_id=opts.run_id,
                timestamp=started_at,
                output_dir=opts.output_dir,
                validation_errors=validation.error_rows(),
                validation_rows=validation.report_rows(),
            )

        return RunSummary(
            run_id=opts.run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            input_path=path,
            output_path=output_path,
            results=results,
        )

    def execute_case(self, case: TestCase, run_id: str) -> ExecutionResult:
        """Execute and compare one test case. Never raises for case-level failures."""
        started = perf_counter()
        executed_at = datetime.now(UTC).replace(microsecond=0)
        source_value: ScalarValue = None
        target_value: ScalarValue = None

        def finish(
            status: ExecutionStatus,
            remarks: str,
            *,
            error_side: ErrorSide = ErrorSide.NONE,
            error_code: str = "",
            variance: Decimal | None = None,
        ) -> ExecutionResult:
            computed_variance = (
                variance if variance is not None else variance_of(source_value, target_value)
            )
            return ExecutionResult(
                test_case_id=case.test_case_id,
                row_number=case.row_number,
                status=status,
                executed_at=executed_at,
                run_id=run_id,
                duration_ms=_elapsed_ms(started),
                source_result=source_value,
                target_result=target_value,
                variance=computed_variance,
                remarks=sanitize_text(remarks),
                error_side=error_side,
                error_code=error_code,
            )

        scope = case.execution_scope

        # 1. Offline SQL validation, before any executor is obtained. Only the
        # sides this test declares are validated: an unused side has no SQL by
        # the time a row reaches here, and rejecting its emptiness would fail a
        # test that is correctly written.
        for sql, side, error_side in (
            (case.source_sql, "Source query", ErrorSide.SOURCE),
            (case.target_sql, "Target query", ErrorSide.TARGET),
        ):
            if error_side is ErrorSide.SOURCE and not scope.uses_source:
                continue
            if error_side is ErrorSide.TARGET and not scope.uses_target:
                continue
            try:
                assert_read_only(sql, label=side)
            except SqlValidationError as exc:
                return finish(
                    ExecutionStatus.ERROR,
                    str(exc),
                    error_side=error_side,
                    error_code=_CODE_SQL_REJECTED,
                )

        # 2. Source execution. A scope that does not name the source never
        # opens it, so a TARGET_ONLY test cannot be blocked by a source the
        # run has no business connecting to.
        if scope.uses_source:
            try:
                source_executor = self._executor_for(case, QuerySide.SOURCE)
                source_value = source_executor.execute_scalar(case.source_sql, case.timeout_seconds)
            except Exception as exc:
                return finish(
                    ExecutionStatus.ERROR,
                    sanitize_error(exc),
                    error_side=ErrorSide.SOURCE,
                    error_code=_error_code(exc, _CODE_SOURCE_EXEC),
                )

        # 3. Target execution, on the same terms.
        if scope.uses_target:
            try:
                target_executor = self._executor_for(case, QuerySide.TARGET)
                target_value = target_executor.execute_scalar(case.target_sql, case.timeout_seconds)
            except Exception as exc:
                return finish(
                    ExecutionStatus.ERROR,
                    sanitize_error(exc),
                    error_side=ErrorSide.TARGET,
                    error_code=_error_code(exc, _CODE_TARGET_EXEC),
                )

        # 4. Comparison.
        try:
            outcome = compare(
                case.comparison_rule, source_value, target_value, case.tolerance, scope
            )
        except ComparisonError as exc:
            return finish(
                ExecutionStatus.ERROR,
                str(exc),
                error_side=ErrorSide.COMPARISON,
                error_code=_CODE_COMPARISON,
            )

        if outcome.profiled:
            status = ExecutionStatus.PROFILED
        else:
            status = ExecutionStatus.PASS if outcome.passed else ExecutionStatus.FAIL
        return finish(status, outcome.remarks, variance=outcome.variance)

    def _executor_for(self, case: TestCase, side: QuerySide) -> QueryExecutor:
        if side is QuerySide.SOURCE:
            connection_name, database_type = case.source_connection, case.source_type
        else:
            # The migration target is always SQL Server.
            connection_name, database_type = case.target_connection, DatabaseType.SQLSERVER
        return self._factory.get_executor(
            connection_name=connection_name,
            database_type=database_type,
            test_case_id=case.test_case_id,
            side=side,
        )

    def _skipped_result(
        self, skipped: SkippedRow, run_id: str, executed_at: datetime
    ) -> ExecutionResult:
        return ExecutionResult(
            test_case_id=skipped.test_case_id,
            row_number=skipped.row_number,
            status=ExecutionStatus.SKIPPED,
            executed_at=executed_at,
            run_id=run_id,
            duration_ms=0,
            remarks=sanitize_text(skipped.reason),
        )

    def _workbook_error_result(
        self, invalid: InvalidRow, run_id: str, executed_at: datetime
    ) -> ExecutionResult:
        return ExecutionResult(
            test_case_id=invalid.test_case_id,
            row_number=invalid.row_number,
            status=ExecutionStatus.ERROR,
            executed_at=executed_at,
            run_id=run_id,
            duration_ms=0,
            remarks=sanitize_text(invalid.message),
            error_side=ErrorSide.WORKBOOK,
            error_code=_CODE_WORKBOOK_ROW,
        )


def _select(
    cases: Sequence[TestCase], options: RunOptions
) -> tuple[list[TestCase], list[tuple[TestCase, str]]]:
    """Split enabled cases into those to run and those excluded by the options."""
    selected = list(cases)
    deselected: list[tuple[TestCase, str]] = []

    if options.case_ids:
        wanted = {c.casefold() for c in options.case_ids}
        matched = [c for c in selected if c.test_case_id.casefold() in wanted]
        found = {c.test_case_id.casefold() for c in matched}
        unknown = sorted(c for c in options.case_ids if c.casefold() not in found)
        if unknown:
            raise WorkbookError(
                f"No enabled test case matches --case {', '.join(unknown)}. "
                f"Check the id and that the row is enabled."
            )
        deselected.extend(
            (c, "Not selected by --case")
            for c in selected
            if c.test_case_id.casefold() not in wanted
        )
        selected = matched

    if options.limit is not None:
        if options.limit < 0:
            raise WorkbookError(f"--limit must be 0 or greater (got {options.limit})")
        deselected.extend((c, "Beyond --limit") for c in selected[options.limit :])
        selected = selected[: options.limit]

    return selected, deselected


def _error_code(exc: BaseException, expected: str) -> str:
    """Deliberate framework errors keep their specific code; anything else is unexpected."""
    return expected if isinstance(exc, ReconciliationError) else _CODE_UNEXPECTED


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))
