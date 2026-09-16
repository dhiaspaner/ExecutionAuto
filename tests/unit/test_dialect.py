"""Offline dialect checking: catching the other engine's SQL without a database."""

from __future__ import annotations

import pytest

from migration_reconciliation.database.dialect import describe_incompatibility
from migration_reconciliation.models import DatabaseType

ORACLE = DatabaseType.ORACLE
SQLSERVER = DatabaseType.SQLSERVER


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 0 FROM DUAL",
        "SELECT NVL(SUM(AMOUNT), 0) FROM PAYMENTS",
        "SELECT COUNT(*) FROM T WHERE CREATED >= SYSDATE",
        "SELECT COUNT(*) FROM T WHERE ROWNUM < 10",
        "SELECT TO_DATE('2023-01-01', 'YYYY-MM-DD') FROM T",
        "SELECT COUNT(*) FROM V WHERE CREATED_DATE >= DATE '2023-01-01'",
        "SELECT COUNT(*) FROM A, B WHERE A.ID = B.ID (+)",
    ],
)
def test_oracle_syntax_is_refused_for_a_sql_server_connection(sql: str) -> None:
    reason = describe_incompatibility(sql, SQLSERVER)

    assert reason is not None
    assert "Oracle syntax" in reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT_BIG(*) FROM dbo.Payments",
        "SELECT GETDATE()",
        "SELECT ISNULL(Amount, 0) FROM dbo.T",
        "SELECT DATEADD(day, 1, Created) FROM dbo.T",
        "SELECT TOP 1 Id FROM dbo.T",
        "SELECT COUNT(*) FROM dbo.T WITH (NOLOCK)",
    ],
)
def test_sql_server_syntax_is_refused_for_an_oracle_connection(sql: str) -> None:
    reason = describe_incompatibility(sql, ORACLE)

    assert reason is not None
    assert "SQL Server syntax" in reason


@pytest.mark.parametrize(
    ("sql", "engine"),
    [
        ("SELECT COUNT(*) FROM dbo.Payments", SQLSERVER),
        ("SELECT COUNT(*) FROM PAYMENTS", ORACLE),
        ("SELECT SUM(Amount) FROM T WHERE Status = 'POSTED'", SQLSERVER),
    ],
)
def test_portable_sql_is_left_for_the_database_to_judge(sql: str, engine: DatabaseType) -> None:
    """No finding never means "valid" — only that nothing foreign was spotted."""
    assert describe_incompatibility(sql, engine) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM T WHERE note = 'computed from SYSDATE'",
        "SELECT COUNT(*) FROM T WHERE note = 'see FROM DUAL in the spec'",
        "SELECT COUNT(*) FROM T -- NVL( was used in the legacy query\n",
        "SELECT COUNT(*) FROM T /* TO_DATE( */",
    ],
)
def test_a_word_inside_a_literal_or_comment_is_not_syntax(sql: str) -> None:
    """Scanning data as if it were code would fail a correct query."""
    assert describe_incompatibility(sql, SQLSERVER) is None


def test_several_findings_are_named_together() -> None:
    sql = "SELECT NVL(X, 0) FROM DUAL WHERE C >= SYSDATE"

    reason = describe_incompatibility(sql, SQLSERVER)

    assert reason is not None
    for expected in ("FROM DUAL", "NVL(", "SYSDATE"):
        assert expected in reason


def test_bracketed_identifiers_are_left_to_the_database() -> None:
    """Deliberately not flagged: brackets are indistinguishable after stripping.

    Recorded as a test so the gap is a decision on the record rather than an
    oversight someone later "fixes" into a false-positive.
    """
    assert describe_incompatibility("SELECT COUNT(*) FROM [dbo].[Payments]", ORACLE) is None


def test_a_finding_says_what_to_write_instead() -> None:
    reason = describe_incompatibility("SELECT NVL(X, 0) FROM T", SQLSERVER)

    assert reason is not None
    assert "ISNULL(" in reason or "COALESCE(" in reason
