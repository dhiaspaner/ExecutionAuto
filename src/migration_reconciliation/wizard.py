"""The interactive connection wizard.

One question at a time, in a fixed order, each one re-asked until it has a
valid answer. There are no connection flags: a person running a reconciliation
against an unfamiliar server should not have to remember a command line, and a
forgotten ``--source-server`` should not become a failed run.

Nothing gathered here is cached, written to a file, or read from an environment
variable. Every run asks for everything again, including the passwords, which
are read with :func:`getpass.getpass` and never echoed.

No connection is attempted from this module. The wizard's only side effect is
opening the workbook to list its sheet names.
"""

from __future__ import annotations

import getpass as getpass_module
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .database.base import QuerySide
from .database.settings import AuthMode, ConnectionSettings, default_port_for
from .errors import ReconciliationError, WorkbookError
from .models import DatabaseType
from .profile import ConnectionProfile, RunProfile

__all__ = ["Prompter", "WizardAnswers", "run_wizard"]

MAX_PORT = 65535


@dataclass(frozen=True, slots=True)
class WizardAnswers:
    """Everything the fifteen questions produced."""

    workbook_path: Path
    sheet_name: str
    source: ConnectionSettings
    target: ConnectionSettings

    def __repr__(self) -> str:
        """Passwords live inside ``source`` and ``target``; keep them unprintable."""
        return (
            f"WizardAnswers(workbook={self.workbook_path.name!r}, "
            f"sheet={self.sheet_name!r}, source={self.source!r}, target={self.target!r})"
        )


class Prompter:
    """Asks questions until they are answered properly.

    ``input_fn`` and ``getpass_fn`` are injected so the whole wizard can be
    driven by tests without a terminal.
    """

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] | None = None,
        getpass_fn: Callable[[str], str] | None = None,
        output: Callable[[str], None] | None = None,
    ) -> None:
        # Deliberately stored as None rather than defaulting to the builtins:
        # a default argument binds once, at import, which would make the real
        # ``input`` unreplaceable and the whole wizard untestable.
        self._input = input_fn
        self._getpass = getpass_fn
        self._output = output
        self._asked = 0
        self._total = 15

    # -- presentation ----------------------------------------------------

    def say(self, message: str = "") -> None:
        write = self._output if self._output is not None else print
        write(message)

    def heading(self, title: str) -> None:
        self.say("")
        self.say(title)
        self.say("-" * len(title))

    def set_total(self, total: int) -> None:
        """Oracle sources skip the certificate question, so the total shrinks."""
        self._total = total

    def _label(self, question: str) -> str:
        self._asked += 1
        return f"[{self._asked}/{self._total}] {question}"

    def _retry(self, reason: str) -> None:
        self.say(f"      {reason}")

    def supplied(self, question: str, value: object) -> None:
        """Echo an answer that came from the profile instead of the keyboard.

        Printed rather than silently applied, so the person can see every value
        the run is about to use.
        """
        self.say(f"      [profile] {question}: {value}")

    def warn(self, message: str) -> None:
        self.say(f"      [profile] {message}")

    # -- question types --------------------------------------------------

    def text(self, question: str) -> str:
        """A non-empty line. Re-asked until one arrives."""
        label = self._label(question)
        while True:
            answer = self._read(label).strip()
            if answer:
                return answer
            self._retry("A value is required.")

    def secret(self, question: str) -> str:
        """A password, read with a hidden prompt and never echoed."""
        label = self._label(question)
        while True:
            answer = self._read_secret(label)
            if answer:
                return answer
            self._retry("A password is required.")

    def choice(self, question: str, options: Sequence[str]) -> str:
        """One of ``options``, compared case-insensitively."""
        label = self._label(question)
        lookup = {option.casefold(): option for option in options}
        while True:
            answer = self._read(label).strip().casefold()
            if answer in lookup:
                return lookup[answer]
            self._retry(f"Answer must be one of: {', '.join(options)}.")

    def yes_no(self, question: str) -> bool:
        label = self._label(question)
        while True:
            answer = self._read(label).strip().casefold()
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self._retry("Answer y or n.")

    def port(self, question: str, default: int) -> int:
        """A TCP port. Pressing Enter accepts ``default``."""
        label = self._label(f"{question} [{default}]")
        while True:
            answer = self._read(label).strip()
            if not answer:
                return default
            try:
                port = int(answer)
            except ValueError:
                self._retry(f"Enter a number between 1 and {MAX_PORT}, or press Enter.")
                continue
            if 1 <= port <= MAX_PORT:
                return port
            self._retry(f"Enter a number between 1 and {MAX_PORT}, or press Enter.")

    def existing_workbook(self, question: str) -> Path:
        """A path to a readable ``.xlsx`` file."""
        label = self._label(question)
        while True:
            raw = self._read(label).strip().strip('"').strip("'")
            if not raw:
                self._retry("A path is required.")
                continue
            path = Path(raw).expanduser()
            if not path.exists():
                self._retry(f"No such file: {path}")
                continue
            if not path.is_file():
                self._retry(f"Not a file: {path}")
                continue
            if path.suffix.lower() != ".xlsx":
                self._retry("The workbook must be an .xlsx file.")
                continue
            return path

    def sheet(self, question: str, sheet_names: Sequence[str]) -> str:
        """One sheet, chosen by its number in the printed list or by name."""
        for index, name in enumerate(sheet_names, start=1):
            self.say(f"      {index}. {name}")
        label = self._label(question)
        by_name = {name.casefold(): name for name in sheet_names}
        while True:
            answer = self._read(label).strip()
            if not answer:
                self._retry("Choose a sheet by number or name.")
                continue
            if answer.isdigit():
                position = int(answer)
                if 1 <= position <= len(sheet_names):
                    return sheet_names[position - 1]
                self._retry(f"Choose a number between 1 and {len(sheet_names)}.")
                continue
            match = by_name.get(answer.casefold())
            if match is not None:
                return match
            self._retry("That does not match any sheet in this workbook.")

    # -- io --------------------------------------------------------------

    def _read(self, label: str) -> str:
        reader = self._input if self._input is not None else input
        try:
            return reader(f"{label}\n      > ")
        except EOFError:
            raise ReconciliationError("Input ended before the wizard finished.") from None

    def _read_secret(self, label: str) -> str:
        self.say(label)
        reader = self._getpass if self._getpass is not None else getpass_module.getpass
        try:
            return reader("      > ")
        except EOFError:
            raise ReconciliationError("Input ended before the wizard finished.") from None


def run_wizard(
    prompter: Prompter | None = None,
    *,
    profile: RunProfile | None = None,
    list_sheets: Callable[[Path], Sequence[str]] | None = None,
) -> WizardAnswers:
    """Ask for everything the profile did not already answer, in order.

    No connection is opened here, and no query is run. The caller connects only
    after every answer is in hand.
    """
    ask = prompter or Prompter()
    supplied = profile or RunProfile.empty()
    _reject_oracle_target(supplied)
    read_sheets = list_sheets or sheet_names_of
    ask.set_total(_expected_total(supplied))

    ask.heading("Workbook")
    workbook_path = _workbook_from_profile(ask, supplied)
    if workbook_path is None:
        workbook_path = ask.existing_workbook("Path to the workbook (.xlsx)")

    sheets = read_sheets(workbook_path)
    if not sheets:
        raise WorkbookError(f"'{workbook_path.name}' contains no sheets.")
    sheet_name = _sheet_from_profile(ask, supplied, sheets)
    if sheet_name is None:
        ask.say("")
        ask.say(f"      {workbook_path.name} contains {len(sheets)} sheet(s):")
        sheet_name = ask.sheet("Which sheet holds the test cases? (number or name)", sheets)

    ask.heading("Source database")
    source_type = supplied.source.database_type
    if source_type is not None:
        ask.supplied("Source database type", source_type.value)
    else:
        source_type = DatabaseType(
            ask.choice("Source database type (sqlserver or oracle)", ["sqlserver", "oracle"])
        )
    ask.set_total(_expected_total(supplied, source_type=source_type))
    if source_type is DatabaseType.ORACLE:
        ask.say("      Oracle uses a username and password; no certificate question applies.")

    source_server = _server(ask, supplied.source, "Source server (hostname or IP)")
    source_port = _port(ask, supplied.source, "Source port", source_type)
    source_database = _database(
        ask,
        supplied.source,
        "Source service name" if source_type is DatabaseType.ORACLE else "Source database name",
    )

    source_trust = False
    source_auth = AuthMode.PASSWORD
    if source_type is DatabaseType.SQLSERVER:
        source_trust = _trust(ask, supplied.source, "source")
        source_auth = _auth_mode(ask, supplied.source, "source")
        ask.set_total(_expected_total(supplied, source_type=source_type, source_auth=source_auth))

    source_username, source_password = _credentials(ask, supplied.source, source_auth, "Source")

    ask.heading("Target database (always SQL Server)")
    target_server = _server(ask, supplied.target, "Target server (hostname or IP)")
    target_port = _port(ask, supplied.target, "Target port", DatabaseType.SQLSERVER)
    target_database = _database(ask, supplied.target, "Target database name")
    target_trust = _trust(ask, supplied.target, "target")
    target_auth = _auth_mode(ask, supplied.target, "target")
    ask.set_total(
        _expected_total(
            supplied,
            source_type=source_type,
            source_auth=source_auth,
            target_auth=target_auth,
        )
    )
    target_username, target_password = _credentials(ask, supplied.target, target_auth, "Target")

    source = ConnectionSettings(
        side=QuerySide.SOURCE,
        database_type=source_type,
        server=source_server,
        port=source_port,
        database=source_database,
        username=source_username,
        password=source_password,
        auth_mode=source_auth,
        trust_server_certificate=source_trust,
    )
    target = ConnectionSettings(
        side=QuerySide.TARGET,
        database_type=DatabaseType.SQLSERVER,
        server=target_server,
        port=target_port,
        database=target_database,
        username=target_username,
        password=target_password,
        auth_mode=target_auth,
        trust_server_certificate=target_trust,
    )
    return WizardAnswers(
        workbook_path=workbook_path,
        sheet_name=sheet_name,
        source=source,
        target=target,
    )


# -- one question each, profile first -----------------------------------------


def _reject_oracle_target(profile: RunProfile) -> None:
    """Refuse a profile whose target is Oracle, before a single question is asked.

    The target this wizard builds is always SQL Server, so a profile naming
    Oracle there would otherwise be quietly ignored and the run would connect to
    an engine the file never asked for. Oracle is a source engine only.
    """
    if profile.target.database_type is not DatabaseType.ORACLE:
        return
    if profile.target_type_inherited:
        raise ReconciliationError(
            f'{profile.source_path}: [target] has no "type", so it inherited "oracle" from '
            '[source], but the target is always SQL Server. Add type = "sqlserver" to [target].'
        )
    raise ReconciliationError(
        f'{profile.source_path}: [target] type = "oracle" is not supported. Oracle is '
        "available as a source only; the target is always SQL Server."
    )


def _workbook_from_profile(ask: Prompter, profile: RunProfile) -> Path | None:
    """Use the profile's workbook if it is actually there; otherwise ask."""
    path = profile.workbook_path
    if path is None:
        return None
    if not path.is_file():
        ask.warn(f"workbook '{path}' was not found, so the question is being asked.")
        return None
    if path.suffix.lower() != ".xlsx":
        ask.warn(f"workbook '{path}' is not an .xlsx file, so the question is being asked.")
        return None
    ask.supplied("Workbook", path)
    return path


def _sheet_from_profile(ask: Prompter, profile: RunProfile, sheets: Sequence[str]) -> str | None:
    """Use the profile's sheet if this workbook has it; otherwise ask."""
    wanted = profile.sheet_name
    if wanted is None:
        return None
    for name in sheets:
        if name.casefold() == wanted.casefold():
            ask.supplied("Sheet", name)
            return name
    ask.warn(f"sheet '{wanted}' is not in this workbook, so the question is being asked.")
    return None


def _server(ask: Prompter, profile: ConnectionProfile, question: str) -> str:
    if profile.server is not None:
        ask.supplied(question, profile.server)
        return profile.server
    return ask.text(question)


def _port(
    ask: Prompter, profile: ConnectionProfile, question: str, database_type: DatabaseType
) -> int:
    if profile.port is not None:
        ask.supplied(question, profile.port)
        return profile.port
    return ask.port(question, default_port_for(database_type))


def _database(ask: Prompter, profile: ConnectionProfile, question: str) -> str:
    if profile.database is not None:
        ask.supplied(question, profile.database)
        return profile.database
    return ask.text(question)


def _trust(ask: Prompter, profile: ConnectionProfile, side: str) -> bool:
    question = f"Trust the {side} server's certificate? (y/n)"
    if profile.trust_server_certificate is not None:
        ask.supplied(question, "yes" if profile.trust_server_certificate else "no")
        return profile.trust_server_certificate
    ask.say("")
    ask.say("      The connection is encrypted. Answer y if this server uses a self-signed")
    ask.say("      certificate that this machine does not already trust, which is usual for")
    ask.say("      local and internal servers. Answer n to verify it properly.")
    return ask.yes_no(question)


def _auth_mode(ask: Prompter, profile: ConnectionProfile, side: str) -> AuthMode:
    question = f"{side.capitalize()} authentication (windows or password)"
    if profile.auth_mode is not None:
        ask.supplied(question, profile.auth_mode.value)
        return profile.auth_mode
    ask.say("")
    ask.say("      Answer 'windows' to connect as the account already signed in to this")
    ask.say("      machine, which needs no username or password at all. Answer 'password'")
    ask.say("      to sign in with a SQL Server login typed in below.")
    return AuthMode(ask.choice(question, ["windows", "password"]))


def _credentials(
    ask: Prompter, profile: ConnectionProfile, auth_mode: AuthMode, side: str
) -> tuple[str, str]:
    """Username and password, or nothing at all under Windows authentication."""
    if auth_mode is AuthMode.WINDOWS:
        ask.say(f"      {side} connection uses the signed-in Windows account; no password needed.")
        return "", ""
    if profile.username is not None:
        ask.supplied(f"{side} username", profile.username)
        username = profile.username
    else:
        username = ask.text(f"{side} username")
    password = ask.secret(f"{side} password (input is hidden)")
    return username, password


def _expected_total(
    profile: RunProfile,
    *,
    source_type: DatabaseType | None = None,
    source_auth: AuthMode | None = None,
    target_auth: AuthMode | None = None,
) -> int:
    """How many questions remain to be asked, given what is already known.

    Branches change the count — Oracle skips the certificate question, Windows
    authentication skips a username and a password, and a profile skips whatever
    it answered — so the total is recomputed as each branch resolves, and the
    longer path is assumed until then.
    """
    total = 0
    if profile.workbook_path is None:
        total += 1
    if profile.sheet_name is None:
        total += 1

    source = profile.source
    engine = source_type or source.database_type
    if source.database_type is None:
        total += 1
    total += sum(1 for value in (source.server, source.port, source.database) if value is None)

    resolved_source_auth = source_auth or source.auth_mode or AuthMode.PASSWORD
    if engine is DatabaseType.ORACLE:
        resolved_source_auth = AuthMode.PASSWORD
    else:
        if source.trust_server_certificate is None:
            total += 1
        if source.auth_mode is None:
            total += 1
    if resolved_source_auth is AuthMode.PASSWORD:
        total += 1 if source.username is None else 0
        total += 1  # the password is always typed, never taken from a file

    target = profile.target
    total += sum(1 for value in (target.server, target.port, target.database) if value is None)
    if target.trust_server_certificate is None:
        total += 1
    if target.auth_mode is None:
        total += 1
    resolved_target_auth = target_auth or target.auth_mode or AuthMode.PASSWORD
    if resolved_target_auth is AuthMode.PASSWORD:
        total += 1 if target.username is None else 0
        total += 1
    return total


def sheet_names_of(path: Path) -> list[str]:
    """List a workbook's sheets without reading any data row."""
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True)
    except Exception as exc:
        raise WorkbookError(f"Cannot open workbook '{path.name}': {exc}") from None
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()
