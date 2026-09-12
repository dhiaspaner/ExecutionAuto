"""Offline read-only SQL validation."""

from __future__ import annotations

import pytest

from migration_reconciliation.errors import SqlValidationError
from migration_reconciliation.security.sql_guard import (
    assert_read_only,
    sql_fingerprint,
    strip_comments_and_literals,
)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select count(*) from payments",
        "   \n\t SELECT COUNT(*) FROM PAYMENTS",
        "-- daily count\nSELECT COUNT(*) FROM PAYMENTS",
        "/* owner: migration team */ SELECT COUNT(*) FROM PAYMENTS",
        "/* nested /* comment */ */ SELECT 1",
        "WITH c AS (SELECT id FROM payments) SELECT COUNT(*) FROM c",
        "SELECT COUNT(*) FROM PAYMENTS;",
        "SELECT COUNT(*) FROM PAYMENTS WHERE STATUS = 'DELETED'",
        "SELECT COUNT(*) FROM [dbo].[Payments]",
        'SELECT COUNT(*) FROM "SCHEMA"."PAYMENTS"',
        "SELECT SUM(amount) FROM payments WHERE note = 'insert; drop table t'",
    ],
)
def test_accepts_read_only_statements(sql: str) -> None:
    assert assert_read_only(sql) == sql, "validated SQL must be returned unmodified"


@pytest.mark.parametrize("sql", ["", "   ", "\n\t ", None])
def test_rejects_empty_sql(sql: str | None) -> None:
    with pytest.raises(SqlValidationError, match="is empty"):
        assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    ["-- nothing here", "/* just a comment */", "-- one\n-- two\n"],
)
def test_rejects_comment_only_sql(sql: str) -> None:
    with pytest.raises(SqlValidationError, match="no executable statement"):
        assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO payments VALUES (1)",
        "UPDATE payments SET amount = 0",
        "DELETE FROM payments",
        "MERGE INTO payments USING staging ON (1=1)",
        "DROP TABLE payments",
        "ALTER TABLE payments ADD col INT",
        "CREATE TABLE t (id INT)",
        "TRUNCATE TABLE payments",
        "EXEC sp_who",
        "EXECUTE some_proc",
        "CALL some_proc()",
        "BEGIN TRANSACTION",
        "COMMIT",
        "ROLLBACK",
        "GRANT SELECT ON payments TO analyst",
        "USE master",
        "SET NOCOUNT ON",
        "BACKUP DATABASE prod TO DISK = 'x'",
    ],
)
def test_rejects_write_ddl_and_procedural_statements(sql: str) -> None:
    with pytest.raises(SqlValidationError):
        assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT COUNT(*) FROM t; DELETE FROM t",
        "SELECT 1;DROP TABLE t;",
        "SELECT 1; -- trailing\nSELECT 2",
    ],
)
def test_rejects_multiple_statements(sql: str) -> None:
    with pytest.raises(SqlValidationError, match="statements"):
        assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) INTO #tmp FROM payments",
        "SELECT * INTO backup_table FROM payments",
    ],
)
def test_rejects_select_into_which_creates_a_table(sql: str) -> None:
    with pytest.raises(SqlValidationError, match="INTO"):
        assert_read_only(sql)


def test_rejects_statement_hidden_behind_a_leading_comment() -> None:
    with pytest.raises(SqlValidationError, match="must start with SELECT or WITH"):
        assert_read_only("/* harmless */ DELETE FROM payments")


def test_keyword_inside_an_identifier_is_not_flagged() -> None:
    assert_read_only("SELECT update_count, created_by FROM audit_updates")


def test_error_message_does_not_echo_the_query() -> None:
    secret_sql = "DELETE FROM payments WHERE card_number = '4111111111111111'"

    with pytest.raises(SqlValidationError) as excinfo:
        assert_read_only(secret_sql)

    assert "4111111111111111" not in str(excinfo.value)


def test_label_identifies_the_offending_side() -> None:
    with pytest.raises(SqlValidationError, match="Target query is empty"):
        assert_read_only("", label="Target query")


def test_rejects_oversized_sql() -> None:
    with pytest.raises(SqlValidationError, match="above the"):
        assert_read_only("SELECT 1 " + "-- pad\n" * 20000)


def test_strip_comments_and_literals_blanks_out_data() -> None:
    stripped = strip_comments_and_literals(
        "SELECT 'DROP TABLE t' /* c */ FROM [weird;name] -- tail\n"
    )

    assert "DROP" not in stripped
    assert ";" not in stripped
    assert "c" not in stripped.replace("SELECT", "").replace("FROM", "")


def test_fingerprint_is_stable_and_non_reversible() -> None:
    sql = "SELECT COUNT(*) FROM PAYMENTS"

    fingerprint = sql_fingerprint(sql)

    assert fingerprint == sql_fingerprint(sql)
    assert fingerprint.startswith("sha256:")
    assert "PAYMENTS" not in fingerprint
