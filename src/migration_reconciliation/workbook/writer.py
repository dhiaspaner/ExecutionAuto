"""Writes results into a new, timestamped copy of the workbook.

Three guarantees this module enforces:

1. The input workbook is opened and saved **elsewhere**. Its bytes are never
   rewritten, and an output path that resolves to the input is refused.
2. An existing file is never overwritten. If the timestamped name is already
   taken — two runs inside the same second — the run id disambiguates it; only
   a collision on both names is an error.
3. Only cells belonging to fields declared ``write = true`` are touched. Every
   other cell — and every other sheet — is left exactly as it was.

Status and variance are computed in Python and written as literal values, so a
result workbook is correct the moment it is produced, with no dependence on
Excel recalculating anything.

A run keeps one :class:`ResultWorkbook` open and saves it again after every
case, so the file grows while the run is still going. Each save goes to a
temporary file that then replaces the result, so an interrupted run leaves the
last complete save — never a half-written workbook.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..errors import WorkbookError
from ..models import (
    ErrorSide,
    ExecutionResult,
    FieldDefinition,
    FieldType,
    WorkbookSchema,
)

__all__ = ["TIMESTAMP_FORMAT", "ResultWorkbook", "build_output_path", "write_results"]

#: Sortable, filename-safe, and identical on Windows and POSIX.
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

_UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def build_output_path(
    input_path: Path,
    schema: WorkbookSchema,
    *,
    timestamp: datetime,
    run_id: str,
    output_dir: Path | None = None,
) -> Path:
    """Render the schema's filename pattern into a concrete output path."""
    filename = schema.output_filename_pattern.format(
        input_stem=input_path.stem,
        timestamp=timestamp.strftime(TIMESTAMP_FORMAT),
        run_id=run_id,
        profile_name=schema.profile_name,
    )
    # The pattern is validated at schema load; the substituted values are
    # scrubbed here so a stem or run id can never introduce a path separator.
    filename = _UNSAFE_FILENAME_CHARS.sub("_", filename)
    directory = output_dir if output_dir is not None else input_path.parent
    return (directory / filename).resolve()


def write_results(
    input_path: str | Path,
    schema: WorkbookSchema,
    results: Iterable[ExecutionResult],
    *,
    run_id: str,
    timestamp: datetime,
    output_dir: str | Path | None = None,
) -> Path:
    """Write ``results`` into a fresh copy of the workbook and return its path."""
    result_workbook = ResultWorkbook(
        input_path, schema, run_id=run_id, timestamp=timestamp, output_dir=output_dir
    )
    try:
        result_workbook.write(results)
        return result_workbook.save()
    finally:
        result_workbook.close()


class ResultWorkbook:
    """A result copy of the workbook, held open for a run and saved as it fills.

    The output path is chosen, and the sheet and result columns checked, when
    this is created — before any query runs. The first :meth:`save` refuses to
    replace a file that already exists; later saves replace only this run's own
    result file.
    """

    def __init__(
        self,
        input_path: str | Path,
        schema: WorkbookSchema,
        *,
        run_id: str,
        timestamp: datetime,
        output_dir: str | Path | None = None,
    ) -> None:
        self._schema = schema
        self._run_id = run_id
        self._saved = False

        source_path = Path(input_path).resolve()
        if not source_path.is_file():
            raise WorkbookError(f"Workbook not found: {source_path}")

        directory = Path(output_dir).resolve() if output_dir is not None else None
        output_path = build_output_path(
            source_path, schema, timestamp=timestamp, run_id=run_id, output_dir=directory
        )
        if output_path == source_path:
            raise WorkbookError(
                "Refusing to write results over the input workbook. Check "
                "output_filename_pattern in the schema."
            )
        if directory is not None and not directory.is_dir():
            raise WorkbookError(f"Output directory does not exist: {directory}")
        self.path = _resolve_free_path(output_path, run_id)

        try:
            self._workbook = load_workbook(source_path)
        except Exception as exc:
            raise WorkbookError(f"Cannot open workbook '{source_path.name}': {exc}") from None

        try:
            if schema.sheet_name not in self._workbook.sheetnames:
                available = ", ".join(self._workbook.sheetnames) or "<none>"
                raise WorkbookError(
                    f"Sheet '{schema.sheet_name}' not found. Available sheets: {available}"
                )
            self._sheet = self._workbook[schema.sheet_name]
            self._column_map = _map_writable_columns(self._sheet, schema)

            if not schema.preserve_other_sheets:
                for name in list(self._workbook.sheetnames):
                    if name != schema.sheet_name:
                        del self._workbook[name]
        except BaseException:
            self.close()
            raise

    def write(self, results: Iterable[ExecutionResult]) -> None:
        """Put results into their rows. Nothing reaches disk until :meth:`save`."""
        for result in results:
            _write_row(self._sheet, self._schema, self._column_map, result)

    def save(self) -> Path:
        """Save everything written so far, atomically, and return the path."""
        if not self._saved:
            # Re-checked at the last moment: another run may have taken the name.
            self.path = _resolve_free_path(self.path, self._run_id)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.stem}.", suffix=".xlsx.tmp", dir=self.path.parent
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            self._workbook.save(temporary)
            # Same directory, so this is a rename: either the previous save or
            # this one is in place, never a partial file.
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise WorkbookError(f"Cannot write '{self.path}': {exc.strerror}") from None
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self._saved = True
        return self.path

    def save_to_new_file(self) -> Path:
        """Save under a fresh name, for when this run's file cannot be replaced.

        On Windows a result workbook opened in Excel is locked, so replacing it
        fails. Losing a finished run over that would be the worst outcome.
        """
        self.path = _next_free_path(self.path)
        self._saved = False
        return self.save()

    def close(self) -> None:
        """Release the workbook. Safe to call more than once."""
        workbook = getattr(self, "_workbook", None)
        if workbook is not None:
            # Cleanup must never mask the failure that triggered it.
            with contextlib.suppress(Exception):
                workbook.close()


def _resolve_free_path(output_path: Path, run_id: str) -> Path:
    """Return a path that does not exist yet, without ever overwriting.

    The default filename pattern is timestamped to the second, so two quick runs
    — a ``--case`` pilot followed by another — can land on the same name. The
    results are already computed by this point, so failing would discard a
    finished run over a filename. The run id disambiguates instead; only a
    genuine collision on *both* names is an error.
    """
    if not output_path.exists():
        return output_path
    if run_id:
        candidate = output_path.with_name(f"{output_path.stem}_{run_id}{output_path.suffix}")
        if not candidate.exists():
            return candidate
    raise WorkbookError(f"Refusing to overwrite existing file: {output_path}")


def _next_free_path(path: Path) -> Path:
    """``name_2.xlsx``, ``name_3.xlsx``, ... — the first that does not exist."""
    for number in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise WorkbookError(f"Refusing to overwrite existing file: {path}")


def _map_writable_columns(sheet: Any, schema: WorkbookSchema) -> dict[str, int]:
    """Find the column for every writable field, by header text."""
    headers: dict[str, int] = {}
    for cell in sheet[schema.header_row]:
        if cell.value is None:
            continue
        normalized = " ".join(str(cell.value).split()).casefold()
        if normalized and normalized not in headers:
            headers[normalized] = cell.column

    column_map: dict[str, int] = {}
    missing: list[str] = []
    for definition in schema.writable_fields():
        index = headers.get(" ".join(definition.header.split()).casefold())
        if index is None:
            if definition.required:
                missing.append(f"{definition.header} ({definition.name})")
            continue
        column_map[definition.name] = index

    if missing:
        raise WorkbookError(
            f"Sheet '{schema.sheet_name}' is missing required result column(s): "
            f"{', '.join(missing)}"
        )
    return column_map


def _write_row(
    sheet: Any,
    schema: WorkbookSchema,
    column_map: Mapping[str, int],
    result: ExecutionResult,
) -> None:
    values: dict[str, Any] = {
        "source_result": result.source_result,
        "target_result": result.target_result,
        "variance": result.variance,
        "status": result.status.value,
        "remarks": result.remarks,
        "executed_at": result.executed_at,
        "run_id": result.run_id,
        "duration_ms": result.duration_ms,
        "error_side": result.error_side.value,
        "error_code": result.error_code,
    }
    for name, value in values.items():
        definition = schema.fields.get(name)
        if definition is None:
            continue
        if not definition.write:
            # Defence in depth: the schema loader already rejects a result field
            # that is not writable, so reaching here means a caller bug.
            raise WorkbookError(
                f"Refusing to write read-only field '{name}' (column '{definition.header}')"
            )
        column = column_map.get(name)
        if column is None:
            continue
        cell = sheet.cell(row=result.row_number, column=column)
        cell.value = _to_cell_value(value, definition)
        if definition.format:
            cell.number_format = definition.format


def _to_cell_value(value: Any, definition: FieldDefinition) -> Any:
    """Convert a result value into something openpyxl can store."""
    if value is None or value == "" or value is ErrorSide.NONE:
        return None
    if isinstance(value, datetime):
        # Excel has no concept of a timezone; timestamps are UTC by schema
        # contract, so the offset is dropped rather than silently converted.
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, Decimal):
        return value if value.is_finite() else str(value)
    if definition.type is FieldType.STRING and not isinstance(value, str):
        return str(value)
    return value
