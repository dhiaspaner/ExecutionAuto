"""Run-profile parsing, and the credential keys it must refuse."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from migration_reconciliation.database.settings import AuthMode
from migration_reconciliation.errors import ReconciliationError
from migration_reconciliation.models import DatabaseType
from migration_reconciliation.profile import RunProfile, load_profile, parse_profile

EXAMPLE_PROFILE_PATH = Path(__file__).resolve().parents[2] / "config" / "run_profile.example.toml"


def document(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": "1.0",
        "workbook": {"path": "C:/data/payments.xlsx", "sheet": "Payments"},
        "source": {
            "type": "oracle",
            "server": "legacy-ora.corp.local",
            "port": 1521,
            "database": "LEGACYPAY",
            "username": "recon_reader",
        },
        "target": {
            "server": "sql-mig-01.corp.local",
            "database": "PaymentsMigrated",
            "trust_server_certificate": True,
            "authentication": "windows",
        },
    }
    base.update(overrides)
    return base


def test_a_full_profile_parses() -> None:
    profile = parse_profile(document())

    assert profile.sheet_name == "Payments"
    assert profile.source.database_type is DatabaseType.ORACLE
    assert profile.source.port == 1521
    assert profile.source.username == "recon_reader"
    assert profile.target.auth_mode is AuthMode.WINDOWS
    assert profile.target.trust_server_certificate is True


def test_the_workbook_table_may_name_a_schema_file() -> None:
    profile = parse_profile(
        document(
            workbook={
                "path": "C:/data/payments.xlsx",
                "sheet": "Payments",
                "schema": "~/schemas/payments.toml",
            }
        )
    )

    assert profile.schema_path == Path("~/schemas/payments.toml").expanduser()


def test_a_profile_without_a_schema_leaves_the_layout_undecided() -> None:
    assert parse_profile(document()).schema_path is None


def test_an_empty_profile_answers_nothing() -> None:
    profile = RunProfile.empty()

    assert profile.workbook_path is None
    assert profile.source.server is None
    assert profile.target.auth_mode is None


def test_every_section_is_optional() -> None:
    profile = parse_profile({"version": "1.0"})

    assert profile.workbook_path is None
    assert profile.source.database_type is None


def test_the_shipped_example_is_valid() -> None:
    """If the template ever stops parsing, this test says so."""
    profile = load_profile(EXAMPLE_PROFILE_PATH)

    assert profile.source.server is not None
    assert profile.target.server is not None


def test_the_shipped_example_contains_no_credential_key() -> None:
    raw = tomllib.loads(EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8"))
    flattened = [
        key.casefold() for table in raw.values() if isinstance(table, dict) for key in table
    ]

    assert "password" not in flattened
    assert "pwd" not in flattened


# -- credentials are refused, never ignored ------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "pwd",
        "passwd",
        "secret",
        "token",
        "credential",
        "access_token",
        "api_key",
        "apikey",
        "API-KEY",
        "clientSecret",
        "connection_string",
    ],
)
def test_a_credential_key_is_rejected(key: str) -> None:
    bad = document()
    bad["source"][key] = "hunter2"

    with pytest.raises(ReconciliationError, match="never contain a password"):
        parse_profile(bad)


def test_a_top_level_credential_key_is_rejected() -> None:
    bad = document(password="hunter2")

    with pytest.raises(ReconciliationError, match="never contain a password"):
        parse_profile(bad)


def test_a_credential_key_is_rejected_at_any_depth() -> None:
    """A nested secret that was merely ignored would still be a secret on disk."""
    bad = document()
    bad["source"]["extra"] = {"deeper": {"password": "hunter2"}}

    with pytest.raises(ReconciliationError, match="never contain a password"):
        parse_profile(bad)


def test_a_credential_key_inside_an_array_of_tables_is_rejected() -> None:
    bad = document()
    bad["logins"] = [{"name": "svc", "api_key": "abc123"}]

    with pytest.raises(ReconciliationError, match="never contain a password"):
        parse_profile(bad)


def test_password_is_allowed_as_a_value_because_it_names_a_mode() -> None:
    """`authentication = "password"` is a choice, not a credential."""
    with_password_auth = document()
    with_password_auth["source"]["authentication"] = "password"

    profile = parse_profile(with_password_auth)

    assert profile.source.auth_mode is AuthMode.PASSWORD


def test_the_rejection_names_the_error_code() -> None:
    bad = document()
    bad["source"]["password"] = "hunter2"

    with pytest.raises(ReconciliationError, match="FORBIDDEN_SECRET_KEY"):
        parse_profile(bad)


def test_windows_authentication_with_a_username_is_rejected() -> None:
    bad = document()
    bad["target"]["username"] = "svc_recon"

    with pytest.raises(ReconciliationError, match="remove the username"):
        parse_profile(bad)


def test_windows_authentication_on_an_oracle_source_is_rejected() -> None:
    bad = document()
    bad["source"]["authentication"] = "windows"
    del bad["source"]["username"]

    with pytest.raises(ReconciliationError, match="SQL Server only"):
        parse_profile(bad)


# -- ordinary validation -------------------------------------------------


def test_an_unsupported_version_is_rejected() -> None:
    with pytest.raises(ReconciliationError, match="version must be one of"):
        parse_profile(document(version="9.9"))


def test_a_missing_version_is_rejected() -> None:
    bad = document()
    del bad["version"]

    with pytest.raises(ReconciliationError, match="version must be one of"):
        parse_profile(bad)


def test_an_unknown_key_is_rejected() -> None:
    bad = document()
    bad["source"]["timeout"] = 30

    with pytest.raises(ReconciliationError, match="unknown key"):
        parse_profile(bad)


def test_the_target_may_declare_its_own_engine() -> None:
    """The new template states the target engine explicitly."""
    both_sqlserver = document()
    both_sqlserver["source"]["type"] = "sqlserver"
    both_sqlserver["target"]["type"] = "sqlserver"

    profile = parse_profile(both_sqlserver)

    assert profile.target.database_type is DatabaseType.SQLSERVER
    assert profile.target_type_inherited is False
    assert profile.warnings == ()


def test_a_missing_target_engine_is_inherited_with_a_warning() -> None:
    inheriting = document()
    inheriting["source"]["type"] = "sqlserver"
    inheriting["target"].pop("type", None)

    profile = parse_profile(inheriting)

    assert profile.target.database_type is DatabaseType.SQLSERVER
    assert profile.target_type_inherited is True
    assert any('no "type"' in warning for warning in profile.warnings)


def test_windows_authentication_overrides_an_inherited_oracle_engine() -> None:
    """Only SQL Server offers Windows authentication, so it settles the engine."""
    profile = parse_profile(document())  # oracle source, target without a type

    assert profile.source.database_type is DatabaseType.ORACLE
    assert profile.target.database_type is DatabaseType.SQLSERVER
    assert profile.target_type_inherited is True


def test_an_unknown_database_type_is_rejected() -> None:
    bad = document()
    bad["source"]["type"] = "postgres"

    with pytest.raises(ReconciliationError, match="type must be one of"):
        parse_profile(bad)


def test_an_unknown_authentication_mode_is_rejected() -> None:
    bad = document()
    bad["target"]["authentication"] = "kerberos"

    with pytest.raises(ReconciliationError, match="authentication must be one of"):
        parse_profile(bad)


@pytest.mark.parametrize("port", [0, 70000, -1, "1433", True])
def test_an_invalid_port_is_rejected(port: Any) -> None:
    bad = document()
    bad["source"]["port"] = port

    with pytest.raises(ReconciliationError, match="between 1 and 65535"):
        parse_profile(bad)


def test_an_empty_string_is_rejected() -> None:
    bad = document()
    bad["source"]["server"] = "   "

    with pytest.raises(ReconciliationError, match="must be a non-empty string"):
        parse_profile(bad)


def test_a_non_boolean_certificate_answer_is_rejected() -> None:
    bad = document()
    bad["target"]["trust_server_certificate"] = "yes"

    with pytest.raises(ReconciliationError, match="must be true or false"):
        parse_profile(bad)


def test_a_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(ReconciliationError, match="Cannot read profile"):
        load_profile(tmp_path / "nope.toml")


def test_invalid_toml_is_reported_clearly(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('version = "1.0"\n[source\n', encoding="utf-8")

    with pytest.raises(ReconciliationError, match="not valid TOML"):
        load_profile(bad)


# -- Oracle client mode --------------------------------------------------


def test_thick_mode_is_read_from_the_profile() -> None:
    doc = document()
    doc["source"]["oracle_client_mode"] = "thick"
    doc["source"]["oracle_client_dir"] = "/opt/oracle/instantclient_19_8"

    profile = parse_profile(doc)

    assert profile.source.use_thick_client is True
    assert profile.source.oracle_client_dir == "/opt/oracle/instantclient_19_8"


def test_thin_mode_is_read_from_the_profile() -> None:
    doc = document()
    doc["source"]["oracle_client_mode"] = "thin"

    assert parse_profile(doc).source.use_thick_client is False


def test_an_absent_client_mode_stays_unset() -> None:
    assert parse_profile(document()).source.use_thick_client is None


def test_an_unknown_client_mode_is_rejected() -> None:
    doc = document()
    doc["source"]["oracle_client_mode"] = "fat"

    with pytest.raises(ReconciliationError, match="oracle_client_mode must be one of"):
        parse_profile(doc)


def test_a_client_mode_on_a_sql_server_section_is_rejected() -> None:
    doc = document()
    doc["target"]["oracle_client_mode"] = "thick"

    with pytest.raises(ReconciliationError, match='type = "oracle" only'):
        parse_profile(doc)


def test_a_client_directory_without_thick_mode_is_rejected() -> None:
    """A directory that would never be loaded is a mistake worth naming."""
    doc = document()
    doc["source"]["oracle_client_dir"] = "/opt/oracle/instantclient_19_8"

    with pytest.raises(ReconciliationError, match="only used in thick mode"):
        parse_profile(doc)
