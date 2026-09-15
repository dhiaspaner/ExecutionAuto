"""The ``reconcile run`` command: what it validates before it opens anything."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from migration_reconciliation.cli import EXIT_FAILURES, EXIT_OK, EXIT_USAGE, main

WINDOWS_PROFILE = """
version = "1.0"

[workbook]
sheet = "Test Cases"

[source]
type = "sqlserver"
server = "localhost"
port = 1433
authentication = "windows"
trust_server_certificate = true
database = "webservice"

[target]
type = "sqlserver"
server = "localhost"
port = 1433
database = "PaymentRecon_Target_Local"
trust_server_certificate = true
authentication = "windows"
"""


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    path = tmp_path / "run_profile.toml"
    path.write_text(WINDOWS_PROFILE, encoding="utf-8")
    return path


def test_a_dry_run_validates_without_touching_a_database(
    profile_path: Path, make_recon_workbook: Any, test_row: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    workbook = make_recon_workbook([test_row("TC-001")])

    code = main(["run", "--profile", str(profile_path), "--workbook", str(workbook), "--dry-run"])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "DRY RUN" in out
    assert "enabled tests : 1" in out
    assert not list(workbook.parent.glob("*_results_*.xlsx"))


def test_a_dry_run_fails_when_a_test_is_misconfigured(
    profile_path: Path, make_recon_workbook: Any, test_row: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    workbook = make_recon_workbook(
        [test_row("TC-001", Comparison_Type="ROUGHLY_EQUAL")], name="broken.xlsx"
    )

    code = main(["run", "--profile", str(profile_path), "--workbook", str(workbook), "--dry-run"])

    out = capsys.readouterr().out
    assert code == EXIT_FAILURES
    assert "CONFIG ERROR" in out
    assert "INVALID_COMPARISON_TYPE" in out


def test_the_documentation_sheets_are_reported_as_ignored(
    profile_path: Path, make_recon_workbook: Any, test_row: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    workbook = make_recon_workbook([test_row("TC-001")])

    main(["run", "--profile", str(profile_path), "--workbook", str(workbook), "--dry-run"])

    out = capsys.readouterr().out
    assert "Executor Contract" in out
    assert "never parsed for instructions" in out


def test_a_profile_holding_a_password_is_refused_before_anything_opens(
    tmp_path: Path, make_recon_workbook: Any, test_row: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    leaky = tmp_path / "leaky.toml"
    leaky.write_text(WINDOWS_PROFILE + '\npassword = "hunter2"\n', encoding="utf-8")
    workbook = make_recon_workbook([test_row("TC-001")])

    code = main(["run", "--profile", str(leaky), "--workbook", str(workbook), "--dry-run"])

    assert code == EXIT_USAGE
    assert "never contain a password" in capsys.readouterr().err


def test_a_workbook_that_is_not_there_is_reported_plainly(
    profile_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "run",
            "--profile",
            str(profile_path),
            "--workbook",
            str(tmp_path / "nope.xlsx"),
            "--dry-run",
        ]
    )

    assert code == EXIT_USAGE
    assert "Workbook not found" in capsys.readouterr().err


def test_a_non_interactive_run_without_a_workbook_says_so(
    profile_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["run", "--profile", str(profile_path), "--non-interactive", "--dry-run"])

    assert code == EXIT_USAGE
    assert "--workbook" in capsys.readouterr().err


def test_a_sheet_that_is_not_a_test_case_sheet_is_refused(
    profile_path: Path, make_recon_workbook: Any, test_row: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    workbook = make_recon_workbook([test_row("TC-001")])

    code = main(
        [
            "run",
            "--profile",
            str(profile_path),
            "--workbook",
            str(workbook),
            "--sheet",
            "Run History",
            "--dry-run",
        ]
    )

    assert code == EXIT_USAGE
    assert "Test_ID" in capsys.readouterr().err  # --sheet was honoured, then rejected


def test_make_workbook_produces_a_workbook_this_executor_can_run(
    tmp_path: Path, profile_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "Payments.xlsx"

    assert main(["make-workbook", str(destination)]) == EXIT_OK
    capsys.readouterr()

    code = main(
        ["run", "--profile", str(profile_path), "--workbook", str(destination), "--dry-run"]
    )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "config errors: 0" in out


def test_make_workbook_refuses_to_clobber_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "Payments.xlsx"
    main(["make-workbook", str(destination)])
    capsys.readouterr()

    assert main(["make-workbook", str(destination)]) == EXIT_USAGE
    assert "--force" in capsys.readouterr().err
    assert main(["make-workbook", str(destination), "--force"]) == EXIT_OK


def test_a_target_type_inherited_from_the_source_is_announced(
    tmp_path: Path, make_recon_workbook: Any, test_row: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    inheriting = tmp_path / "inheriting.toml"
    inheriting.write_text(
        WINDOWS_PROFILE.replace('[target]\ntype = "sqlserver"\n', "[target]\n"),
        encoding="utf-8",
    )
    workbook = make_recon_workbook([test_row("TC-001")])

    main(["run", "--profile", str(inheriting), "--workbook", str(workbook), "--dry-run"])

    assert 'no "type"' in capsys.readouterr().out


def test_a_profile_path_that_moved_is_asked_about_rather_than_fatal(
    tmp_path: Path,
    make_recon_workbook: Any,
    test_row: Any,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared profile outliving a moved workbook is ordinary, not an error."""
    stale = tmp_path / "stale.toml"
    stale.write_text(
        WINDOWS_PROFILE.replace(
            '[workbook]\nsheet = "Test Cases"',
            '[workbook]\npath = "C:/nowhere/Payments.xlsx"\nsheet = "Test Cases"',
        ),
        encoding="utf-8",
    )
    workbook = make_recon_workbook([test_row("TC-001")])
    monkeypatch.setattr("builtins.input", lambda _prompt: str(workbook))

    code = main(["run", "--profile", str(stale), "--dry-run"])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "which is not there" in out
    assert "enabled tests : 1" in out


def test_a_profile_path_that_moved_is_fatal_when_nothing_can_be_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "stale.toml"
    stale.write_text(
        WINDOWS_PROFILE.replace(
            '[workbook]\nsheet = "Test Cases"',
            '[workbook]\npath = "C:/nowhere/Payments.xlsx"\nsheet = "Test Cases"',
        ),
        encoding="utf-8",
    )

    code = main(["run", "--profile", str(stale), "--non-interactive", "--dry-run"])

    assert code == EXIT_USAGE
    assert "--workbook" in capsys.readouterr().err


def test_an_explicit_workbook_that_is_missing_is_never_second_guessed(
    profile_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["run", "--profile", str(profile_path), "--workbook", str(tmp_path / "typo.xlsx")])

    assert code == EXIT_USAGE
    assert "Workbook not found" in capsys.readouterr().err


def test_a_dry_run_reports_validation_not_a_run(
    profile_path: Path, make_recon_workbook: Any, test_row: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two hundred identical "not executed" lines would bury the real findings."""
    workbook = make_recon_workbook([test_row(f"TC-{n:03d}") for n in range(1, 6)])

    main(["run", "--profile", str(profile_path), "--workbook", str(workbook), "--dry-run"])

    out = capsys.readouterr().out
    assert "5 test(s) validated and ready to run" in out
    assert "no database was opened" in out
    assert "NOT EXECUTED" not in out
    assert "Overall:" not in out


def test_a_dry_run_lists_only_the_rows_with_something_wrong(
    profile_path: Path, make_recon_workbook: Any, test_row: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    workbook = make_recon_workbook(
        [test_row("TC-001"), test_row("TC-BAD", Result_Type="MONEY")], name="mixed.xlsx"
    )

    main(["run", "--profile", str(profile_path), "--workbook", str(workbook), "--dry-run"])

    out = capsys.readouterr().out
    assert "1 misconfigured" in out
    assert "TC-BAD" in out
    assert "Fix the rows above" in out
