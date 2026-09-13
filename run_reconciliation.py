#!/usr/bin/env python3
"""Run reconciliation test cases from an Excel workbook against two databases.

Start it with no arguments::

    uv run python run_reconciliation.py

It asks fifteen questions, one at a time — workbook, sheet, then the source and
target connection details — and only then opens a connection, reads the sheet
or runs a query. There is no flag for a server, a database, a username or a
password: connection details are typed in, used for the life of the run, and
never stored. Passwords are read with :func:`getpass.getpass`, never echoed,
never logged, and never written to the result workbook.

Three optional flags exist, all of them execution modifiers rather than
information the script needs to run:

``--profile FILE``   a TOML file that pre-answers the questions (never a password)
``--case ID``        run only this test case; repeatable
``--limit N``        run at most N cases, for a pilot
``--output-dir DIR`` where to put the result workbook

Exit codes match the ``reconcile`` command: 0 clean, 1 failures or errors,
2 could not start, 130 interrupted.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from migration_reconciliation.database.base import QuerySide
from migration_reconciliation.database.live import LiveExecutorFactory
from migration_reconciliation.errors import ReconciliationError
from migration_reconciliation.models import WorkbookSchema
from migration_reconciliation.profile import RunProfile, load_profile
from migration_reconciliation.reporting import (
    make_output_encoding_safe,
    render_identity,
    render_summary,
)
from migration_reconciliation.runner import ReconciliationRunner, RunOptions
from migration_reconciliation.security.redaction import sanitize_error
from migration_reconciliation.wizard import WizardAnswers, run_wizard
from migration_reconciliation.workbook.inline import (
    OPTIONAL_INPUT_HEADERS,
    REQUIRED_INPUT_HEADERS,
    RESULT_HEADERS,
    build_inline_schema,
)
from migration_reconciliation.workbook.reader import validate_workbook

PROGRAM = "run_reconciliation.py"

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Run workbook reconciliation against a source and a target database. "
            "Connection details are asked for interactively; there are no flags for them."
        ),
        epilog="Passwords are never accepted as arguments and never stored.",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        metavar="PATH",
        help=(
            "TOML file answering any of the connection questions. It never contains a "
            "password: one is still prompted for, unless Windows authentication is used."
        ),
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        dest="cases",
        metavar="ID",
        help="Run only this test case id. Repeatable.",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="Run at most N test cases (pilot run)."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        metavar="PATH",
        help="Directory for the result workbook (default: next to the workbook).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    make_output_encoding_safe()
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except ReconciliationError as exc:
        _error(str(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("")
        _error("Interrupted. Nothing was written.")
        return EXIT_INTERRUPTED
    except Exception as exc:  # never let a raw driver message reach the console
        _error(sanitize_error(exc))
        return EXIT_USAGE


def _run(args: argparse.Namespace) -> int:
    _banner()
    profile = RunProfile.empty()
    if args.profile is not None:
        profile = load_profile(args.profile)
        _emit(f"  Profile          : {args.profile}")
    answers = run_wizard(profile=profile)
    schema = build_inline_schema(answers.sheet_name, answers.source.database_type)

    factory = LiveExecutorFactory(answers.source, answers.target)
    try:
        _emit("")
        _emit("Connecting...")
        identities = factory.connect_all()
        for side in (QuerySide.SOURCE, QuerySide.TARGET):
            _emit(render_identity(identities[side]))

        case_count = _preflight(answers, schema)
        if case_count == 0:
            _error("No enabled test cases to run.")
            return EXIT_USAGE

        _emit("")
        _emit("Executing...")
        options = RunOptions(
            case_ids=tuple(args.cases),
            limit=args.limit,
            output_dir=args.output_dir,
            write_output=True,
        )
        summary = ReconciliationRunner(schema, factory).run(answers.workbook_path, options)
    finally:
        factory.close_all()

    note = "  No result workbook written."
    for line in render_summary(summary, no_output_note=note):
        _emit(line)
    return EXIT_OK if summary.is_clean else EXIT_FAILURES


def _preflight(answers: WizardAnswers, schema: WorkbookSchema) -> int:
    """Read the sheet and report what was found, before any query runs.

    Reading is cheap and catches a mistyped sheet, a missing ``Source SQL``
    column or a duplicate id while the person is still watching — rather than
    after two hundred queries have already gone to the database.
    """
    read = validate_workbook(answers.workbook_path, schema)

    _emit("")
    _emit(f"Sheet '{answers.sheet_name}':")
    _emit(f"  enabled cases  : {len(read.test_cases)}")
    _emit(f"  disabled cases : {len(read.skipped)}")
    _emit(f"  invalid rows   : {len(read.invalid)}")
    for invalid in read.invalid:
        _emit(f"    row {invalid.row_number} [{invalid.test_case_id or '?'}]: {invalid.message}")

    found_optional = [
        header for name, header in OPTIONAL_INPUT_HEADERS.items() if name in read.column_map
    ]
    if found_optional:
        _emit(f"  optional columns used: {', '.join(found_optional)}")

    missing_results = [
        header for name, header in RESULT_HEADERS.items() if name not in read.column_map
    ]
    if len(missing_results) == len(RESULT_HEADERS):
        raise ReconciliationError(
            f"Sheet '{answers.sheet_name}' has none of the result columns "
            f"({', '.join(RESULT_HEADERS.values())}), so there is nowhere to write the outcome."
        )
    if missing_results:
        _emit(f"  result columns missing (will not be written): {', '.join(missing_results)}")
    return len(read.test_cases)


def _banner() -> None:
    required = ", ".join(REQUIRED_INPUT_HEADERS.values())
    results = ", ".join(RESULT_HEADERS.values())
    _emit("Migration reconciliation")
    _emit("=" * 72)
    _emit(f"  Required columns : {required}")
    _emit(f"  Result columns   : {results}")
    _emit("  Nothing is connected to until every question has been answered.")
    _emit("  Passwords are hidden, kept in memory for this run only, and never saved.")


def _emit(message: str) -> None:
    print(message)


def _error(message: str) -> None:
    print(f"{PROGRAM}: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
