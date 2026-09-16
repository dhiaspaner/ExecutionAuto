"""The ``Run Control`` sheet: how a run behaves, not what it tests.

Run Control answers questions about execution — how long a query may take, how
much of a message to keep, where the output goes — and nothing about which
databases to open or which SQL to run. Connections come from TOML; queries come
from ``Test Cases``.

Two settings are deliberately not obeyed as written:

``Require_Read_Only_SQL``
    Read-only validation is a security requirement, so a workbook cannot switch
    it off. ``No`` is accepted, warned about, and ignored.

``Max_Parallel_Workers``
    Validated and reported, then capped at one. This build executes
    sequentially; a workbook that asks for eight workers gets a warning rather
    than a surprise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ..errors import ConfigurationError
from ..models import ErrorCode, Platform
from .sheetio import cell, find_sheet, locate_header_row, map_headers, sheet_rows, text_of

__all__ = [
    "MAX_SUPPORTED_WORKERS",
    "SUPPORTED_TEMPLATE_VERSIONS",
    "OutputMode",
    "RunControl",
    "read_run_control",
]

#: Template versions this build understands. Anything else stops the run.
SUPPORTED_TEMPLATE_VERSIONS: frozenset[str] = frozenset({"1.0", "2.0", "2.1"})

#: Sequential execution only, per the framework's no-parallelism rule.
MAX_SUPPORTED_WORKERS = 1

_SETTING_HEADERS = ("Setting", "Value", "Description", "Notes")

_TRUE_WORDS = frozenset({"yes", "y", "true", "t", "1", "on", "enabled"})
_FALSE_WORDS = frozenset({"no", "n", "false", "f", "0", "off", "disabled"})


class OutputMode(StrEnum):
    """Where the finished workbook lands."""

    #: Write a new timestamped workbook and leave the input untouched.
    NEW_FILE = "NEW_FILE"
    #: Replace the input workbook, still through a temporary file.
    IN_PLACE = "IN_PLACE"


@dataclass(frozen=True, slots=True)
class RunControl:
    """Validated run-control settings. Defaults are the safe answers."""

    template_version: str = ""
    #: ``0`` means no limit: a query runs until the database answers.
    query_timeout_seconds: int = 0
    max_parallel_workers: int = 1
    continue_on_test_error: bool = True
    stop_on_critical_config_error: bool = True
    observation_max_length: int = 300
    error_detail_max_length: int = 500
    output_mode: OutputMode = OutputMode.NEW_FILE
    output_directory_env: str = ""
    evidence_directory_env: str = ""
    dry_run: bool = False
    require_read_only_sql: bool = True
    #: Settings that were present but could not be obeyed, and unknown keys.
    warnings: tuple[str, ...] = ()
    #: True when the sheet was absent and every value is a default.
    defaulted: bool = False

    def with_overrides(self, *, dry_run: bool | None = None) -> RunControl:
        """Apply command-line overrides. Only the ones a person may set."""
        if dry_run is None or dry_run == self.dry_run:
            return self
        return RunControl(**{**_as_dict(self), "dry_run": dry_run})


def _as_dict(control: RunControl) -> dict[str, Any]:
    return {name: getattr(control, name) for name in RunControl.__slots__}


@dataclass
class _Reader:
    """Accumulates settings and warnings while walking the sheet."""

    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def read_run_control(workbook: Any, *, sheet_name: str = "Run Control") -> RunControl:
    """Read and validate the ``Run Control`` sheet.

    An absent sheet is not an error: every setting has a safe default, and a
    workbook that carries only test cases still runs.
    """
    sheet = find_sheet(workbook, sheet_name)
    if sheet is None:
        return RunControl(defaulted=True)

    rows = sheet_rows(sheet)
    raw = _collect_settings(rows)
    return _build(raw)


def _collect_settings(rows: Sequence[tuple[Any, ...]]) -> dict[str, Any]:
    """Pull ``name -> value`` pairs out of the sheet.

    Handles both shapes seen in practice: a titled table with ``Setting`` and
    ``Value`` headers, and a bare two-column list.
    """
    header_row = locate_header_row(rows, _SETTING_HEADERS, must_contain="Setting")
    key_column, value_column, first_row = 1, 2, 1
    if header_row is not None:
        headers = map_headers(rows[header_row - 1])
        key_column = headers.get("setting", 1)
        value_column = headers.get("value", key_column + 1)
        first_row = header_row + 1

    settings: dict[str, Any] = {}
    for row in rows[first_row - 1 :]:
        name = text_of(cell(row, key_column))
        if not name:
            continue
        settings.setdefault(_normalize_setting(name), cell(row, value_column))
    return settings


def _normalize_setting(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _build(raw: dict[str, Any]) -> RunControl:
    reader = _Reader()
    known = {
        _normalize_setting(name): name
        for name in (
            "Template_Version",
            "Query_Timeout_Seconds",
            "Max_Parallel_Workers",
            "Continue_On_Test_Error",
            "Stop_On_Critical_Config_Error",
            "Observation_Max_Length",
            "Error_Detail_Max_Length",
            "Output_Mode",
            "Output_Directory_Env",
            "Evidence_Directory_Env",
            "Dry_Run",
            "Require_Read_Only_SQL",
        )
    }
    for key in raw:
        if key not in known:
            reader.warn(f"Run Control: ignoring unsupported setting '{key}'.")

    template_version = text_of(raw.get(_normalize_setting("Template_Version")))
    if template_version and template_version not in SUPPORTED_TEMPLATE_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_TEMPLATE_VERSIONS))
        raise ConfigurationError(
            f"Run Control: Template_Version '{template_version}' is not supported by this "
            f"executor (supported: {supported}).",
            code=ErrorCode.UNSUPPORTED_TEMPLATE_VERSION,
            platform=Platform.WORKBOOK,
        )

    workers = _integer(raw, "Max_Parallel_Workers", 1, minimum=1, maximum=64)
    if workers > MAX_SUPPORTED_WORKERS:
        reader.warn(
            f"Run Control: Max_Parallel_Workers = {workers}, but this build executes "
            f"sequentially; {MAX_SUPPORTED_WORKERS} worker is being used."
        )
        workers = MAX_SUPPORTED_WORKERS

    require_read_only = _boolean(raw, "Require_Read_Only_SQL", True)
    if not require_read_only:
        reader.warn(
            "Run Control: Require_Read_Only_SQL = No is ignored. Read-only SQL validation "
            "is a security requirement and cannot be switched off from the workbook."
        )

    return RunControl(
        template_version=template_version,
        query_timeout_seconds=_integer(raw, "Query_Timeout_Seconds", 0, minimum=0, maximum=3600),
        max_parallel_workers=workers,
        continue_on_test_error=_boolean(raw, "Continue_On_Test_Error", True),
        stop_on_critical_config_error=_boolean(raw, "Stop_On_Critical_Config_Error", True),
        observation_max_length=_integer(
            raw, "Observation_Max_Length", 300, minimum=40, maximum=4000
        ),
        error_detail_max_length=_integer(
            raw, "Error_Detail_Max_Length", 500, minimum=40, maximum=4000
        ),
        output_mode=_output_mode(raw),
        output_directory_env=text_of(raw.get(_normalize_setting("Output_Directory_Env"))),
        evidence_directory_env=text_of(raw.get(_normalize_setting("Evidence_Directory_Env"))),
        dry_run=_boolean(raw, "Dry_Run", False),
        require_read_only_sql=True,
        warnings=tuple(reader.warnings),
    )


def _output_mode(raw: dict[str, Any]) -> OutputMode:
    text = text_of(raw.get(_normalize_setting("Output_Mode")))
    if not text:
        return OutputMode.NEW_FILE
    try:
        return OutputMode(text.strip().upper())
    except ValueError:
        allowed = ", ".join(mode.value for mode in OutputMode)
        raise ConfigurationError(
            f"Run Control: Output_Mode '{text}' is not supported (allowed: {allowed}).",
            code=ErrorCode.UNSUPPORTED_TEMPLATE_VERSION,
            platform=Platform.WORKBOOK,
        ) from None


def _integer(raw: dict[str, Any], name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = raw.get(_normalize_setting(name))
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        raise _range_error(name, value, minimum, maximum)
    try:
        number = Decimal(str(value).strip())
    except Exception:
        raise _range_error(name, value, minimum, maximum) from None
    if number != number.to_integral_value():
        raise _range_error(name, value, minimum, maximum)
    parsed = int(number)
    if not minimum <= parsed <= maximum:
        raise _range_error(name, value, minimum, maximum)
    return parsed


def _range_error(name: str, value: Any, minimum: int, maximum: int) -> ConfigurationError:
    return ConfigurationError(
        f"Run Control: {name} must be a whole number between {minimum} and {maximum} "
        f"(found '{value}').",
        code=ErrorCode.INVALID_PROFILE,
        platform=Platform.WORKBOOK,
    )


def _boolean(raw: dict[str, Any], name: str, default: bool) -> bool:
    value = raw.get(_normalize_setting(name))
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
    text = text_of(value).casefold()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise ConfigurationError(
        f"Run Control: {name} must be Yes or No (found '{value}').",
        code=ErrorCode.INVALID_PROFILE,
        platform=Platform.WORKBOOK,
    )
