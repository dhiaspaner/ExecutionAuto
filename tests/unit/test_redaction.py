"""Credential redaction in anything user-visible."""

from __future__ import annotations

import pytest

from migration_reconciliation.security.redaction import MASK, sanitize_error, sanitize_text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("DRIVER={ODBC Driver 18 for SQL Server};SERVER=prod;UID=svc;PWD=Hunter2;", "Hunter2"),
        ("Login failed for password=S3cret!", "S3cret!"),
        ("connect using PWD = 'quoted secret'", "quoted secret"),
        ('{"username": "admin", "password": "letmein"}', "letmein"),
        ("ORA-01017 invalid username/password for scott/tiger@PRODDB", "tiger"),
        ("Data Source=prod-sql-01;Initial Catalog=Payments", "prod-sql-01"),
    ],
)
def test_masks_credentials_and_hosts(text: str, secret: str) -> None:
    cleaned = sanitize_text(text)

    assert secret not in cleaned
    assert MASK in cleaned


def test_keeps_the_useful_part_of_a_message() -> None:
    cleaned = sanitize_text("ORA-00942: table or view does not exist")

    assert cleaned == "ORA-00942: table or view does not exist"


def test_sanitize_error_keeps_the_exception_type() -> None:
    cleaned = sanitize_error(RuntimeError("failed with pwd=topsecret"))

    assert cleaned.startswith("RuntimeError: ")
    assert "topsecret" not in cleaned


def test_sanitize_error_handles_an_empty_message() -> None:
    assert sanitize_error(ValueError()) == "ValueError"


def test_collapses_newlines_so_a_cell_stays_readable() -> None:
    cleaned = sanitize_text("line one\nline two\r\n\tline three")

    assert cleaned == "line one line two line three"


def test_truncates_long_driver_payloads() -> None:
    cleaned = sanitize_text("x" * 5000)

    assert len(cleaned) <= 300
    assert cleaned.endswith("...")


def test_empty_input_stays_empty() -> None:
    assert sanitize_text("") == ""
