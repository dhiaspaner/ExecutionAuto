"""Shared fixtures.

Every fixture here is offline: workbooks are generated into ``tmp_path`` and all
database access goes through the fake executor. Nothing in this suite opens a
socket, reads a credential or touches a real reconciliation workbook.
"""

from __future__ import annotations

import copy
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from migration_reconciliation.database.fake import FakeResultBook, parse_fake_results
from migration_reconciliation.models import WorkbookSchema
from migration_reconciliation.workbook.payments_template import (
    DEFAULT_RUN_CONTROL,
    write_workbook,
)
from migration_reconciliation.workbook.schema import parse_schema
from migration_reconciliation.workbook.template import write_template

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The generic schema the suite is built on. It is a test fixture, not a
#: shipped config file: config/ carries only the schema real runs use.
GENERIC_SCHEMA_PATH = REPO_ROOT / "tests" / "fixtures" / "workbook_schema.generic.toml"
EXAMPLE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "fake_results.toml"
EXAMPLE_TEMPLATE_PATH = REPO_ROOT / "templates" / "reconciliation_template.xlsx"


@pytest.fixture
def schema_document() -> dict[str, Any]:
    """A mutable copy of the generic schema fixture.

    Tests mutate this to produce invalid variants, which also keeps the fixture
    honest: if it ever stops being valid, most of the suite fails.
    """
    return copy.deepcopy(tomllib.loads(GENERIC_SCHEMA_PATH.read_text(encoding="utf-8")))


@pytest.fixture
def schema(schema_document: dict[str, Any]) -> WorkbookSchema:
    return parse_schema(schema_document, source="<test>")


@pytest.fixture
def make_workbook(schema: WorkbookSchema, tmp_path: Path):
    """Build a workbook in ``tmp_path`` from rows of semantic field values."""

    def _make(
        rows: Sequence[Mapping[str, Any]],
        *,
        name: str = "cases.xlsx",
        workbook_schema: WorkbookSchema | None = None,
        include_readme_sheet: bool = True,
    ) -> Path:
        return write_template(
            tmp_path / name,
            workbook_schema or schema,
            rows,
            include_readme_sheet=include_readme_sheet,
        )

    return _make


@pytest.fixture
def case_row() -> Any:
    """Factory for one valid test-case row; override any field by keyword."""

    def _row(test_case_id: str = "TC-001", **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "test_case_id": test_case_id,
            "domain": "Payments",
            "entity": "Payment",
            "enabled": True,
            "source_type": "oracle",
            "source_connection": "LEGACY_ORACLE",
            "target_connection": "MIGRATED_SQLSERVER",
            "source_sql": "SELECT COUNT(*) FROM PAYMENTS",
            "target_sql": "SELECT COUNT(*) FROM dbo.Payments",
            "comparison_rule": "equal",
            "tolerance": 0,
            "timeout_seconds": 120,
        }
        row.update(overrides)
        return row

    return _row


@pytest.fixture
def make_results() -> Any:
    """Factory for a :class:`FakeResultBook` from ``[[case]]`` style entries."""

    def _make(*cases: Mapping[str, Any], on_unknown_case: str = "error") -> FakeResultBook:
        return parse_fake_results(
            {
                "version": "1.0",
                "defaults": {"on_unknown_case": on_unknown_case},
                "case": [dict(case) for case in cases],
            },
            source="<test-fixture>",
        )

    return _make


# -- reconciliation workbook template ----------------------------------------


@pytest.fixture
def test_row() -> Any:
    """Factory for one ``Test Cases`` row; override any column by keyword."""

    def _row(test_id: str = "TC-001", **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "Test_ID": test_id,
            "Enabled": "Yes",
            "Domain": "Payments",
            "Flow": "Migration",
            "Test_Name": "Row counts match",
            "Execution_Scope": "SOURCE_TARGET",
            "Comparison_Type": "EQUAL",
            "Result_Type": "INTEGER",
            "Source_Profile_Section": "source",
            "Source_SQL": "SELECT COUNT(*) FROM webservice.dbo.Payments",
            "Target_Profile_Section": "target",
            "Target_SQL": "SELECT COUNT(*) FROM dbo.Payments",
            "Severity": "High",
        }
        row.update(overrides)
        return {name: value for name, value in row.items() if value is not None}

    return _row


@pytest.fixture
def make_recon_workbook(tmp_path: Path) -> Any:
    """Build a reconciliation workbook in ``tmp_path`` from the given rows."""

    def _make(
        tests: Sequence[Mapping[str, Any]],
        *,
        name: str = "Payments.xlsx",
        run_control: Sequence[tuple[str, Any, str]] | None = None,
        observation_rules: Sequence[Mapping[str, str]] | None = None,
        include_documentation: bool = True,
    ) -> Path:
        kwargs: dict[str, Any] = {
            "tests": tests,
            "include_documentation": include_documentation,
        }
        if run_control is not None:
            kwargs["run_control"] = run_control
        if observation_rules is not None:
            kwargs["observation_rules"] = observation_rules
        return write_workbook(tmp_path / name, **kwargs)

    return _make


@pytest.fixture
def run_control_settings() -> Any:
    """Factory for ``Run Control`` rows: the defaults with a few values changed."""

    def _settings(**overrides: Any) -> list[tuple[str, Any, str]]:
        rows = [list(row) for row in DEFAULT_RUN_CONTROL]
        for name, value in overrides.items():
            for row in rows:
                if row[0] == name:
                    row[1] = value
                    break
            else:  # a setting the shipped defaults do not carry
                rows.append([name, value, ""])
        return [tuple(row) for row in rows]

    return _settings
