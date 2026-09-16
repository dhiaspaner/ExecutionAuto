"""Offline dialect checking: catch a query the engine cannot compile.

The database's own compiler is the authority on whether a query is valid, but
asking it costs a round trip per query, and the answer only arrives once a
connection is open. This module answers the cheapest part of that question
locally and instantly: SQL written for the *other* engine.

It is deliberately conservative and is **not** a SQL parser. It reports only
constructs that belong to one engine and cannot compile on the other, so a
finding here is a real problem rather than a guess. Everything it does not
know about is left for the database to judge — an unknown table, a misspelled
column and a subtly wrong expression are all still the server's call.

Comments and literals are blanked before scanning, so a word inside a string
never looks like syntax.
"""

from __future__ import annotations

import re

from ..models import DatabaseType
from ..security.sql_guard import strip_comments_and_literals

__all__ = ["describe_incompatibility"]

#: Constructs no SQL Server session can compile. Each entry is (pattern, what
#: to write instead) so a finding tells the author what to do about it.
_ORACLE_ONLY: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bFROM\s+DUAL\b", re.I), "FROM DUAL", "drop it; SQL Server needs no FROM"),
    (re.compile(r"\bNVL\s*\(", re.I), "NVL(", "use ISNULL( or COALESCE("),
    (re.compile(r"\bSYSDATE\b", re.I), "SYSDATE", "use GETDATE()"),
    (re.compile(r"\bROWNUM\b", re.I), "ROWNUM", "use TOP or ROW_NUMBER()"),
    (re.compile(r"\bTO_DATE\s*\(", re.I), "TO_DATE(", "use CONVERT( or CAST("),
    (re.compile(r"\bTO_CHAR\s*\(", re.I), "TO_CHAR(", "use CONVERT( or FORMAT("),
    (re.compile(r"\bTO_NUMBER\s*\(", re.I), "TO_NUMBER(", "use CAST( ... AS numeric)"),
    # After literals are blanked, DATE '2023-01-01' reads as DATE ''.
    (re.compile(r"\bDATE\s*''"), "DATE 'literal'", "use CAST('...' AS date)"),
    (re.compile(r"\(\s*\+\s*\)"), "(+) outer join", "use LEFT JOIN"),
)

#: Constructs no Oracle session can compile.
_SQLSERVER_ONLY: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bCOUNT_BIG\s*\(", re.I), "COUNT_BIG(", "use COUNT("),
    (re.compile(r"\bGETDATE\s*\(", re.I), "GETDATE()", "use SYSDATE"),
    (re.compile(r"\bISNULL\s*\(", re.I), "ISNULL(", "use NVL( or COALESCE("),
    (re.compile(r"\bDATEADD\s*\(", re.I), "DATEADD(", "use date arithmetic or INTERVAL"),
    (re.compile(r"\bDATEDIFF\s*\(", re.I), "DATEDIFF(", "subtract the dates"),
    (re.compile(r"\bSELECT\s+TOP\s+\d", re.I), "SELECT TOP n", "use FETCH FIRST n ROWS ONLY"),
    (re.compile(r"\bWITH\s*\(\s*NOLOCK\s*\)", re.I), "WITH (NOLOCK)", "drop it"),
)

# Bracketed identifiers ([dbo].[T]) are SQL Server only, but the stripper folds
# them to the same "q" it gives standard quoted identifiers, so they cannot be
# told apart here. Scanning the raw SQL instead would flag a bracket inside a
# string literal, and a false finding fails a correct query while a missed one
# is caught by the database a moment later.

_FOREIGN_TO: dict[DatabaseType, tuple[tuple[re.Pattern[str], str, str], ...]] = {
    DatabaseType.SQLSERVER: _ORACLE_ONLY,
    DatabaseType.ORACLE: _SQLSERVER_ONLY,
}

_OTHER_ENGINE = {
    DatabaseType.SQLSERVER: "Oracle",
    DatabaseType.ORACLE: "SQL Server",
}


def describe_incompatibility(sql: str, engine: DatabaseType) -> str | None:
    """Explain why ``sql`` cannot compile on ``engine``, or ``None``.

    ``None`` never means "this query is valid" — only that nothing recognisably
    belonging to the other engine was found. The database still has the last
    word.
    """
    patterns = _FOREIGN_TO.get(engine)
    if not patterns:
        return None

    scannable = strip_comments_and_literals(sql)
    found = [(name, advice) for pattern, name, advice in patterns if pattern.search(scannable)]
    if not found:
        return None

    named = ", ".join(f"{name} ({advice})" for name, advice in found[:3])
    more = f" and {len(found) - 3} more" if len(found) > 3 else ""
    return (
        f"{_OTHER_ENGINE[engine]} syntax in a query the {engine.value} connection must "
        f"compile: {named}{more}."
    )
