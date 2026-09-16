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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
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
from .reporting import render_identity
from .security.redaction import sanitize_error, sanitize_text
from .security.sql_guard import assert_read_only
from .workbook.columns import VALIDATION_ERRORS_SHEET
from .workbook.reader import InvalidRow, SkippedRow, read_workbook
from .workbook.writer import ResultWriter

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

#: Stands in for "no upper bound" on --end-row, so the range check is one
#: comparison regardless of whether the person gave an end at all.
_NO_END_ROW = 2**63

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
    """One line's worth of an already-sanitized message.

    Collapsed onto one line so a multi-line driver error cannot break the
    ``[index/total] id  status`` layout, but never cut short: a truncated
    "..." is exactly the failure someone is trying to read past.
    """
    return " ".join(detail.split())


def _result_line(result: ExecutionResult) -> str:
    """One log line for one test case, whatever became of it.

    Every row the run touched says so, so the log accounts for the whole
    sheet rather than only the rows that reached a database.
    """
    code = f"[{result.error_code}] " if result.error_code else ""
    detail = f"  {code}{_short(result.remarks)}" if (code or result.remarks) else ""
    return f"  row {result.row_number:>4}  {result.test_case_id:<14} {result.status.value}{detail}"


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
    #: Restrict the run to workbook rows in this inclusive range (as Excel
    #: numbers them, matching ``row_number`` and every log line and sheet that
    #: already reports it). ``None`` on either end leaves that side open.
    #: Applied after ``--case``, so the two can be combined.
    start_row: int | None = None
    end_row: int | None = None
    output_dir: Path | None = None
    write_output: bool = True
    run_id: str = field(default_factory=new_run_id)


class ReconciliationRunner:
    """Executes the test cases in one workbook against one executor factory."""

    def __init__(self, schema: WorkbookSchema, factory: ExecutorFactory) -> None:
        self._schema = schema
        self._factory = factory

    def _engine_of(
        self, connection_name: str, side: QuerySide, declared: DatabaseType | None
    ) -> DatabaseType | None:
        """What engine this query will really meet.

        The connection is the authority: a workbook constant describes what the
        author meant, while the profile decides what the query is actually sent
        to. Asking the connection is what makes changing a profile's engine
        reach the dialect check at all. Factories that cannot say — the offline
        fakes — fall back to the workbook's own declaration.
        """
        ask = getattr(self._factory, "engine_of", None)
        if ask is None:
            return declared
        try:
            return ask(connection_name, side)
        except Exception:  # a factory that cannot resolve the name proves nothing
            return declared

    def check_dialects(
        self, cases: Sequence[TestCase], run_id: str, *, log: ProgressLog
    ) -> tuple[ExecutionResult, ...]:
        """Find rows written for a different engine than the one configured.

        Offline and instant: no connection is used and no query is sent, so
        this answers before there is anything to wait for.

        Each row is judged against the connection *it* names, not against one
        source for the whole run. A workbook whose sources span two platforms
        is therefore legal: the Oracle rows name an Oracle connection, the SQL
        Server rows name a SQL Server one, and only a row pointing at the wrong
        one is an error. That row costs one result; the rest still run.
        """
        checked_at = datetime.now(UTC).replace(microsecond=0)
        mismatches: list[ExecutionResult] = []
        for case in cases:
            scope = case.execution_scope
            sides = []
            if scope.uses_source:
                sides.append(
                    (
                        case.source_sql,
                        ErrorSide.SOURCE,
                        self._engine_of(case.source_connection, QuerySide.SOURCE, case.source_type),
                    )
                )
            if scope.uses_target:
                sides.append(
                    (
                        case.target_sql,
                        ErrorSide.TARGET,
                        self._engine_of(
                            case.target_connection, QuerySide.TARGET, DatabaseType.SQLSERVER
                        ),
                    )
                )
            for sql, error_side, engine in sides:
                if engine is None:
                    continue
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
                f"  {len(mismatches)} of {len(cases)} test(s) name a connection that speaks "
                f"a different dialect, and are recorded as ERROR:"
            )
            # The rows themselves are logged as they are recorded, every one of
            # them: a list cut off at five hides the row someone is looking for.
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

        results: list[ExecutionResult] = []
        writer: ResultWriter | None = None
        if opts.write_output:
            # Created now, before a single query runs: the output file exists
            # and is openable from this point on, and every write below saves
            # it again, so it is never more than one test behind the run.
            writer = ResultWriter(
                path,
                self._schema,
                run_id=opts.run_id,
                timestamp=started_at,
                output_dir=opts.output_dir,
            )
            log(f"  Result workbook: {writer.output_path}")

        def record(result: ExecutionResult, *, announce: bool = True) -> None:
            """Record one result: to the summary, to the file, and to the log.

            ``announce`` is off only where the caller has already printed a
            richer block for that row, so every test case reaches the log
            exactly once however it turned out.
            """
            results.append(result)
            if writer is not None:
                writer.write_row(result)
            if announce:
                log(_result_line(result))

        def record_all(rs: Iterable[ExecutionResult], *, announce: bool = True) -> None:
            """Record a group whose outcomes were all decided together.

            Each still reaches the log on its own line, but the workbook is
            saved once for the whole group rather than once per row: these
            outcomes were settled before the group was formed, so there is no
            intermediate state worth writing.
            """
            group = list(rs)
            if not group:
                return
            results.extend(group)
            if writer is not None:
                writer.write_rows(group)
            if announce:
                for result in group:
                    log(_result_line(result))

        undecided = [
            self._workbook_error_result(invalid, opts.run_id, started_at)
            for invalid in read.invalid
        ]
        undecided.extend(
            self._skipped_result(skipped, opts.run_id, started_at) for skipped in read.skipped
        )
        # Rows outside --case/--start-row/--end-row/--limit are already fully
        # decided, so they are written now rather than held until the end.
        undecided.extend(
            self._skipped_result(
                SkippedRow(case.row_number, case.test_case_id, reason), opts.run_id, started_at
            )
            for case, reason in deselected
        )
        if undecided:
            log("")
            log(f"Not run ({len(undecided)} test(s)), decided before anything was opened:")
        record_all(sorted(undecided, key=lambda r: r.row_number))

        validation = ValidationOutcome(passed=tuple(selected))
        try:
            # Before anything is opened, compiled or waited for: does each row
            # match the connection it names? Offline, so a wrong pairing costs
            # nothing to find.
            mismatches = self.check_dialects(selected, opts.run_id, log=log)
            mismatched_ids = {result.test_case_id for result in mismatches}
            record_all(mismatches)
            # A row bound for the wrong engine is not sent to any database, but
            # it no longer stops the rows that are correctly paired.
            mismatch_rows = tuple(
                _validation_row(
                    case,
                    opts.run_id,
                    datetime.now(UTC).replace(microsecond=0),
                    _SHEET_RESULT_MISMATCH,
                    ErrorSide.NONE,
                    "",
                    "",
                )
                for case in selected
                if case.test_case_id in mismatched_ids
            )
            selected = [c for c in selected if c.test_case_id not in mismatched_ids]

            # Compiling every query costs a second round trip per test. An
            # execute run skips it and lets the database report a bad query
            # when it runs one, which is what asking for results means. Ask
            # for the check by running --mode validate, or keep the gate in an
            # execute run with --on-syntax-error stop.
            compiles_first = (
                opts.mode is RunMode.VALIDATE or opts.on_syntax_error is OnSyntaxError.STOP
            )
            if not compiles_first:
                validation = replace(
                    ValidationOutcome(passed=tuple(selected)),
                    failures=mismatches,
                    rows=mismatch_rows,
                )
                log("")
                log(f"Execution phase: running {len(selected)} test(s), one at a time.")
                self._execute_all(selected, opts, record, record_all, log)
                return self._finish(path, opts, started_at, results, writer, validation)

            validation = self.validate_cases(selected, opts.run_id, log=log)
            # Mismatched rows belong on the evidence sheets too, even though
            # they never reached the database.
            # Only the rejections this pass found are recorded here: the
            # mismatches are merged in for the evidence sheets, but they were
            # already recorded above and must not be counted a second time.
            # The compile loop announced each of these as it checked it.
            record_all(validation.failures, announce=False)
            validation = replace(
                validation,
                failures=(*mismatches, *validation.failures),
                rows=(*mismatch_rows, *validation.rows),
            )
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
                record_all(
                    self._not_executed(case, opts.run_id, checked_at) for case in validation.passed
                )
            elif opts.mode is RunMode.VALIDATE:
                log("")
                log(
                    f"Validation run: stopping here by request. "
                    f"{len(validation.passed)} test(s) compiled and none were executed."
                )
                checked_at = datetime.now(UTC).replace(microsecond=0)
                record_all(
                    self._validated(case, opts.run_id, checked_at) for case in validation.passed
                )
            else:
                log("")
                log(f"Execution phase: running {len(validation.passed)} test(s), one at a time.")
                self._execute_all(validation.passed, opts, record, record_all, log)
        finally:
            self._factory.close_all()

        return self._finish(path, opts, started_at, results, writer, validation)

    def _execute_all(
        self,
        cases: Sequence[TestCase],
        opts: RunOptions,
        record: Callable[..., None],
        record_all: Callable[..., None],
        log: ProgressLog,
    ) -> None:
        """Run each test in row order, reporting and recording as it goes.

        Shared by both routes into execution, so a run that skipped the
        compile pass behaves identically once it starts executing. ``record``
        is what makes each result reach the result workbook immediately —
        this method knows nothing about files, only about running tests.

        Every case in ``cases`` produces log output, including the ones a
        ``--fail-fast`` stop means never ran: a test that vanishes from the
        log is indistinguishable from one nobody selected. Those are handed
        over as one group, because one answer settled all of them at once.
        """
        total = len(cases)
        for index, case in enumerate(cases, start=1):
            log(f"  [{index:>4}/{total}] {case.test_case_id}")
            result = self.execute_case(case, opts.run_id, log=log)
            # Already announced in full above, down to the query and the value.
            record(result, announce=False)
            detail = (
                f"  [{result.error_code}] {_short(result.remarks)}" if result.error_code else ""
            )
            log(f"      -> {result.status.value}{detail}")
            if opts.fail_fast and result.status is not ExecutionStatus.PASS:
                reason = "Stopped by --fail-fast"
                remaining = cases[index:]
                if remaining:
                    log("")
                    log(f"  {reason}. {len(remaining)} test(s) are not run:")
                    halted_at = datetime.now(UTC)
                    record_all(
                        self._skipped_result(
                            SkippedRow(later.row_number, later.test_case_id, reason),
                            opts.run_id,
                            halted_at,
                        )
                        for later in remaining
                    )
                return

    def _finish(
        self,
        path: Path,
        opts: RunOptions,
        started_at: datetime,
        results: list[ExecutionResult],
        writer: ResultWriter | None,
        validation: ValidationOutcome,
    ) -> RunSummary:
        """Close out the result workbook and summarise the run.

        Every row was already written as it was decided; what is left is the
        two validation sheets, which only mean something once the whole run
        is known, and the final save.
        """
        results.sort(key=lambda r: r.row_number)

        output_path: Path | None = None
        if writer is not None:
            writer.write_validation_report(validation.report_rows())
            writer.write_validation_errors(validation.error_rows())
            output_path = writer.finish()

        return RunSummary(
            run_id=opts.run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            input_path=path,
            output_path=output_path,
            results=results,
        )

    def execute_case(
        self, case: TestCase, run_id: str, *, log: ProgressLog | None = None
    ) -> ExecutionResult:
        """Execute and compare one test case. Never raises for case-level failures.

        ``log`` receives one line per side naming the connection actually used
        — engine, host, database — and the query sent to it, then the value
        and how long it took. All of it comes from :class:`ConnectionIdentity`
        and the workbook's own SQL, so nothing secret is ever formatted in.
        """
        emit: ProgressLog = log if log is not None else (lambda _m: None)
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
                self._log_before_query(emit, "SOURCE", source_executor, case.source_sql)
                query_started = perf_counter()
                source_value = source_executor.execute_scalar(case.source_sql, case.timeout_seconds)
                emit(f"      SOURCE  -> {source_value!r}  ({_elapsed_ms(query_started)} ms)")
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
                self._log_before_query(emit, "TARGET", target_executor, case.target_sql)
                query_started = perf_counter()
                target_value = target_executor.execute_scalar(case.target_sql, case.timeout_seconds)
                emit(f"      TARGET  -> {target_value!r}  ({_elapsed_ms(query_started)} ms)")
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

    def _log_before_query(
        self, log: ProgressLog, label: str, executor: QueryExecutor, sql: str
    ) -> None:
        """Name the connection a query is about to run on, and the query itself.

        ``test_connection()`` does no network round trip once a session is
        open — it reports what the driver already knows, so asking for it here
        costs nothing per query. If it fails for any reason, execution carries
        on without the debug line rather than losing the real result over it.
        """
        try:
            identity = executor.test_connection()
        except Exception:
            log(f"      {label}  (connection details unavailable)")
        else:
            log(f"      {label}  {render_identity(identity)}")
        log(f"              {' '.join(sql.split())}")

    def _executor_for(self, case: TestCase, side: QuerySide) -> QueryExecutor:
        if side is QuerySide.SOURCE:
            connection_name, declared = case.source_connection, case.source_type
        else:
            # The migration target is a SQL Server database, whichever
            # connection the row names for it.
            connection_name, declared = case.target_connection, DatabaseType.SQLSERVER
        # A sheet that declares an engine is asserting something and is checked
        # against the connection. A sheet that says nothing asserts nothing, so
        # the connection's own engine is used and nothing can contradict it.
        database_type = declared or self._engine_of(connection_name, side, None)
        if database_type is None:
            database_type = DatabaseType.SQLSERVER
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

    if options.start_row is not None or options.end_row is not None:
        start = options.start_row if options.start_row is not None else 1
        end = options.end_row if options.end_row is not None else _NO_END_ROW
        if start < 1:
            raise WorkbookError(f"--start-row must be 1 or greater (got {start})")
        if options.end_row is not None and end < start:
            raise WorkbookError(
                f"--end-row ({end}) must be greater than or equal to --start-row ({start})"
            )
        shown_end = "end" if options.end_row is None else str(end)
        label = f"Outside --start-row {start}/--end-row {shown_end}"
        deselected.extend((c, label) for c in selected if not (start <= c.row_number <= end))
        selected = [c for c in selected if start <= c.row_number <= end]

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
