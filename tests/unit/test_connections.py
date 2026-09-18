"""Turning a profile into connection settings, and what is asked for."""

from __future__ import annotations

from typing import Any

import pytest

from migration_reconciliation.database.base import QuerySide
from migration_reconciliation.database.settings import AuthMode
from migration_reconciliation.errors import ConfigurationError
from migration_reconciliation.execution.connections import (
    SectionExecutors,
    expected_question_count,
    resolve_section_settings,
)
from migration_reconciliation.models import DatabaseType, ErrorCode
from migration_reconciliation.profile import parse_profile
from migration_reconciliation.wizard import Prompter


def windows_profile(**overrides: Any) -> Any:
    document: dict[str, Any] = {
        "version": "1.0",
        "workbook": {"path": "C:/data/Payments.xlsx", "sheet": "Test Cases"},
        "source": {
            "type": "sqlserver",
            "server": "localhost",
            "port": 1433,
            "authentication": "windows",
            "trust_server_certificate": True,
            "database": "webservice",
        },
        "target": {
            "type": "sqlserver",
            "server": "localhost",
            "port": 1433,
            "database": "PaymentRecon_Target_Local",
            "trust_server_certificate": True,
            "authentication": "windows",
        },
    }
    for section, values in overrides.items():
        document[section] = {**document.get(section, {}), **values}
    return parse_profile(document, source="<test>")


class Answers:
    """A scripted keyboard. Records which prompts were hidden.

    An exhausted queue raises ``EOFError``, exactly as a closed stdin does.
    Returning a blank answer instead would make the prompter re-ask forever
    and hang the suite rather than fail it.
    """

    def __init__(self, *answers: str) -> None:
        self.queue = list(answers)
        self.asked: list[str] = []
        self.hidden: list[str] = []
        self.printed: list[str] = []

    def _next(self) -> str:
        if not self.queue:
            raise EOFError("the scripted answers ran out")
        return self.queue.pop(0)

    def input(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self._next()

    def getpass(self, prompt: str) -> str:
        self.hidden.append(prompt)
        return self._next()

    def say(self, message: str) -> None:
        self.printed.append(message)

    def prompter(self) -> Prompter:
        return Prompter(input_fn=self.input, getpass_fn=self.getpass, output=self.say)


# -- only what is needed -----------------------------------------------------


def test_only_the_requested_sections_are_built() -> None:
    settings = resolve_section_settings(windows_profile(), ["target"])

    assert set(settings) == {"target"}
    assert settings["target"].database == "PaymentRecon_Target_Local"
    assert settings["target"].side is QuerySide.TARGET


def test_windows_authentication_asks_nothing_and_carries_no_credential() -> None:
    answers = Answers()

    settings = resolve_section_settings(
        windows_profile(), ["source", "target"], prompter=answers.prompter()
    )

    assert answers.asked == []
    assert answers.hidden == []
    for connection in settings.values():
        assert connection.uses_windows_authentication
        assert connection.username == ""
        assert connection.password == ""


# -- password authentication -------------------------------------------------


def test_a_password_is_read_with_a_hidden_prompt_and_never_from_the_file() -> None:
    profile = windows_profile(source={"authentication": "password", "username": "recon_reader"})
    answers = Answers("s3cret")

    settings = resolve_section_settings(profile, ["source"], prompter=answers.prompter())

    assert settings["source"].username == "recon_reader"
    assert settings["source"].password == "s3cret"
    assert len(answers.hidden) == 1
    assert answers.asked == []  # the username came from TOML


def test_a_missing_username_is_asked_for_before_the_password() -> None:
    without_username = parse_profile(
        {
            "version": "1.0",
            "source": {
                "type": "sqlserver",
                "server": "localhost",
                "port": 1433,
                "database": "webservice",
                "authentication": "password",
                "trust_server_certificate": True,
            },
        },
        source="<test>",
    )
    answers = Answers("typed_user", "typed_password")

    settings = resolve_section_settings(without_username, ["source"], prompter=answers.prompter())

    assert settings["source"].username == "typed_user"
    assert settings["source"].password == "typed_password"
    assert len(answers.asked) == 1
    assert len(answers.hidden) == 1


def test_a_password_never_appears_in_a_repr() -> None:
    profile = windows_profile(source={"authentication": "password", "username": "recon_reader"})
    settings = resolve_section_settings(profile, ["source"], prompter=Answers("hunter2").prompter())

    assert "hunter2" not in repr(settings["source"])
    assert "hunter2" not in str(settings["source"])
    assert "hunter2" not in settings["source"].describe()


# -- prompting for what the profile left out ---------------------------------


def test_a_missing_value_is_asked_for_one_question_at_a_time() -> None:
    partial = parse_profile(
        {"version": "1.0", "source": {"type": "sqlserver", "authentication": "windows"}},
        source="<test>",
    )
    answers = Answers("sql01", "", "webservice", "y")

    settings = resolve_section_settings(partial, ["source"], prompter=answers.prompter())

    assert settings["source"].server == "sql01"
    assert settings["source"].port is None  # Enter left it for the driver to detect
    assert settings["source"].database == "webservice"
    assert settings["source"].trust_server_certificate is True


def test_the_question_count_matches_what_is_missing() -> None:
    complete = windows_profile()
    assert expected_question_count(complete, ["source", "target"]) == 0

    partial = parse_profile(
        {"version": "1.0", "source": {"type": "sqlserver", "authentication": "windows"}},
        source="<test>",
    )
    # server, port, database, certificate trust
    assert expected_question_count(partial, ["source"]) == 4


# -- non-interactive ---------------------------------------------------------


def test_a_non_interactive_run_never_prompts() -> None:
    answers = Answers()

    settings = resolve_section_settings(
        windows_profile(), ["source"], prompter=answers.prompter(), interactive=False
    )

    assert answers.asked == []
    assert settings["source"].server == "localhost"


def test_a_non_interactive_run_refuses_password_authentication() -> None:
    """There is no non-interactive source for a password, by design."""
    profile = windows_profile(source={"authentication": "password", "username": "recon_reader"})

    with pytest.raises(ConfigurationError) as caught:
        resolve_section_settings(profile, ["source"], interactive=False)

    assert caught.value.code == ErrorCode.MISSING_CREDENTIALS


def test_a_non_interactive_run_names_the_key_it_is_missing() -> None:
    partial = parse_profile(
        {"version": "1.0", "source": {"type": "sqlserver", "authentication": "windows"}},
        source="<test>",
    )

    with pytest.raises(ConfigurationError, match=r"\[source\] server"):
        resolve_section_settings(partial, ["source"], interactive=False)


# -- the executors -----------------------------------------------------------


def test_executors_are_built_for_each_section_without_connecting() -> None:
    settings = resolve_section_settings(windows_profile(), ["source", "target"])

    executors = SectionExecutors(settings)

    assert executors.sections == ("source", "target")
    assert executors.database_name("source") == "webservice"
    assert "localhost:1433/webservice" in executors.describe("source")
    executors.close_all()


def test_a_failing_connection_is_collected_rather_than_raised() -> None:
    """A broken source must not stop the target-only tests from running."""
    settings = resolve_section_settings(windows_profile(), ["source"])
    executors = SectionExecutors(settings)
    try:
        _identities, failures = executors.open_all()
    finally:
        executors.close_all()

    # No driver is installed in the test environment, so opening must fail
    # cleanly and describe itself without leaking a connection string.
    assert set(failures) == {"source"}
    assert "PWD" not in failures["source"]


def test_the_target_engine_is_whatever_the_profile_declares() -> None:
    profile = windows_profile(target={"type": "oracle", "authentication": "password"})

    assert profile.target.database_type is DatabaseType.ORACLE
    assert profile.target.auth_mode is AuthMode.PASSWORD


# -- ports -------------------------------------------------------------------


def _sqlserver_section(**extra: Any) -> Any:
    section: dict[str, Any] = {
        "type": "sqlserver",
        "server": "HOST\\SQLEXPRESS",
        "database": "webservice",
        "authentication": "windows",
        "trust_server_certificate": True,
    }
    section.update(extra)
    return parse_profile({"version": "1.0", "source": section}, source="<test>")


def test_a_sql_server_port_left_out_stays_unset_rather_than_becoming_1433() -> None:
    settings = resolve_section_settings(_sqlserver_section(), ["source"], interactive=False)

    # None is what makes the driver ask the SQL Browser for the instance's port.
    assert settings["source"].port is None


def test_an_empty_sql_server_port_means_the_same_as_leaving_it_out() -> None:
    settings = resolve_section_settings(_sqlserver_section(port=""), ["source"], interactive=False)

    assert settings["source"].port is None


def test_pressing_enter_at_the_port_question_leaves_it_unset() -> None:
    answers = Answers("")

    settings = resolve_section_settings(
        _sqlserver_section(), ["source"], prompter=answers.prompter()
    )

    assert len(answers.asked) == 1
    assert settings["source"].port is None


def test_a_typed_sql_server_port_is_kept() -> None:
    answers = Answers("14330")

    settings = resolve_section_settings(
        _sqlserver_section(), ["source"], prompter=answers.prompter()
    )

    assert settings["source"].port == 14330


def test_oracle_still_takes_its_default_port_because_it_has_no_discovery() -> None:
    profile = parse_profile(
        {
            "version": "1.0",
            "source": {
                "type": "oracle",
                "server": "legacy-ora",
                "database": "LEGACYPAY",
                "username": "recon_reader",
            },
        },
        source="<test>",
    )
    answers = Answers("", "s3cret")

    settings = resolve_section_settings(profile, ["source"], prompter=answers.prompter())

    assert settings["source"].port == 1521
