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
import sys
from collections.abc import Sequence
from pathlib import Path

from .database.factory import create_executor_factory, real_adapters_available
from .database.fake import load_fake_results
from .errors import ReconciliationError
from .execution.connections import SectionExecutors, resolve_section_settings
from .execution.engine import RunReport, execute_plan, new_run_id
from .execution.plan import ExecutionPlan, build_plan
from .models import RunMode, RunSummary, WorkbookSchema
from .profile import RunProfile, load_profile
from .reporting import make_output_encoding_safe, render_run_report, render_summary
from .runner import ReconciliationRunner, RunOptions
from .security.redaction import sanitize_error
from .wizard import Prompter
from .workbook.columns import TEST_CASES_SHEET
from .workbook.payments_template import write_workbook
from .workbook.reader import validate_workbook
from .workbook.schema import load_schema
from .workbook.template import write_template

__all__ = ["EXIT_FAILURES", "EXIT_OK", "EXIT_USAGE", "build_parser", "main"]

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

PROGRAM = "reconcile"
VERSION = "0.2.0"


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
        "--start-row",
        type=int,
        metavar="N",
        help=(
            "Skip enabled rows before workbook row N (Excel's own row numbers). "
            "Combines with --end-row to run one slice of a large sheet; applied "
            "after --case."
        ),
    )
    execute.add_argument(
        "--end-row",
        type=int,
        metavar="N",
        help="Skip enabled rows after workbook row N (inclusive). See --start-row.",
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

    run = subparsers.add_parser(
        "run",
        help=("Run a reconciliation workbook against the databases named in a TOML profile."),
    )
    run.add_argument(
        "--profile",
        required=True,
        type=Path,
        metavar="PATH",
        help=(
            "TOML profile holding the workbook location and the database connections. "
            "It never contains a password."
        ),
    )
    run.add_argument(
        "--workbook",
        type=Path,
        metavar="PATH",
        help="Workbook to run, overriding [workbook] path in the profile.",
    )
    run.add_argument(
        "--sheet",
        metavar="NAME",
        help=f"Test-case sheet, overriding [workbook] sheet (default: {TEST_CASES_SHEET}).",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate everything and open nothing. Runs no reconciliation SQL.",
    )
    run.add_argument(
        "--mode",
        choices=[mode.value for mode in RunMode],
        default=RunMode.EXECUTE.value,
        help=(
            "'validate' connects and asks the database to compile every query, writes the "
            "validation sheets and stops without executing anything. 'execute' (the default) "
            "runs that same check first and only executes when every query compiled. "
            "Unlike --dry-run, 'validate' does open the databases."
        ),
    )
    run.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Never prompt. Every required value must be in the profile, and every "
            'connection must use authentication = "windows".'
        ),
    )
    run.add_argument(
        "--output-dir",
        type=Path,
        metavar="PATH",
        help="Directory for the result workbook (default: next to the input).",
    )
    run.add_argument(
        "--no-write",
        action="store_true",
        help="Run and report without producing a result workbook.",
    )
    run.set_defaults(handler=_cmd_run)

    make_workbook = subparsers.add_parser(
        "make-workbook",
        help="Generate a reconciliation workbook matching this executor's template.",
    )
    make_workbook.add_argument("output", type=Path, help="Path of the workbook to create.")
    make_workbook.add_argument(
        "--force", action="store_true", help="Allow overwriting an existing file."
    )
    make_workbook.set_defaults(handler=_cmd_make_workbook)

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
    make_output_encoding_safe()
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
        start_row=args.start_row,
        end_row=args.end_row,
        limit=args.limit,
        fail_fast=args.fail_fast,
        output_dir=args.output_dir,
        write_output=not args.no_write,
    )
    summary = ReconciliationRunner(schema, factory).run(args.workbook, options)
    _report(summary)
    return EXIT_OK if summary.is_clean else EXIT_FAILURES


def _cmd_run(args: argparse.Namespace) -> int:
    """The reconciliation-template workflow, in the order the contract fixes.

    Nothing is opened until every test definition has been validated and the
    set of required connections is known, so a misconfigured workbook costs a
    second rather than a round of failed logins.
    """
    profile = load_profile(args.profile)
    for warning in profile.warnings:
        _emit(f"  warning: {warning}")

    prompter = Prompter()
    workbook_path = _workbook_path(args, profile, prompter)
    sheet_name = args.sheet or profile.sheet_name or TEST_CASES_SHEET

    plan = build_plan(workbook_path, sheet_name=sheet_name)
    if args.dry_run:
        plan = ExecutionPlan(
            workbook_path=plan.workbook_path,
            sheet=plan.sheet,
            control=plan.control.with_overrides(dry_run=True),
            rules=plan.rules,
            warnings=plan.warnings,
            ignored_sheets=plan.ignored_sheets,
        )
    _report_plan(plan, workbook_path, sheet_name)

    executors: SectionExecutors | None = None
    if not plan.control.dry_run and plan.executable:
        sections = plan.required_sections
        if not sections:
            raise ReconciliationError(
                "No enabled test names a profile section, so there is nothing to connect to."
            )
        settings = resolve_section_settings(
            profile,
            sections,
            prompter=prompter,
            interactive=not args.non_interactive,
        )
        executors = SectionExecutors(settings)
        _emit("")
        _emit("Connecting...")
        for section in sections:
            _emit(f"  [{section}] {executors.describe(section)}")

    report = execute_plan(
        plan,
        executors=executors,
        run_id=new_run_id(),
        output_dir=args.output_dir,
        write_output=not args.no_write,
        on_progress=_emit,
        mode=RunMode(args.mode),
    )
    _report_run(report)
    if report.dry_run:
        # A dry run reports what a real run would do. Its own success is
        # whether validation found anything, not whether tests would pass.
        return EXIT_FAILURES if plan.problems else EXIT_OK
    return EXIT_OK if report.is_clean else EXIT_FAILURES


def _workbook_path(args: argparse.Namespace, profile: RunProfile, prompter: Prompter) -> Path:
    """The workbook to run: the flag, then the profile, then a question.

    A ``--workbook`` that is not there is a hard error: the person named that
    exact file. A *profile* path that is not there is not — a shared profile
    outliving a moved workbook is ordinary, so it warns and asks instead, which
    is what the interactive runner has always done.
    """
    if args.workbook is not None:
        path = Path(args.workbook).expanduser()
        if not path.is_file():
            raise ReconciliationError(f"Workbook not found: {path}")
        return path

    if profile.workbook_path is not None:
        path = Path(profile.workbook_path).expanduser()
        if path.is_file():
            return path
        if args.non_interactive:
            raise ReconciliationError(
                f"Workbook not found: {path}. Fix [workbook] path in "
                f"'{profile.source_path}', or pass --workbook."
            )
        _emit(
            f"  warning: [workbook] path in '{profile.source_path}' is '{path}', "
            f"which is not there. Asking instead."
        )
    elif args.non_interactive:
        raise ReconciliationError(
            "No workbook was given. Set [workbook] path in the profile or pass --workbook."
        )

    prompter.set_total(1)
    return prompter.existing_workbook("Path to the workbook (.xlsx)")


def _report_plan(plan: ExecutionPlan, workbook_path: Path, sheet_name: str) -> None:
    _emit(f"Workbook : {workbook_path}")
    _emit(f"Sheet    : {plan.sheet.sheet_name} (headers on row {plan.sheet.header_row})")
    _emit(
        f"  enabled tests : {len(plan.executable)}   "
        f"disabled: {len(plan.disabled)}   config errors: {len(plan.problems)}"
    )
    _emit(f"  connections   : {', '.join(plan.required_sections) or 'none required'}")
    _emit(
        f"  timeout       : {_describe_timeout(plan.control.query_timeout_seconds)}   "
        f"output mode: {plan.control.output_mode.value}"
        f"{'   DRY RUN' if plan.control.dry_run else ''}"
    )
    for warning in plan.warnings:
        _emit(f"  warning: {warning}")
    for problem in plan.problems:
        _emit(
            f"  CONFIG ERROR row {problem.row_number} "
            f"[{problem.test_id or '?'}] {problem.code.value}: {problem.message}"
        )


def _report_run(report: RunReport) -> None:
    note = (
        "  Dry run: no result workbook was written and no results were cleared."
        if report.dry_run
        else "  No result workbook written (--no-write)."
    )
    for line in render_run_report(report, no_output_note=note):
        _emit(line)


def _cmd_make_workbook(args: argparse.Namespace) -> int:
    try:
        path = write_workbook(args.output, overwrite=args.force)
    except FileExistsError as exc:
        _error(f"{exc} Use --force to replace it.")
        return EXIT_USAGE
    _emit(f"Workbook written: {path}")
    return EXIT_OK


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
    note = "  No result workbook written (--no-write)."
    for line in render_summary(summary, no_output_note=note):
        _emit(line)


def _describe_timeout(seconds: int) -> str:
    """Render the query timeout, including the case where there is not one."""
    if seconds <= 0:
        return "no query timeout"
    return f"{seconds}s per query"


def _emit(message: str) -> None:
    print(message)


def _error(message: str) -> None:
    print(f"{PROGRAM}: {message}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
