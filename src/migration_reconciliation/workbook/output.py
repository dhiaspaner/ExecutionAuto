"""Writing results back, safely.

Four guarantees, in the order they matter:

1. **Only output columns are written.** Cells are located by header name and
   filtered against :data:`~.columns.OUTPUT_COLUMNS`, so a definition column
   cannot be touched even by a caller that asks for it.
2. **The input survives.** Under ``NEW_FILE`` the result is a new workbook and
   the original bytes are never rewritten.
3. **A save is atomic.** Everything goes to a temporary file in the destination
   directory first and is moved into place only once the save has succeeded, so
   a crash mid-write cannot leave a half-written workbook where a result is
   expected.
4. **Everything else is preserved.** The workbook is opened with formulas,
   formatting, validation and unrelated sheets intact, and saved back the same
   way.

Nothing here decides anything. Statuses, variances and observations arrive
already computed; this module puts them in cells.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..errors import WorkbookError
from .columns import (
    OPTIONAL_OUTPUT_COLUMNS,
    OUTPUT_COLUMNS,
    RUN_HISTORY_COLUMNS,
    RUN_HISTORY_SHEET,
    normalize_header,
)
from .run_control import OutputMode
from .sheetio import find_sheet, map_headers

__all__ = ["TIMESTAMP_FORMAT", "ResultWriter", "build_output_path", "resolve_directory_env"]

#: Sortable, filename-safe, identical on Windows and POSIX.
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

_DATETIME_FORMAT = "yyyy-mm-dd hh:mm:ss"

#: Every column this module may write. The union is computed once so a caller
#: cannot widen it by passing an unexpected key.
_WRITABLE = {normalize_header(h) for h in (*OUTPUT_COLUMNS, *OPTIONAL_OUTPUT_COLUMNS)}


def resolve_directory_env(variable_name: str) -> Path | None:
    """Read a directory from the named environment variable, if it is set.

    The workbook names the *variable*, never the path, so a template committed
    to source control carries no machine-specific location.
    """
    if not variable_name:
        return None
    raw = os.environ.get(variable_name, "").strip()
    if not raw:
        return None
    directory = Path(raw).expanduser()
    if not directory.is_dir():
        raise WorkbookError(
            f"{variable_name} points at '{directory}', which is not an existing directory."
        )
    return directory


def build_output_path(
    input_path: Path, *, timestamp: datetime, run_id: str, output_dir: Path | None = None
) -> Path:
    """The new workbook's path: same stem, a timestamp, and the run id."""
    directory = output_dir if output_dir is not None else input_path.parent
    name = f"{input_path.stem}_results_{timestamp.strftime(TIMESTAMP_FORMAT)}.xlsx"
    candidate = (directory / name).resolve()
    if candidate == input_path.resolve() or candidate.exists():
        # Two runs inside one second, or a stem that already looks like output.
        candidate = candidate.with_name(f"{candidate.stem}_{run_id}{candidate.suffix}")
    return candidate


class ResultWriter:
    """One writable copy of the workbook, held open for the length of a run."""

    def __init__(self, input_path: str | Path, *, sheet_name: str, header_row: int) -> None:
        self._input_path = Path(input_path).resolve()
        self._header_row = header_row
        try:
            self._workbook = load_workbook(self._input_path)
        except Exception as exc:
            raise WorkbookError(
                f"Cannot open '{self._input_path.name}' for writing: {exc}"
            ) from None
        sheet = find_sheet(self._workbook, sheet_name)
        if sheet is None:  # pragma: no cover - the read pass already found it
            self.close()
            raise WorkbookError(f"Sheet '{sheet_name}' disappeared between reading and writing.")
        self._sheet = sheet
        self._columns = {
            header: index
            for header, index in map_headers([cell.value for cell in sheet[header_row]]).items()
            if header in _WRITABLE
        }

    @property
    def output_columns(self) -> tuple[str, ...]:
        """The output columns this workbook actually has, in template order."""
        return tuple(
            header
            for header in (*OUTPUT_COLUMNS, *OPTIONAL_OUTPUT_COLUMNS)
            if normalize_header(header) in self._columns
        )

    def clear_outputs(self, row_numbers: Iterable[int]) -> None:
        """Blank every output cell on the given rows.

        Stale results are worse than missing ones: a row that errors this time
        must not still be showing last week's PASS.
        """
        for row_number in row_numbers:
            for column in self._columns.values():
                self._sheet.cell(row=row_number, column=column).value = None

    def write_row(self, row_number: int, values: Mapping[str, Any]) -> None:
        """Write one row's outputs. Keys are output column header names."""
        for header, value in values.items():
            normalized = normalize_header(header)
            if normalized not in _WRITABLE:
                raise WorkbookError(
                    f"Refusing to write '{header}': it is not an execution-output column."
                )
            column = self._columns.get(normalized)
            if column is None:
                continue  # this workbook does not carry that output column
            cell = self._sheet.cell(row=row_number, column=column)
            cell.value = _cell_value(value)
            if isinstance(value, datetime):
                cell.number_format = _DATETIME_FORMAT

    def append_run_history(self, values: Mapping[str, Any]) -> None:
        """Add one row to ``Run History``, never touching the rows already there."""
        sheet = find_sheet(self._workbook, RUN_HISTORY_SHEET)
        if sheet is None:
            sheet = self._workbook.create_sheet(RUN_HISTORY_SHEET)
            for column, header in enumerate(RUN_HISTORY_COLUMNS, start=1):
                sheet.cell(row=1, column=column, value=header)
            header_row = 1
        else:
            header_row = _history_header_row(sheet)
        columns = map_headers([cell.value for cell in sheet[header_row]])

        row_number = max(sheet.max_row, header_row) + 1
        for header, value in values.items():
            column = columns.get(normalize_header(header))
            if column is None:
                continue
            cell = sheet.cell(row=row_number, column=column)
            cell.value = _cell_value(value)
            if isinstance(value, datetime):
                cell.number_format = _DATETIME_FORMAT

    def plan_destination(
        self,
        *,
        mode: OutputMode,
        timestamp: datetime,
        run_id: str,
        output_dir: Path | None = None,
    ) -> Path:
        """Where this run will land.

        Decided before anything is written, so the ``Run History`` row can name
        the file it belongs to instead of pointing at nothing.
        """
        if mode is OutputMode.IN_PLACE:
            return self._input_path
        return build_output_path(
            self._input_path, timestamp=timestamp, run_id=run_id, output_dir=output_dir
        )

    def save(self, destination: Path) -> Path:
        """Save atomically to ``destination`` and return it."""
        directory = destination.parent
        if not directory.is_dir():
            raise WorkbookError(f"Output directory does not exist: {directory}")

        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.", suffix=".xlsx.tmp", dir=directory
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            self._workbook.save(temporary)
            # Same directory, so this is a rename: either the old file or the
            # new one is there, never a partial one.
            os.replace(temporary, destination)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise WorkbookError(f"Cannot write '{destination}': {exc.strerror}") from None
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def close(self) -> None:
        """Release the workbook. Safe to call more than once."""
        workbook = getattr(self, "_workbook", None)
        if workbook is not None:
            # Cleanup must never mask the failure that triggered it.
            with contextlib.suppress(Exception):
                workbook.close()


def _history_header_row(sheet: Any) -> int:
    """Find the header row of an existing history sheet, defaulting to row 1."""
    wanted = normalize_header("Run_ID")
    for index, row in enumerate(sheet.iter_rows(max_row=5, values_only=True), start=1):
        if any(value is not None and normalize_header(value) == wanted for value in row):
            return index
    return 1


def _cell_value(value: Any) -> Any:
    """Convert a result value into something openpyxl can store."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        # Excel has no timezone concept; every timestamp here is UTC by
        # contract, so the offset is dropped rather than silently converted.
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, Decimal):
        return value if value.is_finite() else str(value)
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)
