"""Conservative, offline read-only SQL validation.

**This guard is a safety net, not a security boundary.** It is deliberately not
a SQL parser: it strips comments and string literals, then checks that what
remains is a single statement beginning with ``SELECT`` or ``WITH`` and free of
obvious write, DDL, procedural and transaction-control keywords.

A sufficiently exotic statement can slip past it. Production execution therefore
still requires a **read-only database account** — that is the real control. The
guard exists to catch mistakes (a pasted ``UPDATE``, a stray second statement)
before they ever reach a driver.

The validated SQL is never rewritten. Source and target SQL are already written
in their own dialects and are handed to the driver verbatim.
"""

from __future__ import annotations

import hashlib
import re

from ..errors import SqlValidationError

__all__ = ["assert_read_only", "sql_fingerprint", "strip_comments_and_literals"]

#: Tokens that must never appear in a reconciliation query.
_FORBIDDEN_TOKENS: dict[str, str] = {
    # Data modification
    "INSERT": "data modification",
    "UPDATE": "data modification",
    "DELETE": "data modification",
    "MERGE": "data modification",
    "UPSERT": "data modification",
    "INTO": "data modification (SELECT ... INTO writes a table)",
    # Schema modification
    "CREATE": "schema modification",
    "ALTER": "schema modification",
    "DROP": "schema modification",
    "TRUNCATE": "schema modification",
    "RENAME": "schema modification",
    "COMMENT": "schema modification",
    # Procedural execution
    "EXEC": "procedural execution",
    "EXECUTE": "procedural execution",
    "CALL": "procedural execution",
    "DECLARE": "procedural execution",
    "BEGIN": "procedural execution",
    "WAITFOR": "procedural execution",
    # Transaction control
    "COMMIT": "transaction control",
    "ROLLBACK": "transaction control",
    "SAVEPOINT": "transaction control",
    "TRANSACTION": "transaction control",
    "TRAN": "transaction control",
    # Session / privilege / administrative
    "SET": "session control",
    "USE": "session control",
    "GRANT": "privilege change",
    "REVOKE": "privilege change",
    "DENY": "privilege change",
    "BACKUP": "administrative command",
    "RESTORE": "administrative command",
    "SHUTDOWN": "administrative command",
    "KILL": "administrative command",
    "RECONFIGURE": "administrative command",
    # Known dangerous routines / external access
    "XP_CMDSHELL": "operating-system access",
    "SP_EXECUTESQL": "dynamic SQL execution",
    "OPENROWSET": "external data access",
    "OPENQUERY": "external data access",
    "OPENDATASOURCE": "external data access",
    "DBMS_SCHEDULER": "external data access",
    "UTL_FILE": "operating-system access",
    "UTL_HTTP": "network access",
}

_ALLOWED_FIRST_TOKENS = ("SELECT", "WITH")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
_MAX_SQL_LENGTH = 100_000


def assert_read_only(sql: str | None, *, label: str = "SQL") -> str:
    """Validate ``sql`` as a single read-only statement.

    Returns the original, unmodified SQL so callers can pass it straight to the
    driver. Raises :class:`SqlValidationError` with a message that quotes no
    query text beyond the offending keyword.
    """
    if sql is None or not str(sql).strip():
        raise SqlValidationError(f"{label} is empty")
    text = str(sql)
    if len(text) > _MAX_SQL_LENGTH:
        raise SqlValidationError(
            f"{label} is {len(text)} characters, above the {_MAX_SQL_LENGTH} limit"
        )

    stripped = strip_comments_and_literals(text)
    if not stripped.strip():
        raise SqlValidationError(f"{label} contains no executable statement")

    statements = [segment for segment in stripped.split(";") if segment.strip()]
    if len(statements) > 1:
        raise SqlValidationError(
            f"{label} contains {len(statements)} statements — exactly one read-only "
            f"statement is allowed"
        )

    first_match = _WORD_RE.search(statements[0])
    if first_match is None:
        raise SqlValidationError(f"{label} does not start with a SQL keyword")
    first_token = first_match.group(0).upper()
    if first_token not in _ALLOWED_FIRST_TOKENS:
        raise SqlValidationError(f"{label} must start with SELECT or WITH (found '{first_token}')")

    for match in _WORD_RE.finditer(statements[0]):
        token = match.group(0).upper()
        reason = _FORBIDDEN_TOKENS.get(token)
        if reason is not None:
            raise SqlValidationError(f"{label} contains forbidden keyword '{token}' ({reason})")

    return text


def strip_comments_and_literals(sql: str) -> str:
    """Remove comments and blank out literals/quoted identifiers.

    String literals become ``''`` and quoted identifiers become ``"q"`` so that
    keyword scanning never trips over data, and a semicolon inside a literal is
    never mistaken for a statement separator.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if ch == "-" and nxt == "-":
            i = _skip_to_end_of_line(sql, i)
        elif ch == "/" and nxt == "*":
            i = _skip_block_comment(sql, i)
        elif ch == "'":
            i = _skip_quoted(sql, i, "'")
            out.append("''")
        elif ch == '"':
            i = _skip_quoted(sql, i, '"')
            out.append('"q"')
        elif ch == "[":
            i = _skip_bracketed(sql, i)
            out.append('"q"')
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _skip_to_end_of_line(sql: str, i: int) -> int:
    end = sql.find("\n", i)
    return len(sql) if end == -1 else end + 1


def _skip_block_comment(sql: str, i: int) -> int:
    """Skip ``/* ... */``, honouring T-SQL's nested block comments."""
    depth = 0
    n = len(sql)
    while i < n:
        if sql.startswith("/*", i):
            depth += 1
            i += 2
        elif sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return n


def _skip_quoted(sql: str, i: int, quote: str) -> int:
    """Skip a quoted run starting at ``i``; a doubled quote is an escape."""
    n = len(sql)
    i += 1
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _skip_bracketed(sql: str, i: int) -> int:
    """Skip a T-SQL ``[identifier]``; ``]]`` is an escaped bracket."""
    n = len(sql)
    i += 1
    while i < n:
        if sql[i] == "]":
            if i + 1 < n and sql[i + 1] == "]":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def sql_fingerprint(sql: str) -> str:
    """Stable, non-reversible reference to a query, safe for logs and remarks."""
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"
