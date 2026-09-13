"""The interactive wizard: order, validation loops and secret handling.

Every test drives the wizard from a scripted list of answers, so the whole
fifteen-question flow is exercised without a terminal, a workbook or a
database.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from migration_reconciliation.database.settings import ConnectionSettings
from migration_reconciliation.errors import ReconciliationError, WorkbookError
from migration_reconciliation.models import DatabaseType
from migration_reconciliation.wizard import Prompter, run_wizard

SHEETS = ("Summary", "Payments", "Fines")
SOURCE_SECRET = "src-secret-not-real"
TARGET_SECRET = "tgt-secret-not-real"


class ScriptedConsole:
    """Feeds scripted answers to the wizard and records what it printed."""

    def __init__(self, answers: Sequence[str], secrets: Sequence[str] = ()) -> None:
        self.answers = list(answers)
        self.secrets = list(secrets)
        self.printed: list[str] = []
        self.prompts: list[str] = []
        self.secret_prompts: list[str] = []

    def input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def getpass(self, prompt: str) -> str:
        self.secret_prompts.append(prompt)
        if not self.secrets:
            raise EOFError
        return self.secrets.pop(0)

    def print(self, message: str = "") -> None:
        self.printed.append(message)

    @property
    def transcript(self) -> str:
        return "\n".join(self.printed + self.prompts)

    def prompter(self) -> Prompter:
        return Prompter(input_fn=self.input, getpass_fn=self.getpass, output=self.print)


def sqlserver_answers(**overrides: Any) -> list[str]:
    """A complete, valid set of answers for a SQL Server source."""
    answers = [
        "WORKBOOK",  # path, replaced by the fixture
        "2",  # sheet, by number
        "sqlserver",  # source type
        "sql-legacy.internal",  # source server
        "",  # source port, Enter for the default
        "LegacyDb",  # source database
        "y",  # trust the source certificate
        "password",  # source authentication
        "legacy_reader",  # source username
        "sql-new.internal",  # target server
        "1435",  # target port
        "MigratedDb",  # target database
        "n",  # trust the target certificate
        "password",  # target authentication
        "migrated_reader",  # target username
    ]
    for index, value in overrides.items():
        answers[int(index)] = value
    return answers


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    path = tmp_path / "payments.xlsx"
    path.write_bytes(b"not really a workbook; the sheet list is injected")
    return path


@pytest.fixture
def sheets_of() -> Any:
    def _sheets(_path: Path) -> Sequence[str]:
        return SHEETS

    return _sheets


def drive(console: ScriptedConsole, sheets_of: Any) -> Any:
    return run_wizard(console.prompter(), list_sheets=sheets_of)


# -- the happy path ------------------------------------------------------


def test_all_fifteen_answers_are_collected(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    assert result.workbook_path == workbook
    assert result.sheet_name == "Payments"
    assert result.source.database_type is DatabaseType.SQLSERVER
    assert result.source.server == "sql-legacy.internal"
    assert result.source.port == 1433
    assert result.source.database == "LegacyDb"
    assert result.source.trust_server_certificate is True
    assert result.source.username == "legacy_reader"
    assert result.source.password == SOURCE_SECRET
    assert result.target.database_type is DatabaseType.SQLSERVER
    assert result.target.server == "sql-new.internal"
    assert result.target.port == 1435
    assert result.target.database == "MigratedDb"
    assert result.target.trust_server_certificate is False
    assert result.target.username == "migrated_reader"
    assert result.target.password == TARGET_SECRET


def test_questions_are_asked_in_the_specified_order(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    drive(console, sheets_of)

    asked = [prompt.splitlines()[0] for prompt in console.prompts]
    assert "Path to the workbook" in asked[0]
    assert "Which sheet" in asked[1]
    assert "Source database type" in asked[2]
    assert "Source server" in asked[3]
    assert "Source port" in asked[4]
    assert "Source database name" in asked[5]
    assert "Trust the source server's certificate" in asked[6]
    assert "Source authentication" in asked[7]
    assert "Source username" in asked[8]
    assert "Target server" in asked[9]
    assert "Target port" in asked[10]
    assert "Target database name" in asked[11]
    assert "Trust the target server's certificate" in asked[12]
    assert "Target authentication" in asked[13]
    assert "Target username" in asked[14]
    # getpass gets a bare "> "; the labelled question is printed just above it.
    assert len(console.secret_prompts) == 2
    labels = [line for line in console.printed if "password (input is hidden)" in line]
    assert "Source password" in labels[0]
    assert "Target password" in labels[1]


def test_an_oracle_source_asks_for_a_service_name_and_skips_the_certificate_question(
    workbook: Path, sheets_of: Any
) -> None:
    answers = [
        str(workbook),
        "Fines",
        "oracle",
        "legacy-ora.internal",
        "",  # Enter accepts 1521 for Oracle
        "LEGACYSVC",
        "recon_ro",
        "sql-new.internal",
        "",
        "MigratedDb",
        "y",
        "password",
        "migrated_reader",
    ]
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    assert result.source.database_type is DatabaseType.ORACLE
    assert result.source.port == 1521
    assert result.source.database == "LEGACYSVC"
    assert result.source.trust_server_certificate is False
    assert result.target.port == 1433
    assert "Source service name" in console.transcript
    assert "Trust the source server's certificate" not in console.transcript
    assert "Source authentication" not in console.transcript


# -- validation loops ----------------------------------------------------


def test_an_empty_answer_is_re_asked(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    answers.insert(3, "")  # empty source server, then the real one
    answers.insert(3, "   ")  # whitespace only
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    assert result.source.server == "sql-legacy.internal"
    assert console.transcript.count("A value is required.") == 2


def test_an_invalid_database_type_is_re_asked(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    answers.insert(2, "postgres")
    answers.insert(3, "mysql")
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    assert result.source.database_type is DatabaseType.SQLSERVER
    assert console.transcript.count("Answer must be one of: sqlserver, oracle.") == 2


def test_an_unknown_sheet_name_is_re_asked(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    answers[1] = "Payments"
    answers.insert(1, "Invoices")
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    assert result.sheet_name == "Payments"
    assert "That does not match any sheet in this workbook." in console.transcript


def test_a_sheet_number_out_of_range_is_re_asked(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    answers.insert(1, "9")
    answers.insert(2, "0")
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    assert result.sheet_name == "Payments"
    assert console.transcript.count("Choose a number between 1 and 3.") == 2


def test_a_sheet_can_be_chosen_by_name(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    answers[1] = "Fines"
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    assert drive(console, sheets_of).sheet_name == "Fines"


def test_the_sheet_list_is_shown_before_the_question(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    drive(console, sheets_of)

    assert "      1. Summary" in console.printed
    assert "      2. Payments" in console.printed
    assert "      3. Fines" in console.printed


def test_a_missing_workbook_is_re_asked(tmp_path: Path, workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    answers.insert(0, str(tmp_path / "nope.xlsx"))
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    assert result.workbook_path == workbook
    assert "No such file" in console.transcript


def test_a_non_xlsx_file_is_re_asked(tmp_path: Path, workbook: Path, sheets_of: Any) -> None:
    csv = tmp_path / "cases.csv"
    csv.write_text("id,sql\n", encoding="utf-8")
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    answers.insert(0, str(csv))
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    drive(console, sheets_of)

    assert "The workbook must be an .xlsx file." in console.transcript


def test_a_quoted_windows_path_is_accepted(workbook: Path, sheets_of: Any) -> None:
    """Windows Explorer's 'Copy as path' wraps the path in double quotes."""
    answers = sqlserver_answers()
    answers[0] = f'"{workbook}"'
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    assert drive(console, sheets_of).workbook_path == workbook


def test_an_invalid_port_is_re_asked(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    answers[4] = "1433"
    answers.insert(4, "70000")
    answers.insert(4, "not-a-port")
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    assert result.source.port == 1433
    assert console.transcript.count("Enter a number between 1 and 65535") == 2


def test_an_invalid_yes_no_answer_is_re_asked(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    answers.insert(6, "maybe")
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    drive(console, sheets_of)

    assert "Answer y or n." in console.transcript


def test_an_empty_password_is_re_asked(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    console = ScriptedConsole(answers, ["", SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    assert result.source.password == SOURCE_SECRET
    assert "A password is required." in console.transcript


def test_a_workbook_with_no_sheets_is_rejected(workbook: Path) -> None:
    console = ScriptedConsole([str(workbook)], [])

    with pytest.raises(WorkbookError, match="contains no sheets"):
        run_wizard(console.prompter(), list_sheets=lambda _path: [])


def test_input_ending_early_stops_the_run(workbook: Path, sheets_of: Any) -> None:
    console = ScriptedConsole([str(workbook), "2"], [])

    with pytest.raises(ReconciliationError, match="Input ended before the wizard finished"):
        drive(console, sheets_of)


# -- secrets -------------------------------------------------------------


def test_passwords_are_read_only_through_getpass(workbook: Path, sheets_of: Any) -> None:
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    drive(console, sheets_of)

    assert len(console.secret_prompts) == 2
    assert SOURCE_SECRET not in console.transcript
    assert TARGET_SECRET not in console.transcript


def test_a_password_never_appears_in_a_repr(workbook: Path, sheets_of: Any) -> None:
    """A stray print, a traceback frame or a debugger must not leak it."""
    answers = sqlserver_answers()
    answers[0] = str(workbook)
    console = ScriptedConsole(answers, [SOURCE_SECRET, TARGET_SECRET])

    result = drive(console, sheets_of)

    for rendering in (repr(result), str(result), repr(result.source), str(result.target)):
        assert SOURCE_SECRET not in rendering
        assert TARGET_SECRET not in rendering


def test_settings_describe_themselves_without_the_password() -> None:
    settings = ConnectionSettings(
        side=__import__(
            "migration_reconciliation.database.base", fromlist=["QuerySide"]
        ).QuerySide.SOURCE,
        database_type=DatabaseType.SQLSERVER,
        server="sql-01",
        port=1433,
        database="Db",
        username="reader",
        password=SOURCE_SECRET,
    )

    assert SOURCE_SECRET not in settings.describe()
    assert settings.describe() == "sql-01:1433/Db as reader"


# -- Windows authentication ----------------------------------------------


def windows_answers(workbook: Path, *, source: str = "windows", target: str = "windows") -> list:
    """Answers where either side may use the signed-in Windows account."""
    answers = [str(workbook), "2", "sqlserver", "sql-legacy.internal", "", "LegacyDb", "y", source]
    if source == "password":
        answers.append("legacy_reader")
    answers += ["sql-new.internal", "", "MigratedDb", "n", target]
    if target == "password":
        answers.append("migrated_reader")
    return answers


def test_windows_authentication_asks_for_no_username_and_no_password(
    workbook: Path, sheets_of: Any
) -> None:
    console = ScriptedConsole(windows_answers(workbook), [])

    result = drive(console, sheets_of)

    assert result.source.uses_windows_authentication is True
    assert result.target.uses_windows_authentication is True
    assert result.source.username == ""
    assert result.source.password == ""
    assert console.secret_prompts == []
    assert "Source username" not in console.transcript
    assert "Target username" not in console.transcript


def test_the_two_sides_can_authenticate_differently(workbook: Path, sheets_of: Any) -> None:
    console = ScriptedConsole(
        windows_answers(workbook, source="password", target="windows"), [SOURCE_SECRET]
    )

    result = drive(console, sheets_of)

    assert result.source.uses_windows_authentication is False
    assert result.source.username == "legacy_reader"
    assert result.source.password == SOURCE_SECRET
    assert result.target.uses_windows_authentication is True
    assert len(console.secret_prompts) == 1


def test_an_invalid_authentication_answer_is_re_asked(workbook: Path, sheets_of: Any) -> None:
    answers = windows_answers(workbook)
    answers.insert(7, "kerberos")
    console = ScriptedConsole(answers, [])

    drive(console, sheets_of)

    assert "Answer must be one of: windows, password." in console.transcript


def test_windows_authentication_describes_itself_without_a_username(
    workbook: Path, sheets_of: Any
) -> None:
    console = ScriptedConsole(windows_answers(workbook), [])

    result = drive(console, sheets_of)

    assert result.source.describe() == (
        "sql-legacy.internal:1433/LegacyDb using Windows authentication"
    )


def test_oracle_cannot_use_windows_authentication() -> None:
    from migration_reconciliation.database.base import QuerySide
    from migration_reconciliation.database.settings import AuthMode

    with pytest.raises(ReconciliationError, match="SQL Server only"):
        ConnectionSettings(
            side=QuerySide.SOURCE,
            database_type=DatabaseType.ORACLE,
            server="ora",
            port=1521,
            database="SVC",
            auth_mode=AuthMode.WINDOWS,
        )


def test_password_authentication_requires_both_halves() -> None:
    from migration_reconciliation.database.base import QuerySide

    with pytest.raises(ReconciliationError, match="needs a username and a password"):
        ConnectionSettings(
            side=QuerySide.TARGET,
            database_type=DatabaseType.SQLSERVER,
            server="sql",
            port=1433,
            database="Db",
            username="reader",
            password="",
        )


# -- profiles ------------------------------------------------------------


def profile_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": "1.0",
        "workbook": {"sheet": "Payments"},
        "source": {
            "type": "sqlserver",
            "server": "sql-legacy.internal",
            "port": 1433,
            "database": "LegacyDb",
            "trust_server_certificate": True,
            "authentication": "password",
            "username": "legacy_reader",
        },
        "target": {
            "server": "sql-new.internal",
            "port": 1433,
            "database": "MigratedDb",
            "trust_server_certificate": False,
            "authentication": "windows",
        },
    }
    document.update(overrides)
    return document


def test_a_profile_answers_everything_except_the_password(workbook: Path, sheets_of: Any) -> None:
    from migration_reconciliation.profile import parse_profile

    document = profile_document()
    document["workbook"]["path"] = str(workbook)
    console = ScriptedConsole([], [SOURCE_SECRET])

    result = run_wizard(console.prompter(), profile=parse_profile(document), list_sheets=sheets_of)

    assert console.prompts == []  # nothing was typed except the hidden password
    assert len(console.secret_prompts) == 1
    assert result.sheet_name == "Payments"
    assert result.source.server == "sql-legacy.internal"
    assert result.source.username == "legacy_reader"
    assert result.source.password == SOURCE_SECRET
    assert result.target.uses_windows_authentication is True


def test_a_windows_only_profile_asks_nothing_at_all(workbook: Path, sheets_of: Any) -> None:
    from migration_reconciliation.profile import parse_profile

    document = profile_document()
    document["workbook"]["path"] = str(workbook)
    document["source"]["authentication"] = "windows"
    del document["source"]["username"]
    console = ScriptedConsole([], [])

    result = run_wizard(console.prompter(), profile=parse_profile(document), list_sheets=sheets_of)

    assert console.prompts == []
    assert console.secret_prompts == []
    assert result.source.uses_windows_authentication is True


def test_profile_values_are_echoed_rather_than_applied_silently(
    workbook: Path, sheets_of: Any
) -> None:
    from migration_reconciliation.profile import parse_profile

    document = profile_document()
    document["workbook"]["path"] = str(workbook)
    console = ScriptedConsole([], [SOURCE_SECRET])

    run_wizard(console.prompter(), profile=parse_profile(document), list_sheets=sheets_of)

    assert "[profile] Source server (hostname or IP): sql-legacy.internal" in console.transcript
    assert "[profile] Source database name: LegacyDb" in console.transcript
    assert "[profile] Sheet: Payments" in console.transcript


def test_a_partial_profile_still_asks_for_the_rest(workbook: Path, sheets_of: Any) -> None:
    from migration_reconciliation.profile import parse_profile

    document = {
        "version": "1.0",
        "workbook": {"path": str(workbook)},
        "source": {"type": "sqlserver", "server": "sql-legacy.internal"},
        "target": {"authentication": "windows"},
    }
    console = ScriptedConsole(
        ["2", "", "LegacyDb", "y", "windows", "sql-new.internal", "", "MigratedDb", "n"],
        [],
    )

    result = run_wizard(console.prompter(), profile=parse_profile(document), list_sheets=sheets_of)

    assert result.sheet_name == "Payments"
    assert result.source.server == "sql-legacy.internal"
    assert result.source.database == "LegacyDb"
    assert result.target.database == "MigratedDb"


def test_a_profile_pointing_at_a_missing_workbook_falls_back_to_asking(
    tmp_path: Path, workbook: Path, sheets_of: Any
) -> None:
    from migration_reconciliation.profile import parse_profile

    document = profile_document()
    document["workbook"]["path"] = str(tmp_path / "gone.xlsx")
    console = ScriptedConsole([str(workbook)], [SOURCE_SECRET])

    result = run_wizard(console.prompter(), profile=parse_profile(document), list_sheets=sheets_of)

    assert result.workbook_path == workbook
    assert "was not found, so the question is being asked" in console.transcript


def test_a_profile_naming_a_sheet_this_workbook_lacks_falls_back_to_asking(
    workbook: Path, sheets_of: Any
) -> None:
    from migration_reconciliation.profile import parse_profile

    document = profile_document()
    document["workbook"]["path"] = str(workbook)
    document["workbook"]["sheet"] = "Invoices"
    console = ScriptedConsole(["3"], [SOURCE_SECRET])

    result = run_wizard(console.prompter(), profile=parse_profile(document), list_sheets=sheets_of)

    assert result.sheet_name == "Fines"
    assert "is not in this workbook, so the question is being asked" in console.transcript
