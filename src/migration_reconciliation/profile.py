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
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .database.settings import AuthMode
from .errors import ReconciliationError
from .models import DatabaseType

__all__ = [
    "FORBIDDEN_KEY_NAMES",
    "ORACLE_CLIENT_MODES",
    "SUPPORTED_PROFILE_VERSIONS",
    "ConnectionProfile",
    "RunProfile",
    "load_profile",
    "parse_profile",
]

SUPPORTED_PROFILE_VERSIONS: frozenset[str] = frozenset({"1.0"})

_ALLOWED_TOP_LEVEL_KEYS = frozenset({"version", "workbook", "source", "target", "connections"})

#: ``[source]`` and ``[target]`` are the original two connections. They are kept
#: as names in their own right so every existing profile and workbook keeps
#: working: a cell saying "source" resolves the same way it always did.
_ALIAS_SECTIONS: tuple[str, ...] = ("source", "target")
_ALLOWED_WORKBOOK_KEYS = frozenset({"path", "sheet", "schema"})
_ALLOWED_SOURCE_KEYS = frozenset(
    {
        "type",
        "server",
        "port",
        "database",
        "trust_server_certificate",
        "authentication",
        "username",
        "oracle_client_mode",
        "oracle_client_dir",
    }
)
_ALLOWED_TARGET_KEYS = _ALLOWED_SOURCE_KEYS

#: Oracle's two client modes. ``thin`` needs no install but reaches only Oracle
#: Database 12.1 and later; ``thick`` loads the Oracle Client library and
#: reaches back to 9.2.
ORACLE_CLIENT_MODES: frozenset[str] = frozenset({"thin", "thick"})

#: Keys that must never appear, anywhere in the document, at any depth. Naming
#: them explicitly turns "I put the password in the file and it was ignored"
#: into a loud, immediate error. Compared after :func:`_normalize_key`, so
#: ``access_token``, ``accessToken`` and ``ACCESS-TOKEN`` are all the same key.
FORBIDDEN_KEY_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "pwd",
        "passwd",
        "pass",
        "secret",
        "secrets",
        "secretkey",
        "clientsecret",
        "token",
        "accesstoken",
        "authtoken",
        "bearertoken",
        "refreshtoken",
        "sastoken",
        "apikey",
        "apitoken",
        "credential",
        "credentials",
        "passphrase",
        "privatekey",
        "connectionstring",
    }
)

_FORBIDDEN_KEYS = FORBIDDEN_KEY_NAMES

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
    #: Oracle only. True selects thick mode, for servers older than 12.1.
    use_thick_client: bool | None = None
    #: Oracle thick mode only. Where the Oracle Client library lives.
    oracle_client_dir: str | None = None


@dataclass(frozen=True, slots=True)
class RunProfile:
    """Everything a profile file supplied. Absent answers stay ``None``."""

    source: ConnectionProfile
    target: ConnectionProfile
    #: Every connection this profile defines, by name. ``source`` and
    #: ``target`` always appear here too, so a lookup never has to know which
    #: spelling a profile used.
    connections: Mapping[str, ConnectionProfile] = field(default_factory=dict)
    workbook_path: Path | None = None
    sheet_name: str | None = None
    #: The workbook schema DSL to read the sheet with. ``None`` leaves the
    #: caller to decide what layout to assume.
    schema_path: Path | None = None
    #: Where it came from, for the "taken from the profile" messages.
    source_path: str = "<profile>"
    #: True when ``[target].type`` was absent and the source engine was reused.
    target_type_inherited: bool = False
    #: Non-fatal notes to show the person before anything is opened.
    warnings: tuple[str, ...] = ()

    def section(self, name: str) -> ConnectionProfile | None:
        """The connection table a workbook cell names, or ``None`` if unknown."""
        known = dict(self.connections) or {"source": self.source, "target": self.target}
        return known.get(name.strip().casefold())

    def section_names(self) -> tuple[str, ...]:
        """Every connection name this profile defines, for error messages."""
        known = dict(self.connections) or {"source": self.source, "target": self.target}
        return tuple(sorted(known))

    @classmethod
    def empty(cls) -> RunProfile:
        source, target = ConnectionProfile(), ConnectionProfile()
        return cls(
            source=source,
            target=target,
            connections={"source": source, "target": target},
        )


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


def _named_connections(document: dict[str, Any], source: str) -> dict[str, ConnectionProfile]:
    """Read ``[connections.<name>]``, if the profile defines any.

    A workbook that reconciles against more than two databases names them, and
    the profile answers with a table per name. Nothing here is special-cased:
    ``source`` and ``target`` are simply the two names every profile already
    has, so a file that defines neither section behaves exactly as before.
    """
    raw = document.get("connections")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ReconciliationError(f"{source}: [connections] must be a table of named tables.")

    named: dict[str, ConnectionProfile] = {}
    for key, table in raw.items():
        name = str(key).strip().casefold()
        where = f"[connections.{key}]"
        if not name:
            raise ReconciliationError(f"{source}: {where} has an empty connection name.")
        if name in _ALIAS_SECTIONS:
            raise ReconciliationError(
                f"{source}: {where} collides with the [{name}] section. Use one or the "
                f"other, so a workbook cell saying '{name}' has a single meaning."
            )
        if not isinstance(table, dict):
            raise ReconciliationError(f"{source}: {where} must be a table.")
        _reject_unknown(table, _ALLOWED_SOURCE_KEYS, source, where=where)
        named[name] = _connection(table, source, where)
    return named


def parse_profile(document: dict[str, Any], *, source: str = "<profile>") -> RunProfile:
    """Validate an already-parsed profile document."""
    _reject_credentials(document, source, where="")  # whole tree, before anything else
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
    schema_path = _optional_string(workbook, "schema", source, "[workbook]")

    source_table = _table(document, "source", source)
    _reject_unknown(source_table, _ALLOWED_SOURCE_KEYS, source, where="[source]")
    target_table = _table(document, "target", source)
    _reject_unknown(target_table, _ALLOWED_TARGET_KEYS, source, where="[target]")

    source_profile = _connection(source_table, source, "[source]")
    target_profile = _connection(target_table, source, "[target]")

    named = _named_connections(document, source)

    warnings: list[str] = []
    target_inherited = False
    if target_profile.database_type is None and source_profile.database_type is not None:
        # Older profiles omit [target].type entirely, because the target was
        # always SQL Server. Inheriting the source engine keeps the new
        # template's profiles working; the warning stops the inheritance from
        # becoming an invisible default. Windows authentication is the one
        # signal that overrides it: only SQL Server offers it, so inheriting
        # "oracle" there would contradict what the file already says.
        inherited = source_profile.database_type
        if target_profile.auth_mode is AuthMode.WINDOWS and inherited is not DatabaseType.SQLSERVER:
            inherited = DatabaseType.SQLSERVER
        target_profile = replace(target_profile, database_type=inherited)
        target_inherited = True
        warnings.append(
            f'{source}: [target] has no "type", so "{inherited.value}" is being used. '
            'Add an explicit type = "sqlserver" (or "oracle") to [target].'
        )

    if (
        target_profile.auth_mode is AuthMode.WINDOWS
        and target_profile.database_type is DatabaseType.ORACLE
    ):
        raise ReconciliationError(
            f"{source}: [target] Windows authentication is available for SQL Server only."
        )

    # Re-checked after inheritance: a [target] with no "type" has no engine yet
    # while _connection runs, so an Oracle-only key there escapes that pass.
    for label, section in (("[source]", source_profile), ("[target]", target_profile)):
        if (
            section.use_thick_client is not None or section.oracle_client_dir is not None
        ) and section.database_type is DatabaseType.SQLSERVER:
            raise ReconciliationError(
                f"{source}: {label} oracle_client_mode and oracle_client_dir apply to "
                'type = "oracle" only.'
            )

    return RunProfile(
        source=source_profile,
        target=target_profile,
        # The two original sections are names like any other, so a lookup never
        # has to know whether a profile spelled a connection [source] or
        # [connections.source].
        connections={"source": source_profile, "target": target_profile, **named},
        workbook_path=Path(workbook_path).expanduser() if workbook_path else None,
        sheet_name=sheet_name,
        schema_path=Path(schema_path).expanduser() if schema_path else None,
        source_path=source,
        target_type_inherited=target_inherited,
        warnings=tuple(warnings),
    )


def _connection(table: dict[str, Any], source: str, where: str) -> ConnectionProfile:
    database_type: DatabaseType | None = None
    raw_type = _optional_string(table, "type", source, where)
    if raw_type is not None:
        try:
            database_type = DatabaseType(raw_type.strip().casefold())
        except ValueError:
            allowed = ", ".join(engine.value for engine in DatabaseType)
            raise ReconciliationError(
                f"{source}: {where} type must be one of: {allowed} (got {raw_type!r})"
            ) from None

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

    use_thick_client = _optional_client_mode(table, source, where)
    oracle_client_dir = _optional_string(table, "oracle_client_dir", source, where)
    if (use_thick_client is not None or oracle_client_dir is not None) and (
        database_type is DatabaseType.SQLSERVER
    ):
        raise ReconciliationError(
            f"{source}: {where} oracle_client_mode and oracle_client_dir apply to "
            'type = "oracle" only.'
        )
    if oracle_client_dir is not None and use_thick_client is not True:
        raise ReconciliationError(
            f"{source}: {where} sets oracle_client_dir, which is only used in thick mode. "
            'Add oracle_client_mode = "thick", or remove the directory.'
        )

    return ConnectionProfile(
        database_type=database_type,
        server=_optional_string(table, "server", source, where),
        port=_optional_port(table, "port", source, where),
        database=_optional_string(table, "database", source, where),
        trust_server_certificate=_optional_bool(table, "trust_server_certificate", source, where),
        auth_mode=auth_mode,
        username=username,
        use_thick_client=use_thick_client,
        oracle_client_dir=oracle_client_dir,
    )


def _optional_client_mode(table: dict[str, Any], source: str, where: str) -> bool | None:
    """``oracle_client_mode`` as a boolean: True for thick, False for thin."""
    raw = _optional_string(table, "oracle_client_mode", source, where)
    if raw is None:
        return None
    mode = raw.strip().casefold()
    if mode not in ORACLE_CLIENT_MODES:
        allowed = ", ".join(sorted(ORACLE_CLIENT_MODES))
        raise ReconciliationError(
            f"{source}: {where} oracle_client_mode must be one of: {allowed} (got {raw!r})"
        )
    return mode == "thick"


def _normalize_key(key: str) -> str:
    """Fold a key to its comparable form: ``API-Key`` and ``api_key`` are one key."""
    return "".join(ch for ch in key.strip().casefold() if ch.isalnum())


def _reject_credentials(value: object, source: str, *, where: str) -> None:
    """Refuse any secret-like key, at any depth.

    A shallow check would let ``[source.extra] password = "..."`` through and
    then quietly ignore it, which is the worst of both worlds: the secret is on
    disk and the person believes it is being used.
    """
    if isinstance(value, dict):
        for key, nested in value.items():
            if _normalize_key(str(key)) in FORBIDDEN_KEY_NAMES:
                location = f" in {where}" if where else ""
                raise ReconciliationError(
                    f"{source}: remove '{key}'{location}. A profile must never contain a "
                    "password or any other credential: passwords are typed at runtime, or "
                    'avoided entirely with authentication = "windows". [FORBIDDEN_SECRET_KEY]'
                )
            _reject_credentials(nested, source, where=f"{where}.{key}" if where else f"[{key}]")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_credentials(item, source, where=f"{where}[{index}]")


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
