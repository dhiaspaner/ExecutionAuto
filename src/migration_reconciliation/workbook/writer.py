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
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from ..errors import WorkbookError
from ..models import (
    ErrorSide,
    ExecutionResult,
    FieldDefinition,
    FieldType,
    WorkbookSchema,
)
from .columns import (
    VALIDATION_COLUMNS,
    VALIDATION_ERRORS_COLUMNS,
    VALIDATION_ERRORS_SHEET,
    VALIDATION_SHEET,
)

__all__ = ["TIMESTAMP_FORMAT", "build_output_path", "write_results"]

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
    validation_errors: Sequence[Mapping[str, Any]] = (),
    validation_rows: Sequence[Mapping[str, Any]] = (),
) -> Path:
    """Write ``results`` into a fresh copy of the workbook and return its path.

    ``validation_rows`` adds a ``Syntax Validation`` sheet recording the
    pre-execution check for every test, whether it compiled or not.
    ``validation_errors`` adds a ``Validation Errors`` sheet holding just the
    rejected ones. Both are rewritten each run, and the errors sheet is left
    out entirely when nothing failed, so a clean run never shows stale
    failures.
    """
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
    output_path = _resolve_free_path(output_path, run_id)

    try:
        workbook = load_workbook(source_path)
    except Exception as exc:
        raise WorkbookError(f"Cannot open workbook '{source_path.name}': {exc}") from None

    try:
        if schema.sheet_name not in workbook.sheetnames:
            available = ", ".join(workbook.sheetnames) or "<none>"
            raise WorkbookError(
                f"Sheet '{schema.sheet_name}' not found. Available sheets: {available}"
            )
        sheet = workbook[schema.sheet_name]
        column_map = _map_writable_columns(sheet, schema)

        for result in results:
            _write_row(sheet, schema, column_map, result)

        _write_sheet(workbook, VALIDATION_SHEET, VALIDATION_COLUMNS, validation_rows)
        _write_sheet(
            workbook, VALIDATION_ERRORS_SHEET, VALIDATION_ERRORS_COLUMNS, validation_errors
        )

        if not schema.preserve_other_sheets:
            for name in list(workbook.sheetnames):
                if name != schema.sheet_name:
                    del workbook[name]

        try:
            workbook.save(output_path)
        except OSError as exc:
            raise WorkbookError(f"Cannot write '{output_path}': {exc.strerror}") from None
    finally:
        workbook.close()

    return output_path


def _write_sheet(
    workbook: Any,
    name: str,
    headers: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """Replace one report sheet, or leave none behind when there is nothing to say."""
    if name in workbook.sheetnames:
        del workbook[name]
    if not rows:
        return

    sheet = workbook.create_sheet(name)
    for column, header in enumerate(headers, start=1):
        sheet.cell(row=1, column=column, value=header)
    for offset, values in enumerate(rows, start=2):
        for column, header in enumerate(headers, start=1):
            value = values.get(header)
            cell = sheet.cell(row=offset, column=column)
            cell.value = value.replace(tzinfo=None) if isinstance(value, datetime) else value
            if isinstance(value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"

    widths = {"Test_ID": 18, "Error_Code": 24, "Error_Detail": 90, "Checked_At_UTC": 20}
    for column, header in enumerate(headers, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = widths.get(header, 14)
    sheet.freeze_panes = "A2"


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
