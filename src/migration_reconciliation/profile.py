"""Run profiles: a TOML file that pre-answers the wizard's questions.

Typing fifteen answers is fine once. Doing it every morning against the same
two servers is not, so a profile file can supply any of them up front::

    uv run python run_reconciliation.py --profile config/run_profile.example.toml

Whatever the file answers is skipped and echoed back on screen, so the person
can see what was assumed. Whatever it leaves out is still asked, one question at
a time, exactly as before. A profile is a convenience, never a hidden default:
nothing is silently guessed, and the values used are always printed.

**A profile never contains a password.** There is no key for one, and a file
carrying anything that looks like a credential is rejected rather than ignored.
Passwords are typed at runtime, or avoided entirely by using Windows
authentication, which needs no credential at all.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database.settings import AuthMode
from .errors import ReconciliationError
from .models import DatabaseType

__all__ = ["SUPPORTED_PROFILE_VERSIONS", "ConnectionProfile", "RunProfile", "load_profile"]

SUPPORTED_PROFILE_VERSIONS: frozenset[str] = frozenset({"1.0"})

_ALLOWED_TOP_LEVEL_KEYS = frozenset({"version", "workbook", "source", "target"})
_ALLOWED_WORKBOOK_KEYS = frozenset({"path", "sheet"})
_ALLOWED_SOURCE_KEYS = frozenset(
    {"type", "server", "port", "database", "trust_server_certificate", "authentication", "username"}
)
_ALLOWED_TARGET_KEYS = _ALLOWED_SOURCE_KEYS - {"type"}

#: Keys that must never appear. Naming them explicitly turns "I put the password
#: in the file and it was ignored" into a loud, immediate error.
_FORBIDDEN_KEYS = frozenset(
    {"password", "pwd", "passwd", "secret", "token", "credential", "credentials", "pass"}
)

MAX_PORT = 65535


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    """Pre-answered connection details for one side. Every field is optional."""

    database_type: DatabaseType | None = None
    server: str | None = None
    port: int | None = None
    database: str | None = None
    trust_server_certificate: bool | None = None
    auth_mode: AuthMode | None = None
    username: str | None = None


@dataclass(frozen=True, slots=True)
class RunProfile:
    """Everything a profile file supplied. Absent answers stay ``None``."""

    source: ConnectionProfile
    target: ConnectionProfile
    workbook_path: Path | None = None
    sheet_name: str | None = None
    #: Where it came from, for the "taken from the profile" messages.
    source_path: str = "<profile>"

    @classmethod
    def empty(cls) -> RunProfile:
        return cls(source=ConnectionProfile(), target=ConnectionProfile())


def load_profile(path: str | Path) -> RunProfile:
    """Read and validate a profile file."""
    profile_path = Path(path)
    try:
        raw = profile_path.read_bytes()
    except OSError as exc:
        raise ReconciliationError(f"Cannot read profile '{profile_path}': {exc.strerror}") from None
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ReconciliationError(f"'{profile_path}' is not valid UTF-8") from None
    except tomllib.TOMLDecodeError as exc:
        raise ReconciliationError(f"'{profile_path}' is not valid TOML: {exc}") from None
    return parse_profile(document, source=str(profile_path))


def parse_profile(document: dict[str, Any], *, source: str = "<profile>") -> RunProfile:
    """Validate an already-parsed profile document."""
    _reject_credentials(document, source, where="")
    _reject_unknown(document, _ALLOWED_TOP_LEVEL_KEYS, source, where="")

    version = document.get("version")
    if not isinstance(version, str) or version not in SUPPORTED_PROFILE_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_PROFILE_VERSIONS))
        raise ReconciliationError(
            f"{source}: version must be one of: {supported} (got {version!r})"
        )

    workbook = _table(document, "workbook", source)
    _reject_unknown(workbook, _ALLOWED_WORKBOOK_KEYS, source, where="[workbook]")
    workbook_path = _optional_string(workbook, "path", source, "[workbook]")
    sheet_name = _optional_string(workbook, "sheet", source, "[workbook]")

    source_table = _table(document, "source", source)
    _reject_unknown(source_table, _ALLOWED_SOURCE_KEYS, source, where="[source]")
    target_table = _table(document, "target", source)
    _reject_unknown(target_table, _ALLOWED_TARGET_KEYS, source, where="[target]")

    source_profile = _connection(source_table, source, "[source]", allow_type=True)
    target_profile = _connection(target_table, source, "[target]", allow_type=False)

    return RunProfile(
        source=source_profile,
        target=target_profile,
        workbook_path=Path(workbook_path).expanduser() if workbook_path else None,
        sheet_name=sheet_name,
        source_path=source,
    )


def _connection(
    table: dict[str, Any], source: str, where: str, *, allow_type: bool
) -> ConnectionProfile:
    database_type: DatabaseType | None = None
    if allow_type:
        raw_type = _optional_string(table, "type", source, where)
        if raw_type is not None:
            try:
                database_type = DatabaseType(raw_type.strip().casefold())
            except ValueError:
                allowed = ", ".join(engine.value for engine in DatabaseType)
                raise ReconciliationError(
                    f"{source}: {where} type must be one of: {allowed} (got {raw_type!r})"
                ) from None
    else:
        # The target is always SQL Server; the schema does not offer the key.
        database_type = DatabaseType.SQLSERVER

    auth_mode: AuthMode | None = None
    raw_auth = _optional_string(table, "authentication", source, where)
    if raw_auth is not None:
        try:
            auth_mode = AuthMode(raw_auth.strip().casefold())
        except ValueError:
            allowed = ", ".join(mode.value for mode in AuthMode)
            raise ReconciliationError(
                f"{source}: {where} authentication must be one of: {allowed} (got {raw_auth!r})"
            ) from None

    username = _optional_string(table, "username", source, where)
    if auth_mode is AuthMode.WINDOWS and username:
        raise ReconciliationError(
            f'{source}: {where} sets authentication = "windows" and a username. '
            "Windows authentication uses the signed-in account; remove the username."
        )
    if auth_mode is AuthMode.WINDOWS and database_type is DatabaseType.ORACLE:
        raise ReconciliationError(
            f"{source}: {where} Windows authentication is available for SQL Server only."
        )

    return ConnectionProfile(
        database_type=database_type if allow_type else None,
        server=_optional_string(table, "server", source, where),
        port=_optional_port(table, "port", source, where),
        database=_optional_string(table, "database", source, where),
        trust_server_certificate=_optional_bool(table, "trust_server_certificate", source, where),
        auth_mode=auth_mode,
        username=username,
    )


def _reject_credentials(table: dict[str, Any], source: str, *, where: str) -> None:
    found = sorted(key for key in table if key.strip().casefold() in _FORBIDDEN_KEYS)
    if found:
        location = f" in {where}" if where else ""
        raise ReconciliationError(
            f"{source}: remove '{found[0]}'{location}. A profile must never contain a "
            "password or any other credential: passwords are typed at runtime, or avoided "
            'entirely with authentication = "windows".'
        )


def _table(document: dict[str, Any], key: str, source: str) -> dict[str, Any]:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise ReconciliationError(f"{source}: [{key}] must be a table")
    _reject_credentials(value, source, where=f"[{key}]")
    return value


def _reject_unknown(
    table: dict[str, Any], allowed: frozenset[str], source: str, *, where: str
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        location = f" in {where}" if where else ""
        raise ReconciliationError(
            f"{source}: unknown key(s){location}: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )


def _optional_string(table: dict[str, Any], key: str, source: str, where: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationError(f"{source}: {where} {key} must be a non-empty string")
    return value.strip()


def _optional_bool(table: dict[str, Any], key: str, source: str, where: str) -> bool | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, bool):
        raise ReconciliationError(f"{source}: {where} {key} must be true or false")
    return value


def _optional_port(table: dict[str, Any], key: str, source: str, where: str) -> int | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_PORT:
        raise ReconciliationError(
            f"{source}: {where} {key} must be a whole number between 1 and {MAX_PORT}"
        )
    return value
