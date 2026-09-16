"""The fixed column layout used by the interactive runner.

The offline ``reconcile`` command maps semantic fields to Excel headers with a
TOML schema file. The wizard-driven runner deliberately does not: its layout is
these constants, so there is no schema file to pass, to lose, or to get wrong.

Only two things vary: the workbook path and the sheet name, both asked for at
runtime. Everything else here is fixed.

Three groups of fields exist:

**Required columns** — the sheet must have these, by header text.

**Optional columns** — honoured when present, defaulted when absent. A workbook
carrying a richer template still works, and a bare one still runs.

**Wizard-owned fields** — connection identity, which comes from the answers
typed at the keyboard. Their headers are sentinels that no real spreadsheet
uses, so a stray column can never quietly override what the person typed.
"""

from __future__ import annotations

from typing import Any

from ..models import ComparisonRule, DatabaseType, ExecutionScope, WorkbookSchema
from .schema import parse_schema

__all__ = [
    "INLINE_PROFILE_NAME",
    "OPTIONAL_INPUT_HEADERS",
    "REQUIRED_INPUT_HEADERS",
    "RESULT_HEADERS",
    "build_inline_schema",
]

INLINE_PROFILE_NAME = "interactive-runner"

HEADER_ROW = 1
FIRST_DATA_ROW = 2
OUTPUT_FILENAME_PATTERN = "{input_stem}_results_{timestamp}.xlsx"

#: Without these three columns there is nothing to reconcile.
REQUIRED_INPUT_HEADERS: dict[str, str] = {
    "test_case_id": "ID",
    "source_sql": "Source SQL",
    "target_sql": "Target SQL",
}

#: Read when the column exists, defaulted when it does not.
OPTIONAL_INPUT_HEADERS: dict[str, str] = {
    "enabled": "Enabled",
    "execution_scope": "Execution Scope",
    "comparison_rule": "Comparison Rule",
    "tolerance": "Tolerance",
    "timeout_seconds": "Timeout Seconds",
    "domain": "Domain",
    "entity": "Entity",
}

#: Result columns, in the order a person reads them.
RESULT_HEADERS: dict[str, str] = {
    "source_result": "Source Results",
    "target_result": "Target Results",
    "variance": "Variance",
    "status": "Status",
    "remarks": "Remarks",
}

#: Extra result columns the framework fills in when the sheet happens to have
#: them. Absent from most workbooks, and never required: the run id and the
#: timestamp are in the output filename regardless.
EXTRA_RESULT_HEADERS: dict[str, str] = {
    "executed_at": "Executed At",
    "run_id": "Run ID",
    "duration_ms": "Duration",
    "error_side": "Error Side",
}

#: Headers no spreadsheet uses, for the fields the wizard owns.
_SENTINEL = "__wizard__{name}"

#: ``0`` means no limit: a query runs until the database answers.
DEFAULT_TIMEOUT_SECONDS = 0


def build_inline_schema(sheet_name: str, source_type: DatabaseType) -> WorkbookSchema:
    """Build the fixed schema for one chosen sheet and source engine.

    ``source_type`` comes from the wizard and is baked in as the default of a
    field no column can supply, which is what makes the typed answer win.
    """
    fields: dict[str, Any] = {
        "test_case_id": {
            "header": REQUIRED_INPUT_HEADERS["test_case_id"],
            "type": "string",
            "required": True,
            "read": True,
        },
        "source_sql": {
            "header": REQUIRED_INPUT_HEADERS["source_sql"],
            "type": "sql",
            "required": True,
            "read": True,
        },
        "target_sql": {
            "header": REQUIRED_INPUT_HEADERS["target_sql"],
            "type": "sql",
            "required": True,
            "read": True,
        },
        "enabled": {
            "header": OPTIONAL_INPUT_HEADERS["enabled"],
            "type": "boolean",
            "required": False,
            "default": True,
            "read": True,
        },
        # Optional here: a sheet without the column runs both sides, which is
        # what every two-sided reconciliation wants. A sheet that has it can
        # express a one-sided test without a schema file.
        "execution_scope": {
            "header": OPTIONAL_INPUT_HEADERS["execution_scope"],
            "type": "enum",
            "allowed_values": [scope.value for scope in ExecutionScope],
            "required": False,
            "default": ExecutionScope.SOURCE_TARGET.value,
            "read": True,
        },
        "comparison_rule": {
            "header": OPTIONAL_INPUT_HEADERS["comparison_rule"],
            "type": "enum",
            "allowed_values": [rule.value for rule in ComparisonRule],
            "required": False,
            "default": ComparisonRule.EQUAL.value,
            "read": True,
        },
        "tolerance": {
            "header": OPTIONAL_INPUT_HEADERS["tolerance"],
            "type": "decimal",
            "required": False,
            "default": 0,
            "read": True,
        },
        "timeout_seconds": {
            "header": OPTIONAL_INPUT_HEADERS["timeout_seconds"],
            "type": "integer",
            "required": False,
            "default": DEFAULT_TIMEOUT_SECONDS,
            "read": True,
        },
        "domain": {
            "header": OPTIONAL_INPUT_HEADERS["domain"],
            "type": "string",
            "required": False,
            "read": True,
        },
        "entity": {
            "header": OPTIONAL_INPUT_HEADERS["entity"],
            "type": "string",
            "required": False,
            "read": True,
        },
        # -- wizard-owned: no column supplies these -----------------------
        "source_type": {
            "header": _SENTINEL.format(name="source_type"),
            "type": "enum",
            "allowed_values": [engine.value for engine in DatabaseType],
            "required": False,
            "default": source_type.value,
            "read": True,
        },
        "source_connection": {
            "header": _SENTINEL.format(name="source_connection"),
            "type": "string",
            "required": False,
            "default": "SOURCE",
            "read": True,
        },
        "target_connection": {
            "header": _SENTINEL.format(name="target_connection"),
            "type": "string",
            "required": False,
            "default": "TARGET",
            "read": True,
        },
    }

    for name, header in RESULT_HEADERS.items():
        fields[name] = {
            "header": header,
            "type": _RESULT_TYPES[name],
            "required": False,
            "read": False,
            "write": True,
        }
        allowed = _RESULT_ALLOWED_VALUES.get(name)
        if allowed is not None:
            fields[name]["allowed_values"] = allowed

    for name, header in EXTRA_RESULT_HEADERS.items():
        fields[name] = {
            "header": header,
            "type": _RESULT_TYPES[name],
            "required": False,
            "read": False,
            "write": True,
        }
        allowed = _RESULT_ALLOWED_VALUES.get(name)
        if allowed is not None:
            fields[name]["allowed_values"] = allowed
    fields["executed_at"]["format"] = "yyyy-mm-dd hh:mm:ss"
    fields["executed_at"]["timezone"] = "UTC"

    document: dict[str, Any] = {
        "schema_version": "1.0",
        "profile_name": INLINE_PROFILE_NAME,
        "workbook": {
            "test_case_sheet": sheet_name,
            "header_row": HEADER_ROW,
            "first_data_row": FIRST_DATA_ROW,
            "preserve_other_sheets": True,
            "output_filename_pattern": OUTPUT_FILENAME_PATTERN,
        },
        "fields": fields,
    }
    return parse_schema(document, source="<interactive runner>")


_RESULT_TYPES: dict[str, str] = {
    "source_result": "scalar",
    "target_result": "scalar",
    "variance": "decimal",
    "status": "enum",
    "remarks": "string",
    "executed_at": "datetime",
    "run_id": "string",
    "duration_ms": "integer",
    "error_side": "enum",
}

_RESULT_ALLOWED_VALUES: dict[str, list[str]] = {
    "status": ["PASS", "FAIL", "ERROR", "SKIPPED"],
    "error_side": ["SOURCE", "TARGET", "COMPARISON", "WORKBOOK", ""],
}
