"""Small helpers shared by every reader of the reconciliation template.

The template is authored by people, so two things are assumed rather than
demanded: a sheet may be named with different capitalisation, and its header
row may not be row 1 because a title or a note sits above it. Both are found by
looking, not by configuration — there is no header-row setting to get wrong.

Nothing here interprets a cell's meaning. These functions locate sheets, locate
the header row, and map header text to column numbers; every judgement about
what a value means belongs to the module that owns that sheet.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .columns import normalize_header

__all__ = [
    "MAX_HEADER_SEARCH_ROWS",
    "cell",
    "find_sheet",
    "is_blank_row",
    "locate_header_row",
    "map_headers",
    "sheet_rows",
    "text_of",
]

#: How far down a sheet to look for the header row. Deep enough for a title
#: block, shallow enough that a stray word in the data can never win.
MAX_HEADER_SEARCH_ROWS = 20


def find_sheet(workbook: Any, name: str) -> Any | None:
    """Return the worksheet called ``name``, matched case-insensitively."""
    wanted = normalize_header(name)
    for title in workbook.sheetnames:
        if normalize_header(title) == wanted:
            return workbook[title]
    return None


def sheet_rows(sheet: Any) -> list[tuple[Any, ...]]:
    """Every row as a tuple of values, one pass, no formatting."""
    return [tuple(row) for row in sheet.iter_rows(values_only=True)]


def locate_header_row(
    rows: Sequence[tuple[Any, ...]],
    expected: Iterable[str],
    *,
    must_contain: str | None = None,
) -> int | None:
    """Find the 1-based row that carries the sheet's headers.

    The best row wins: the one matching most of ``expected``. ``must_contain``
    names a header the row cannot be without, which stops a data row that
    happens to echo a header word from being mistaken for the header.
    """
    wanted = {normalize_header(header) for header in expected}
    required = normalize_header(must_contain) if must_contain else None

    best_row: int | None = None
    best_score = 0
    for index, row in enumerate(rows[:MAX_HEADER_SEARCH_ROWS], start=1):
        present = {normalize_header(value) for value in row if value is not None}
        if required is not None and required not in present:
            continue
        score = len(present & wanted)
        if score > best_score:
            best_row, best_score = index, score
    return best_row if best_score else None


def map_headers(row: Sequence[Any]) -> dict[str, int]:
    """Map normalized header text to its 1-based column number.

    The first occurrence wins. A template that repeats a header is a template
    mistake, but silently writing into the second copy would be worse than
    writing into the first.
    """
    headers: dict[str, int] = {}
    for index, value in enumerate(row, start=1):
        if value is None:
            continue
        normalized = normalize_header(value)
        if normalized and normalized not in headers:
            headers[normalized] = index
    return headers


def cell(row: Sequence[Any], column: int | None) -> Any:
    """One cell by 1-based column number, tolerating a short row."""
    if column is None or column > len(row):
        return None
    return row[column - 1]


def text_of(value: Any) -> str:
    """A trimmed string for a cell that is meant to hold text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def is_blank_row(row: Sequence[Any]) -> bool:
    return all(value is None or (isinstance(value, str) and not value.strip()) for value in row)
