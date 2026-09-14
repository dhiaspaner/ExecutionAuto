"""Header names of the reconciliation workbook template.

Columns are found by *exact header name*, never by position, so inserting or
re-ordering columns in the template changes nothing here. Header matching is
case-insensitive with collapsed whitespace, which is the only latitude given:
``Test_ID`` and ``test_id`` are the same column, ``TestID`` is not.

The split below is the safety property this module exists for. The executor
reads :data:`DEFINITION_COLUMNS` and writes :data:`OUTPUT_COLUMNS`, and nothing
else. A definition cell is never modified, so a run can never edit the test it
was asked to perform.
"""

from __future__ import annotations

__all__ = [
    "DEFINITION_COLUMNS",
    "DOCUMENTATION_SHEETS",
    "LEGACY_COLUMNS",
    "OBSERVATION_RULES_SHEET",
    "OPTIONAL_OUTPUT_COLUMNS",
    "OUTPUT_COLUMNS",
    "REQUIRED_DEFINITION_COLUMNS",
    "REQUIRED_OUTPUT_COLUMNS",
    "RUN_CONTROL_SHEET",
    "RUN_HISTORY_COLUMNS",
    "RUN_HISTORY_SHEET",
    "TEST_CASES_SHEET",
    "normalize_header",
]

#: Sheets this framework reads.
TEST_CASES_SHEET = "Test Cases"
RUN_CONTROL_SHEET = "Run Control"
OBSERVATION_RULES_SHEET = "Observation Rules"
COMPARISON_TYPES_SHEET = "Comparison Types"
RUN_HISTORY_SHEET = "Run History"

#: Sheets that are documentation or a TOML example, and are never parsed for
#: instructions, connection settings or executable actions. They are listed so
#: the executor can say out loud that it ignored them.
DOCUMENTATION_SHEETS: tuple[str, ...] = (
    "Executor Contract",
    "Conversion Notes",
    "Connections",
)

#: Columns the person fills in. The executor only ever reads these.
DEFINITION_COLUMNS: tuple[str, ...] = (
    "Test_ID",
    "Enabled",
    "Domain",
    "Flow",
    "Test_Name",
    "Execution_Scope",
    "Comparison_Type",
    "Result_Type",
    "Source_Profile_Section",
    "Source_Object",
    "Source_SQL",
    "Target_Profile_Section",
    "Target_Object",
    "Target_SQL",
    "Expected_Value",
    "Absolute_Tolerance",
    "Percentage_Tolerance",
    "Severity",
    "Owner",
    "Tags",
)

#: Without these there is no test to run, so their absence stops the run before
#: anything is opened rather than producing a sheet full of CONFIG ERROR rows.
REQUIRED_DEFINITION_COLUMNS: tuple[str, ...] = (
    "Test_ID",
    "Enabled",
    "Execution_Scope",
    "Comparison_Type",
    "Result_Type",
    "Source_Profile_Section",
    "Source_SQL",
    "Target_Profile_Section",
    "Target_SQL",
)

#: Columns the executor writes. Nothing outside this tuple is ever written on
#: the ``Test Cases`` sheet.
OUTPUT_COLUMNS: tuple[str, ...] = (
    "Source_Result",
    "Target_Result",
    "Actual_Value",
    "Variance",
    "Variance_Percentage",
    "Status",
    "Observation",
    "Source_Duration_ms",
    "Target_Duration_ms",
    "Executed_At_UTC",
    "Run_ID",
    "Error_Code",
    "Error_Detail",
    "Evidence_Path",
)

#: Written when the sheet happens to have it. ``Platform`` is not part of the
#: published template, but a workbook that adds it gets the platform that
#: decided each status, which is otherwise only visible in the console report.
OPTIONAL_OUTPUT_COLUMNS: tuple[str, ...] = ("Platform",)

#: Without somewhere to put the outcome there is no point running.
REQUIRED_OUTPUT_COLUMNS: tuple[str, ...] = ("Status", "Observation", "Error_Code")

#: Columns earlier templates carried. They are read for display only: a
#: workbook can describe what it wants done, it can never issue a command.
LEGACY_COLUMNS: tuple[str, ...] = ("Priority", "Executor_Action")

#: One row per run, appended and never rewritten.
RUN_HISTORY_COLUMNS: tuple[str, ...] = (
    "Run_ID",
    "Started_At_UTC",
    "Completed_At_UTC",
    "Workbook_Name",
    "Template_Version",
    "Executor_Version",
    "Enabled_Tests",
    "Passed",
    "Failed",
    "Profiled",
    "Blocked_Error",
    "Not_Executed",
    "Overall_Status",
    "Output_File",
)


def normalize_header(header: object) -> str:
    """Fold header text for comparison: case and inner whitespace do not count."""
    return " ".join(str(header).split()).casefold()
