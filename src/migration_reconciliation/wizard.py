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
from .database.settings import ConnectionSettings, default_port_for
from .errors import ReconciliationError, WorkbookError
from .models import DatabaseType

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
    list_sheets: Callable[[Path], Sequence[str]] | None = None,
) -> WizardAnswers:
    """Ask all fifteen questions, in order, and return the answers.

    No connection is opened here, and no query is run. The caller connects only
    after every answer is in hand.
    """
    ask = prompter or Prompter()
    read_sheets = list_sheets or sheet_names_of

    ask.heading("Workbook")
    workbook_path = ask.existing_workbook("Path to the workbook (.xlsx)")

    sheets = read_sheets(workbook_path)
    if not sheets:
        raise WorkbookError(f"'{workbook_path.name}' contains no sheets.")
    ask.say("")
    ask.say(f"      {workbook_path.name} contains {len(sheets)} sheet(s):")
    sheet_name = ask.sheet("Which sheet holds the test cases? (number or name)", sheets)

    ask.heading("Source database")
    source_type = DatabaseType(
        ask.choice("Source database type (sqlserver or oracle)", ["sqlserver", "oracle"])
    )
    if source_type is DatabaseType.ORACLE:
        # The certificate question is SQL Server only, so one fewer is asked.
        # Say so, or the question counter appears to lose a step.
        ask.set_total(14)
        ask.say("      Oracle needs no certificate question, so there are 14 questions in all.")

    source_server = ask.text("Source server (hostname or IP)")
    source_port = ask.port("Source port", default_port_for(source_type))
    source_database = ask.text(
        "Source service name" if source_type is DatabaseType.ORACLE else "Source database name"
    )

    source_trust = False
    if source_type is DatabaseType.SQLSERVER:
        ask.say("")
        ask.say("      The connection is encrypted. Answer y if this server uses a self-signed")
        ask.say("      certificate that this machine does not already trust, which is usual for")
        ask.say("      local and internal servers. Answer n to verify it properly.")
        source_trust = ask.yes_no("Trust the source server's certificate? (y/n)")

    source_username = ask.text("Source username")
    source_password = ask.secret("Source password (input is hidden)")

    ask.heading("Target database (always SQL Server)")
    target_server = ask.text("Target server (hostname or IP)")
    target_port = ask.port("Target port", default_port_for(DatabaseType.SQLSERVER))
    target_database = ask.text("Target database name")
    ask.say("")
    ask.say("      Same certificate question for the target server.")
    target_trust = ask.yes_no("Trust the target server's certificate? (y/n)")
    target_username = ask.text("Target username")
    target_password = ask.secret("Target password (input is hidden)")

    source = ConnectionSettings(
        side=QuerySide.SOURCE,
        database_type=source_type,
        server=source_server,
        port=source_port,
        database=source_database,
        username=source_username,
        password=source_password,
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
        trust_server_certificate=target_trust,
    )
    return WizardAnswers(
        workbook_path=workbook_path,
        sheet_name=sheet_name,
        source=source,
        target=target,
    )


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
