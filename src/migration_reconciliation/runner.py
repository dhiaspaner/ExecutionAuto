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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from .database.base import ExecutorFactory, QueryExecutor, QuerySide
from .errors import ComparisonError, ReconciliationError, SqlValidationError, WorkbookError
from .evaluation.comparators import compare, variance_of
from .models import (
    DatabaseType,
    ErrorSide,
    ExecutionResult,
    ExecutionStatus,
    RunSummary,
    ScalarValue,
    TestCase,
    WorkbookSchema,
)
from .security.redaction import sanitize_error, sanitize_text
from .security.sql_guard import assert_read_only
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


def new_run_id() -> str:
    """A short, collision-resistant identifier for one execution."""
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Everything the CLI can vary about a run."""

    case_ids: tuple[str, ...] = ()
    limit: int | None = None
    fail_fast: bool = False
    output_dir: Path | None = None
    write_output: bool = True
    run_id: str = field(default_factory=new_run_id)


class ReconciliationRunner:
    """Executes the test cases in one workbook against one executor factory."""

    def __init__(self, schema: WorkbookSchema, factory: ExecutorFactory) -> None:
        self._schema = schema
        self._factory = factory

    def run(self, workbook_path: str | Path, options: RunOptions | None = None) -> RunSummary:
        """Run the workbook and return a summary. Always closes every executor."""
        opts = options or RunOptions()
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

        stopped_early = False
        try:
            for case in selected:
                if stopped_early:
                    halted = SkippedRow(
                        case.row_number, case.test_case_id, "Stopped by --fail-fast"
                    )
                    results.append(self._skipped_result(halted, opts.run_id, started_at))
                    continue
                result = self.execute_case(case, opts.run_id)
                results.append(result)
                if opts.fail_fast and result.status is not ExecutionStatus.PASS:
                    stopped_early = True
        finally:
            self._factory.close_all()

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

        # 1. Offline SQL validation, before any executor is obtained.
        for sql, side, error_side in (
            (case.source_sql, "Source query", ErrorSide.SOURCE),
            (case.target_sql, "Target query", ErrorSide.TARGET),
        ):
            try:
                assert_read_only(sql, label=side)
            except SqlValidationError as exc:
                return finish(
                    ExecutionStatus.ERROR,
                    str(exc),
                    error_side=error_side,
                    error_code=_CODE_SQL_REJECTED,
                )

        # 2. Source execution.
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

        # 3. Target execution.
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
            outcome = compare(case.comparison_rule, source_value, target_value, case.tolerance)
        except ComparisonError as exc:
            return finish(
                ExecutionStatus.ERROR,
                str(exc),
                error_side=ErrorSide.COMPARISON,
                error_code=_CODE_COMPARISON,
            )

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
