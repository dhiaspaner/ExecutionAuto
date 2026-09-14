"""Optional integration run against a real, local SQL Server.

Skipped unless every one of these is true, in this order:

1. ``MIGRATION_RECONCILIATION_ALLOW_DB_TESTS=1`` is set, deliberately, by a
   person who owns the databases.
2. ``MIGRATION_RECONCILIATION_PROFILE`` points at a local TOML profile.
3. ``MIGRATION_RECONCILIATION_WORKBOOK`` points at a workbook to run.

The profile is **never committed**: keep it outside the repository, or under a
path git ignores. It contains no password, because no profile may — use
``authentication = "windows"`` so nothing is typed, or run this interactively
and answer the hidden prompt.

Nothing here is ever run by an agent, and nothing here runs in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from migration_reconciliation.execution.connections import (
    SectionExecutors,
    resolve_section_settings,
)
from migration_reconciliation.execution.engine import execute_plan, new_run_id
from migration_reconciliation.execution.plan import build_plan
from migration_reconciliation.profile import load_profile

ALLOW_FLAG = "MIGRATION_RECONCILIATION_ALLOW_DB_TESTS"
PROFILE_VARIABLE = "MIGRATION_RECONCILIATION_PROFILE"
WORKBOOK_VARIABLE = "MIGRATION_RECONCILIATION_WORKBOOK"

pytestmark = pytest.mark.skipif(
    os.environ.get(ALLOW_FLAG) != "1",
    reason=(
        f"Database integration tests are opt-in: set {ALLOW_FLAG}=1 only with explicit "
        "approval and read-only accounts."
    ),
)


def _configured(variable: str) -> Path:
    raw = os.environ.get(variable, "").strip()
    if not raw:
        pytest.skip(f"{variable} is not set.")
    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(f"{variable} points at '{path}', which is not a file.")
    return path


def test_a_local_workbook_runs_against_a_local_sql_server() -> None:
    """End to end, non-interactively, against whatever the profile names."""
    profile = load_profile(_configured(PROFILE_VARIABLE))
    plan = build_plan(_configured(WORKBOOK_VARIABLE))

    assert not plan.problems, [p.message for p in plan.problems]

    settings = resolve_section_settings(profile, plan.required_sections, interactive=False)
    executors = SectionExecutors(settings)
    report = execute_plan(plan, executors=executors, run_id=new_run_id(), write_output=True)

    assert report.output_path is not None
    assert report.enabled_tests == len(plan.executable)
    # A local run is a smoke test of the plumbing, not of the client's data, so
    # the assertion is that every test reached a verdict — not which verdict.
    assert report.not_executed == 0
