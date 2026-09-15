"""Console rendering shared by the CLI and the interactive runner.

Both entry points print the same run report, so it is built in one place. Every
line is plain ASCII: a Windows console under a legacy code page cannot encode
much beyond that, and a finished run must never be lost to a print failure.
"""

from __future__ import annotations

import contextlib
import sys
from typing import TYPE_CHECKING

from .models import ConnectionIdentity, RunSummary, TestStatus

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime only
    from .execution.engine import RunReport

__all__ = [
    "make_output_encoding_safe",
    "render_identity",
    "render_run_report",
    "render_summary",
]

RULE_WIDTH = 72


def make_output_encoding_safe() -> None:
    """Never let a console code page turn a finished run into a traceback.

    Windows picks the active code page for redirected output, and a legacy one
    such as cp437 cannot encode every character a message may carry. Degrading
    a stray character to ``?`` is always better than losing the run report.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - replaced stream (pytest capture)
            continue
        # A detached or closed stream is nothing to fail a run over.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(errors="replace")


def render_summary(summary: RunSummary, *, no_output_note: str) -> list[str]:
    """The per-case table and totals, as lines ready to print."""
    lines = ["", f"Run {summary.run_id} - {summary.input_path.name}", "-" * RULE_WIDTH]
    for result in summary.results:
        lines.append(
            f"  {result.status.value:<7} {result.test_case_id:<14} "
            f"row {result.row_number:<4} {result.duration_ms:>5} ms  {result.remarks}"
        )
    lines.append("-" * RULE_WIDTH)
    lines.append(
        f"  {summary.passed} passed, {summary.failed} failed, "
        f"{summary.errored} errors, {summary.skipped} skipped "
        f"({summary.executed} executed in {summary.duration_ms} ms)"
    )
    if summary.output_path is not None:
        lines.append(f"  Results written to: {summary.output_path}")
        lines.append(f"  Original workbook unchanged: {summary.input_path}")
    else:
        lines.append(no_output_note)
    if not summary.is_clean:
        lines.append("")
        lines.append("  Review FAIL and ERROR rows above before signing off the migration.")
    return lines


def render_identity(identity: ConnectionIdentity) -> str:
    """One line describing an established connection. Carries no secret."""
    parts = [f"{identity.connection_name:<7} {identity.database_type.value}"]
    if identity.server_description:
        parts.append(f"at {identity.server_description}")
    if identity.database_name:
        parts.append(f"db={identity.database_name}")
    if identity.account_name:
        parts.append(f"user={identity.account_name}")
    if identity.product_version:
        parts.append(f"version={identity.product_version}")
    return "  " + " ".join(parts)


def render_run_report(report: RunReport, *, no_output_note: str) -> list[str]:
    """The per-test table and totals for a reconciliation-template run.

    Deliberately the same shape as :func:`render_summary`: the two entry points
    produce reports a person can read side by side.
    """
    if report.dry_run:
        return _render_dry_run(report)

    lines = ["", f"Run {report.run_id} - {report.workbook_path.name}", "-" * RULE_WIDTH]
    for outcome in report.outcomes:
        platform = f" [{outcome.platform.value}]" if outcome.platform.value else ""
        code = f" {outcome.error_code}" if outcome.error_code else ""
        lines.append(
            f"  {outcome.status.value:<12} {outcome.test_id:<14} "
            f"row {outcome.row_number:<4}{platform}{code}  {outcome.observation}"
        )
    lines.append("-" * RULE_WIDTH)
    if report.stopped_by_validation:
        lines.append("  Validation pass rejected the SQL above, so nothing was executed:")
        lines.append("  no reconciliation query ran, and no result was recorded for any row.")
        lines.append("-" * RULE_WIDTH)
    lines.append(
        f"  {report.passed} passed, {report.failed} failed, {report.profiled} profiled, "
        f"{report.blocked_error} blocked/error, {report.not_executed} not executed, "
        f"{report.disabled} disabled"
    )
    if report.syntax_errors:
        lines.append(
            f"  {report.syntax_errors} of those is SYNTAX ERROR: SQL the database refused "
            f"to compile."
            if report.syntax_errors == 1
            else f"  {report.syntax_errors} of those are SYNTAX ERROR: SQL the database "
            f"refused to compile."
        )
    lines.append(f"  Overall: {report.overall_status} ({report.enabled_tests} enabled test(s))")
    if report.output_path is not None:
        lines.append(f"  Results written to: {report.output_path}")
        if report.output_path != report.workbook_path:
            lines.append(f"  Original workbook unchanged: {report.workbook_path}")
    else:
        lines.append(no_output_note)
    if report.overall_status != "PASS":
        lines.append("")
        lines.append("  Review every row above that is not PASS before signing off the migration.")
    return lines


def _render_dry_run(report: RunReport) -> list[str]:
    """A validation report, not a run report.

    Listing two hundred identical "not executed" rows would bury the handful of
    rows that actually have something to say, so a dry run reports only what it
    found wrong and how much it found right.
    """
    lines = ["", f"Dry run {report.run_id} - {report.workbook_path.name}", "-" * RULE_WIDTH]
    notable = [
        outcome
        for outcome in report.outcomes
        if outcome.status not in {TestStatus.NOT_EXECUTED, TestStatus.DISABLED}
    ]
    for outcome in notable:
        platform = f" [{outcome.platform.value}]" if outcome.platform.value else ""
        code = f" {outcome.error_code}" if outcome.error_code else ""
        lines.append(
            f"  {outcome.status.value:<12} {outcome.test_id:<14} "
            f"row {outcome.row_number:<4}{platform}{code}  {outcome.error_detail}"
        )
    if notable:
        lines.append("-" * RULE_WIDTH)

    ready = report.not_executed
    misconfigured = report.blocked_error
    lines.append(
        f"  {ready} test(s) validated and ready to run, "
        f"{misconfigured} misconfigured, {report.disabled} disabled"
    )
    lines.append("  Nothing was executed, nothing was written, no database was opened.")
    if misconfigured:
        lines.append("")
        lines.append("  Fix the rows above, then run the dry run again.")
    return lines
