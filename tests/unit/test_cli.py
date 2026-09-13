"""CLI behaviour and exit codes."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

from migration_reconciliation.cli import EXIT_FAILURES, EXIT_OK, EXIT_USAGE, main
from tests.conftest import EXAMPLE_FIXTURE_PATH, EXAMPLE_SCHEMA_PATH, EXAMPLE_TEMPLATE_PATH


@pytest.fixture
def fixture_file(tmp_path: Path) -> Any:
    """Write a scripted-results file for generated workbooks."""

    def _write(body: str, name: str = "fake.toml") -> Path:
        path = tmp_path / name
        path.write_text(f'version = "1.0"\n{body}', encoding="utf-8")
        return path

    return _write


def test_validate_schema_accepts_the_shipped_example(capsys: Any) -> None:
    code = main(["validate-schema", "--schema", str(EXAMPLE_SCHEMA_PATH)])

    assert code == EXIT_OK
    assert "Schema OK" in capsys.readouterr().out


def test_validate_schema_reports_a_bad_schema(tmp_path: Path, capsys: Any) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('schema_version = "9.9"\n', encoding="utf-8")

    code = main(["validate-schema", "--schema", str(bad)])

    assert code == EXIT_USAGE
    assert "unsupported schema_version" in capsys.readouterr().err


def test_validate_template_accepts_the_shipped_template(capsys: Any) -> None:
    code = main(
        [
            "validate-template",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
        ]
    )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "Workbook OK" in out
    assert "enabled cases  : 8" in out


def test_validate_template_never_needs_scripted_results(
    make_workbook: Any, case_row: Any, capsys: Any
) -> None:
    path = make_workbook([case_row("TC-001")])

    code = main(["validate-template", str(path), "--schema", str(EXAMPLE_SCHEMA_PATH)])

    assert code == EXIT_OK
    assert "Workbook OK" in capsys.readouterr().out


def test_validate_template_flags_invalid_rows(
    make_workbook: Any, case_row: Any, capsys: Any
) -> None:
    path = make_workbook([case_row("TC-BAD", source_type="postgres")])

    code = main(["validate-template", str(path), "--schema", str(EXAMPLE_SCHEMA_PATH)])

    assert code == EXIT_FAILURES
    assert "unsupported value 'postgres'" in capsys.readouterr().out


def test_validate_template_reports_a_missing_workbook(tmp_path: Path, capsys: Any) -> None:
    code = main(
        ["validate-template", str(tmp_path / "gone.xlsx"), "--schema", str(EXAMPLE_SCHEMA_PATH)]
    )

    assert code == EXIT_USAGE
    assert "Workbook not found" in capsys.readouterr().err


def test_execute_demonstration_run_exits_non_zero_on_failures(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "execute",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--fake-results",
            str(EXAMPLE_FIXTURE_PATH),
            "--output-dir",
            str(tmp_path),
        ]
    )

    out = capsys.readouterr().out
    assert code == EXIT_FAILURES
    assert "4 passed, 2 failed, 2 errors, 1 skipped" in out
    assert len(list(tmp_path.glob("*_results_*.xlsx"))) == 1


def test_execute_exits_zero_when_everything_passes(
    make_workbook: Any, case_row: Any, fixture_file: Any, tmp_path: Path, capsys: Any
) -> None:
    path = make_workbook([case_row("TC-001")])
    fake = fixture_file('[[case]]\ntest_case_id = "TC-001"\nsource_result = 5\ntarget_result = 5\n')

    code = main(
        [
            "execute",
            str(path),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--fake-results",
            str(fake),
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert code == EXIT_OK
    assert "1 passed, 0 failed" in capsys.readouterr().out


def test_execute_case_selects_one_test(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "execute",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--case",
            "TC-PAY-008",
            "--fake-results",
            str(EXAMPLE_FIXTURE_PATH),
            "--output-dir",
            str(tmp_path),
        ]
    )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "PASS    TC-PAY-008" in out
    assert "1 passed, 0 failed, 0 errors, 8 skipped" in out


def test_execute_case_is_repeatable(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "execute",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--case",
            "TC-PAY-001",
            "--case",
            "TC-PAY-008",
            "--fake-results",
            str(EXAMPLE_FIXTURE_PATH),
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert code == EXIT_OK
    assert "2 passed" in capsys.readouterr().out


def test_execute_limit_runs_a_pilot(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "execute",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--limit",
            "2",
            "--fake-results",
            str(EXAMPLE_FIXTURE_PATH),
            "--output-dir",
            str(tmp_path),
        ]
    )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "2 passed, 0 failed, 0 errors, 7 skipped" in out


def test_execute_fail_fast_stops_early(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "execute",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--fail-fast",
            "--fake-results",
            str(EXAMPLE_FIXTURE_PATH),
            "--output-dir",
            str(tmp_path),
        ]
    )

    out = capsys.readouterr().out
    assert code == EXIT_FAILURES
    assert "Stopped by --fail-fast" in out


def test_execute_reports_an_unknown_case(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "execute",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--case",
            "TC-NOPE",
            "--fake-results",
            str(EXAMPLE_FIXTURE_PATH),
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert code == EXIT_USAGE
    assert "No enabled test case matches" in capsys.readouterr().err


def test_execute_requires_scripted_results() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["execute", str(EXAMPLE_TEMPLATE_PATH), "--schema", str(EXAMPLE_SCHEMA_PATH)])

    assert excinfo.value.code == EXIT_USAGE


def test_execute_leaves_the_input_workbook_untouched(tmp_path: Path) -> None:
    before = EXAMPLE_TEMPLATE_PATH.read_bytes()

    main(
        [
            "execute",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--fake-results",
            str(EXAMPLE_FIXTURE_PATH),
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert EXAMPLE_TEMPLATE_PATH.read_bytes() == before


def test_test_connections_is_an_explicit_offline_placeholder(capsys: Any) -> None:
    code = main(
        ["test-connections", str(EXAMPLE_TEMPLATE_PATH), "--schema", str(EXAMPLE_SCHEMA_PATH)]
    )

    err = capsys.readouterr().err
    assert code == EXIT_USAGE
    assert "not available in offline mode" in err


def test_make_template_generates_a_matching_workbook(tmp_path: Path, capsys: Any) -> None:
    destination = tmp_path / "generated.xlsx"

    code = main(["make-template", str(destination), "--schema", str(EXAMPLE_SCHEMA_PATH)])

    assert code == EXIT_OK
    assert destination.exists()
    assert main(["validate-template", str(destination), "--schema", str(EXAMPLE_SCHEMA_PATH)]) == (
        EXIT_OK
    )
    capsys.readouterr()


def test_make_template_refuses_to_clobber_without_force(tmp_path: Path, capsys: Any) -> None:
    destination = tmp_path / "generated.xlsx"
    destination.write_bytes(b"existing")

    code = main(["make-template", str(destination), "--schema", str(EXAMPLE_SCHEMA_PATH)])

    assert code == EXIT_USAGE
    assert "Use --force" in capsys.readouterr().err
    assert destination.read_bytes() == b"existing"


def test_no_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == EXIT_USAGE


def test_the_cli_offers_no_password_option() -> None:
    """A credential must never be passable on a command line."""
    from migration_reconciliation.cli import build_parser

    help_text = build_parser().format_help()
    for command in ("execute", "validate-template", "test-connections"):
        assert command in help_text
    assert "--password" not in help_text
    assert "--pwd" not in help_text


def test_execute_survives_a_legacy_console_code_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows console code page must never turn a finished run into a traceback.

    Redirected output on Windows is encoded with the active code page, and a
    legacy one such as cp437 cannot represent every character a message may
    carry. The report still has to arrive.
    """
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp437", newline="")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp437", newline="")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    code = main(
        [
            "execute",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--fake-results",
            str(EXAMPLE_FIXTURE_PATH),
            "--output-dir",
            str(tmp_path),
        ]
    )
    stdout.flush()
    out = stdout.buffer.getvalue().decode("cp437")  # type: ignore[attr-defined]

    assert code == EXIT_FAILURES
    assert "4 passed, 2 failed, 2 errors, 1 skipped" in out


def test_run_report_stays_ascii(tmp_path: Path, capsys: Any) -> None:
    """The framework's own report text is plain ASCII, printable under any code page."""
    main(
        [
            "execute",
            str(EXAMPLE_TEMPLATE_PATH),
            "--schema",
            str(EXAMPLE_SCHEMA_PATH),
            "--fake-results",
            str(EXAMPLE_FIXTURE_PATH),
            "--output-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    (captured.out + captured.err).encode("ascii")
