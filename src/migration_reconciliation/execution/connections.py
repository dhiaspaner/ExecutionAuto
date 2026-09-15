"""Turning TOML plus runtime answers into open connections.

The division of labour is the point of this module:

* **TOML** carries everything that is not a secret — engine, server, port,
  database, certificate trust, authentication mode, and a username.
* **The keyboard** carries the password, read with a hidden prompt, held in
  process memory for the run, and written nowhere.

Only the sections the execution plan actually needs are built, and only those
are opened. A workbook of ``TARGET_ONLY`` tests never asks a source question
and never opens a source session.

Non-interactive runs ask nothing. A missing value is then a configuration
error naming the exact key, and password authentication is impossible by
construction: there is no non-interactive source for a password, and adding
one — a flag, a file, an environment variable — is forbidden.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..database.base import QueryExecutor, QuerySide
from ..database.live import build_executor
from ..database.settings import AuthMode, ConnectionSettings, default_port_for
from ..errors import ConfigurationError, DatabaseExecutionError
from ..models import ConnectionIdentity, DatabaseType, ErrorCode, Platform
from ..profile import ConnectionProfile, RunProfile
from ..wizard import Prompter

__all__ = [
    "SectionExecutors",
    "expected_question_count",
    "resolve_section_settings",
]

_SIDES: Mapping[str, QuerySide] = {"source": QuerySide.SOURCE, "target": QuerySide.TARGET}


def resolve_section_settings(
    profile: RunProfile,
    sections: Sequence[str],
    *,
    prompter: Prompter | None = None,
    interactive: bool = True,
) -> dict[str, ConnectionSettings]:
    """Build connection settings for exactly ``sections``, asking for what is missing."""
    ask = prompter if prompter is not None else Prompter()
    if interactive:
        ask.set_total(expected_question_count(profile, sections))

    settings: dict[str, ConnectionSettings] = {}
    for name in sections:
        supplied = profile.section(name)
        if supplied is None:  # pragma: no cover - the reader validates section names
            raise ConfigurationError(
                f"The workbook names profile section '{name}', which does not exist.",
                code=ErrorCode.INVALID_PROFILE,
                platform=Platform.PROFILE,
            )
        if interactive:
            ask.heading(f"{name.capitalize()} database  [{name}]")
        settings[name] = _settings_for(name, supplied, ask, interactive=interactive)
    return settings


def _settings_for(
    name: str, supplied: ConnectionProfile, ask: Prompter, *, interactive: bool
) -> ConnectionSettings:
    label = name.capitalize()
    engine = supplied.database_type
    if engine is None:
        engine = DatabaseType(
            _ask_choice(
                ask,
                f"{label} database type (sqlserver or oracle)",
                ["sqlserver", "oracle"],
                interactive=interactive,
                key=f"[{name}] type",
            )
        )
    elif interactive:
        ask.supplied(f"{label} database type", engine.value)

    server = _answer(
        ask, supplied.server, f"{label} server (hostname or IP)", f"[{name}] server", interactive
    )
    port = supplied.port
    if port is None:
        default = default_port_for(engine)
        port = (
            ask.port(f"{label} port", default)
            if interactive
            else default  # a default port is not a secret and needs no question
        )
    elif interactive:
        ask.supplied(f"{label} port", port)

    database_question = (
        f"{label} service name" if engine is DatabaseType.ORACLE else f"{label} database name"
    )
    database = _answer(ask, supplied.database, database_question, f"[{name}] database", interactive)

    trust = False
    auth = AuthMode.PASSWORD
    if engine is DatabaseType.SQLSERVER:
        trust = _trust(ask, supplied, name, interactive=interactive)
        auth = _auth_mode(ask, supplied, name, interactive=interactive)

    username, password = "", ""
    if auth is AuthMode.PASSWORD:
        if not interactive:
            raise ConfigurationError(
                f"[{name}] uses password authentication, which needs a password typed at "
                f'runtime. A non-interactive run must use authentication = "windows", '
                f"because a password is never read from a file, an argument or an "
                f"environment variable.",
                code=ErrorCode.MISSING_CREDENTIALS,
                platform=Platform.PROFILE,
            )
        username = supplied.username or ""
        if username:
            ask.supplied(f"{label} username", username)
        else:
            username = ask.text(f"{label} username")
        password = ask.secret(f"{label} password (input is hidden)")

    return ConnectionSettings(
        side=_SIDES.get(name, QuerySide.SOURCE),
        database_type=engine,
        server=server,
        port=port,
        database=database,
        username=username,
        password=password,
        auth_mode=auth,
        trust_server_certificate=trust,
        use_thick_client=supplied.use_thick_client,
        oracle_client_dir=supplied.oracle_client_dir or "",
    )


def _answer(ask: Prompter, value: str | None, question: str, key: str, interactive: bool) -> str:
    if value is not None:
        if interactive:
            ask.supplied(question, value)
        return value
    if not interactive:
        raise ConfigurationError(
            f"{key} is missing and this run is non-interactive, so it cannot be asked for.",
            code=ErrorCode.INVALID_PROFILE,
            platform=Platform.PROFILE,
        )
    return ask.text(question)


def _ask_choice(
    ask: Prompter, question: str, options: Sequence[str], *, interactive: bool, key: str
) -> str:
    if not interactive:
        raise ConfigurationError(
            f"{key} is missing and this run is non-interactive, so it cannot be asked for.",
            code=ErrorCode.INVALID_PROFILE,
            platform=Platform.PROFILE,
        )
    return ask.choice(question, options)


def _trust(ask: Prompter, supplied: ConnectionProfile, name: str, *, interactive: bool) -> bool:
    question = f"Trust the {name} server's certificate? (y/n)"
    if supplied.trust_server_certificate is not None:
        if interactive:
            ask.supplied(question, "yes" if supplied.trust_server_certificate else "no")
        return supplied.trust_server_certificate
    if not interactive:
        # Verifying the certificate is the safe answer, so it is the one an
        # unattended run gets without being asked.
        return False
    ask.say("")
    ask.say("      The connection is encrypted. Answer y if this server uses a certificate")
    ask.say("      this machine does not already trust, which is usual for local and")
    ask.say("      internal servers. Answer n to verify it properly.")
    return ask.yes_no(question)


def _auth_mode(
    ask: Prompter, supplied: ConnectionProfile, name: str, *, interactive: bool
) -> AuthMode:
    question = f"{name.capitalize()} authentication (windows or password)"
    if supplied.auth_mode is not None:
        if interactive:
            ask.supplied(question, supplied.auth_mode.value)
        return supplied.auth_mode
    if not interactive:
        raise ConfigurationError(
            f'[{name}] authentication is missing. Set it to "windows" or "password".',
            code=ErrorCode.INVALID_PROFILE,
            platform=Platform.PROFILE,
        )
    ask.say("")
    ask.say("      Answer 'windows' to connect as the account already signed in to this")
    ask.say("      machine, which needs no username or password at all. Answer 'password'")
    ask.say("      to sign in with a database login typed in below.")
    return AuthMode(ask.choice(question, ["windows", "password"]))


def expected_question_count(profile: RunProfile, sections: Sequence[str]) -> int:
    """How many questions the run is about to ask, for the ``[n/total]`` counter."""
    total = 0
    for name in sections:
        supplied = profile.section(name)
        if supplied is None:  # pragma: no cover - validated before this point
            continue
        engine = supplied.database_type
        if engine is None:
            total += 1
        total += sum(
            1 for value in (supplied.server, supplied.port, supplied.database) if value is None
        )
        auth = supplied.auth_mode or AuthMode.PASSWORD
        if engine is not DatabaseType.ORACLE:
            if supplied.trust_server_certificate is None:
                total += 1
            if supplied.auth_mode is None:
                total += 1
        else:
            auth = AuthMode.PASSWORD
        if auth is AuthMode.PASSWORD:
            if supplied.username is None:
                total += 1
            total += 1  # the password is always typed, never taken from a file
    return total


class SectionExecutors:
    """The open connections for one run, keyed by profile section name.

    One executor per section, shared by every test that names it, so a run of
    two hundred tests opens two sessions and asks for each password once.
    """

    def __init__(
        self,
        settings: Mapping[str, ConnectionSettings],
        *,
        drivers: Mapping[str, object] | None = None,
    ) -> None:
        self._settings = dict(settings)
        self._executors: dict[str, QueryExecutor] = {
            name: build_executor(value, driver=(drivers or {}).get(name))
            for name, value in settings.items()
        }

    @property
    def sections(self) -> tuple[str, ...]:
        return tuple(self._executors)

    def settings_for(self, section: str) -> ConnectionSettings:
        return self._settings[section]

    def database_name(self, section: str) -> str:
        """The database or service name, for observations. Never the host or login."""
        return self._settings[section].database

    def describe(self, section: str) -> str:
        """A line safe to print: host, database and how it authenticated."""
        return self._settings[section].describe()

    def open(self, section: str) -> ConnectionIdentity:
        """Establish the session, or raise a sanitized failure."""
        return self._executors[section].test_connection()

    def open_all(self) -> tuple[dict[str, ConnectionIdentity], dict[str, str]]:
        """Open every section, collecting failures rather than stopping at the first.

        A failed source and a working target still lets every ``TARGET_ONLY``
        test run, which is worth more than an early exit.
        """
        identities: dict[str, ConnectionIdentity] = {}
        failures: dict[str, str] = {}
        for section in self._executors:
            try:
                identities[section] = self.open(section)
            except DatabaseExecutionError as exc:
                failures[section] = str(exc)
            except Exception as exc:  # a driver can raise anything at all
                failures[section] = f"{type(exc).__name__}: {exc}"
        return identities, failures

    def executor_for(self, section: str) -> QueryExecutor:
        return self._executors[section]

    def close_all(self) -> None:
        """Close every session. One failing close never skips the rest."""
        for executor in self._executors.values():
            try:
                executor.close()
            except Exception:  # cleanup must not mask the error that triggered it
                continue
