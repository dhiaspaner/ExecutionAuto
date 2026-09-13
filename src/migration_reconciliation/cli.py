"""Command line interface.

Exit codes are part of the contract, so the CLI can gate a pipeline:

===== ===========================================================
Code  Meaning
===== ===========================================================
0     Everything passed (or validation succeeded).
1     The run completed but at least one case FAILED or ERRORED.
2     The run could not start: bad arguments, schema or workbook.
130   Interrupted with Ctrl+C.
===== ===========================================================

No command in this milestone opens a database connection, and no command
accepts a credential. ``--password`` does not exist and must never be added.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path

from .database.factory import create_executor_factory, real_adapters_available
from .database.fake import load_fake_results
from .errors import ReconciliationError
from .models import RunSummary, WorkbookSchema
from .runner import ReconciliationRunner, RunOptions
from .security.redaction import sanitize_error
from .workbook.reader import validate_workbook
from .workbook.schema import load_schema
from .workbook.template import write_template

__all__ = ["EXIT_FAILURES", "EXIT_OK", "EXIT_USAGE", "build_parser", "main"]

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

PROGRAM = "reconcile"
VERSION = "0.1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Workbook-driven data-migration reconciliation. "
            "Milestone 1 runs offline against scripted results only."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema_parent = argparse.ArgumentParser(add_help=False)
    schema_parent.add_argument(
        "--schema",
        required=True,
        type=Path,
        metavar="PATH",
        help="Workbook schema DSL (.toml) that maps semantic fields to Excel headers.",
    )

    validate_schema = subparsers.add_parser(
        "validate-schema",
        parents=[schema_parent],
        help="Validate a schema DSL. Opens no workbook and no database.",
    )
    validate_schema.set_defaults(handler=_cmd_validate_schema)

    validate_template = subparsers.add_parser(
        "validate-template",
        parents=[schema_parent],
        help="Validate a workbook against a schema. Connects to no database.",
    )
    validate_template.add_argument("workbook", type=Path, help="Workbook to validate.")
    validate_template.set_defaults(handler=_cmd_validate_template)

    execute = subparsers.add_parser(
        "execute",
        parents=[schema_parent],
        help="Execute test cases and write a new timestamped result workbook.",
    )
    execute.add_argument("workbook", type=Path, help="Workbook containing the test cases.")
    execute.add_argument(
        "--fake-results",
        required=True,
        type=Path,
        metavar="PATH",
        help="Scripted offline results (.toml). Required: no real adapter exists yet.",
    )
    execute.add_argument(
        "--case",
        action="append",
        default=[],
        dest="cases",
        metavar="ID",
        help="Run only this test case id. Repeatable.",
    )
    execute.add_argument(
        "--limit", type=int, metavar="N", help="Run at most N test cases (pilot run)."
    )
    execute.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failure or error instead of running every case.",
    )
    execute.add_argument(
        "--output-dir",
        type=Path,
        metavar="PATH",
        help="Directory for the result workbook (default: next to the input).",
    )
    execute.add_argument(
        "--no-write",
        action="store_true",
        help="Run and report without producing a result workbook.",
    )
    execute.set_defaults(handler=_cmd_execute)

    test_connections = subparsers.add_parser(
        "test-connections",
        parents=[schema_parent],
        help="Not available offline. Reserved for the database milestone.",
    )
    test_connections.add_argument("workbook", type=Path, help="Workbook to inspect.")
    test_connections.set_defaults(handler=_cmd_test_connections)

    make_template = subparsers.add_parser(
        "make-template",
        parents=[schema_parent],
        help="Generate an example workbook whose headers match a schema.",
    )
    make_template.add_argument("output", type=Path, help="Path of the workbook to create.")
    make_template.add_argument(
        "--force", action="store_true", help="Allow overwriting an existing file."
    )
    make_template.set_defaults(handler=_cmd_make_template)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _make_output_encoding_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ReconciliationError as exc:
        _error(str(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        _error("Interrupted.")
        return EXIT_INTERRUPTED
    except OSError as exc:
        _error(sanitize_error(exc))
        return EXIT_USAGE


def _cmd_validate_schema(args: argparse.Namespace) -> int:
    schema = load_schema(args.schema)
    _emit(f"Schema OK: {args.schema}")
    _emit(f"  profile        : {schema.profile_name} (version {schema.schema_version})")
    _emit(f"  sheet          : {schema.sheet_name}")
    _emit(f"  header row     : {schema.header_row}")
    _emit(f"  first data row : {schema.first_data_row}")
    _emit(f"  output pattern : {schema.output_filename_pattern}")
    _emit(
        f"  fields         : {len(schema.fields)} "
        f"({len(schema.readable_fields())} read, {len(schema.writable_fields())} write)"
    )
    return EXIT_OK


def _cmd_validate_template(args: argparse.Namespace) -> int:
    schema = load_schema(args.schema)
    read = validate_workbook(args.workbook, schema)
    _emit(f"Workbook OK: {args.workbook}")
    _emit(f"  schema         : {schema.profile_name} (version {schema.schema_version})")
    _emit(f"  sheet          : {schema.sheet_name}")
    _emit(f"  enabled cases  : {len(read.test_cases)}")
    _emit(f"  disabled cases : {len(read.skipped)}")
    _emit(f"  invalid rows   : {len(read.invalid)}")
    for invalid in read.invalid:
        _emit(f"    row {invalid.row_number} [{invalid.test_case_id or '?'}]: {invalid.message}")
    return EXIT_FAILURES if read.invalid else EXIT_OK


def _cmd_execute(args: argparse.Namespace) -> int:
    schema: WorkbookSchema = load_schema(args.schema)
    book = load_fake_results(args.fake_results)
    factory = create_executor_factory(fake_results=book)

    _emit(f"Offline run - scripted results from {args.fake_results}")
    options = RunOptions(
        case_ids=tuple(args.cases),
        limit=args.limit,
        fail_fast=args.fail_fast,
        output_dir=args.output_dir,
        write_output=not args.no_write,
    )
    summary = ReconciliationRunner(schema, factory).run(args.workbook, options)
    _report(summary)
    return EXIT_OK if summary.is_clean else EXIT_FAILURES


def _cmd_test_connections(args: argparse.Namespace) -> int:
    _error(
        "test-connections is not available in offline mode: no database adapter is "
        "registered in this milestone."
    )
    _error(
        "It will connect using interactive (getpass) or Windows integrated "
        "authentication once adapters are approved and enabled."
    )
    if real_adapters_available():  # pragma: no cover - milestone 2
        _error("Adapters are registered but this command is not implemented yet.")
    return EXIT_USAGE


def _cmd_make_template(args: argparse.Namespace) -> int:
    schema = load_schema(args.schema)
    try:
        path = write_template(args.output, schema, overwrite=args.force)
    except FileExistsError as exc:
        _error(f"{exc} Use --force to replace it.")
        return EXIT_USAGE
    _emit(f"Template written: {path}")
    return EXIT_OK


def _report(summary: RunSummary) -> None:
    _emit("")
    _emit(f"Run {summary.run_id} - {summary.input_path.name}")
    _emit("-" * 72)
    for result in summary.results:
        _emit(
            f"  {result.status.value:<7} {result.test_case_id:<14} "
            f"row {result.row_number:<4} {result.duration_ms:>5} ms  {result.remarks}"
        )
    _emit("-" * 72)
    _emit(
        f"  {summary.passed} passed, {summary.failed} failed, "
        f"{summary.errored} errors, {summary.skipped} skipped "
        f"({summary.executed} executed in {summary.duration_ms} ms)"
    )
    if summary.output_path is not None:
        _emit(f"  Results written to: {summary.output_path}")
        _emit(f"  Original workbook unchanged: {summary.input_path}")
    else:
        _emit("  No result workbook written (--no-write).")
    if not summary.is_clean:
        _emit("")
        _emit("  Review FAIL and ERROR rows above before signing off the migration.")


def _make_output_encoding_safe() -> None:
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


def _emit(message: str) -> None:
    print(message)


def _error(message: str) -> None:
    print(f"{PROGRAM}: {message}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
