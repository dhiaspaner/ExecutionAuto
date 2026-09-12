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
from migration_reconciliation.workbook.schema import parse_schema
from migration_reconciliation.workbook.template import write_template

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SCHEMA_PATH = REPO_ROOT / "config" / "workbook_schema.example.toml"
EXAMPLE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "fake_results.toml"
EXAMPLE_TEMPLATE_PATH = REPO_ROOT / "templates" / "reconciliation_template.xlsx"


@pytest.fixture
def schema_document() -> dict[str, Any]:
    """A mutable copy of the shipped example schema.

    Tests mutate this to produce invalid variants, which also keeps the shipped
    example honest: if it ever stops being valid, most of the suite fails.
    """
    return copy.deepcopy(tomllib.loads(EXAMPLE_SCHEMA_PATH.read_text(encoding="utf-8")))


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
