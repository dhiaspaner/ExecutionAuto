"""Console rendering shared by the CLI and the interactive runner.

Both entry points print the same run report, so it is built in one place. Every
line is plain ASCII: a Windows console under a legacy code page cannot encode
much beyond that, and a finished run must never be lost to a print failure.
"""

from __future__ import annotations

import contextlib
import sys

from .models import ConnectionIdentity, RunSummary

__all__ = ["make_output_encoding_safe", "render_summary"]

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
