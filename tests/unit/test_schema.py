"""Schema DSL loading and validation."""

from __future__ import annotations

from typing import Any

import pytest

from migration_reconciliation.errors import SchemaError
from migration_reconciliation.models import FieldType
from migration_reconciliation.workbook.schema import load_schema, parse_schema
from tests.conftest import GENERIC_SCHEMA_PATH


def test_loads_the_shipped_example_schema() -> None:
    schema = load_schema(GENERIC_SCHEMA_PATH)

    assert schema.schema_version == "1.0"
    assert schema.profile_name == "default-reconciliation"
    assert schema.sheet_name == "Execution Test Cases"
    assert schema.header_row == 1
    assert schema.first_data_row == 2
    assert schema.preserve_other_sheets is True
    assert schema.field("test_case_id").header == "Test Case ID"
    assert schema.field("source_type").allowed_values == ("oracle", "sqlserver")
    assert schema.field("timeout_seconds").default == 120
    assert schema.field("enabled").default is True


def test_read_and_write_fields_are_partitioned(schema: Any) -> None:
    readable = {f.name for f in schema.readable_fields()}
    writable = {f.name for f in schema.writable_fields()}

    assert "source_sql" in readable
    assert "status" in writable
    assert readable.isdisjoint(writable), "a field is an input or a result, never both"


def test_optional_semantic_fields_are_supported(schema: Any) -> None:
    for name in ("domain", "entity", "reconciliation_type", "expected_result", "severity"):
        assert schema.field(name).read is True
    assert schema.field("error_code").write is True


def test_rejects_unsupported_schema_version(schema_document: dict[str, Any]) -> None:
    schema_document["schema_version"] = "2.0"

    with pytest.raises(SchemaError, match=r"unsupported schema_version '2\.0'"):
        parse_schema(schema_document)


def test_rejects_missing_mandatory_read_field(schema_document: dict[str, Any]) -> None:
    del schema_document["fields"]["source_sql"]

    with pytest.raises(SchemaError, match="missing mandatory readable field"):
        parse_schema(schema_document)


def test_rejects_missing_mandatory_write_field(schema_document: dict[str, Any]) -> None:
    del schema_document["fields"]["status"]

    with pytest.raises(SchemaError, match="missing mandatory writable field"):
        parse_schema(schema_document)


def test_rejects_mandatory_result_field_declared_read_only(
    schema_document: dict[str, Any],
) -> None:
    """A result column that is not writable must be caught at load time."""
    schema_document["fields"]["status"]["write"] = False
    schema_document["fields"]["status"]["read"] = True

    with pytest.raises(SchemaError, match="missing mandatory writable field"):
        parse_schema(schema_document)


def test_rejects_duplicate_header_mapping(schema_document: dict[str, Any]) -> None:
    schema_document["fields"]["entity"]["header"] = "Domain"

    with pytest.raises(SchemaError, match="duplicate Excel header 'Domain'"):
        parse_schema(schema_document)


def test_duplicate_header_detection_ignores_case_and_spacing(
    schema_document: dict[str, Any],
) -> None:
    schema_document["fields"]["entity"]["header"] = "  domain  "

    with pytest.raises(SchemaError, match="duplicate Excel header"):
        parse_schema(schema_document)


@pytest.mark.parametrize(
    ("header_row", "first_data_row", "message"),
    [
        (0, 2, "header_row must be 1 or greater"),
        (5, 5, "first_data_row .* must be greater than header_row"),
        (5, 3, "first_data_row .* must be greater than header_row"),
        (-1, 2, "header_row must be 1 or greater"),
    ],
)
def test_rejects_invalid_row_configuration(
    schema_document: dict[str, Any], header_row: int, first_data_row: int, message: str
) -> None:
    schema_document["workbook"]["header_row"] = header_row
    schema_document["workbook"]["first_data_row"] = first_data_row

    with pytest.raises(SchemaError, match=message):
        parse_schema(schema_document)


def test_rejects_unknown_field_type(schema_document: dict[str, Any]) -> None:
    schema_document["fields"]["tolerance"]["type"] = "money"

    with pytest.raises(SchemaError, match="unknown type 'money'"):
        parse_schema(schema_document)


def test_rejects_enum_without_allowed_values(schema_document: dict[str, Any]) -> None:
    del schema_document["fields"]["comparison_rule"]["allowed_values"]

    with pytest.raises(SchemaError, match="requires allowed_values"):
        parse_schema(schema_document)


def test_rejects_allowed_values_outside_the_framework_enum(
    schema_document: dict[str, Any],
) -> None:
    schema_document["fields"]["source_type"]["allowed_values"] = ["oracle", "postgres"]

    with pytest.raises(SchemaError, match="not valid for 'source_type'"):
        parse_schema(schema_document)


def test_rejects_default_outside_allowed_values(schema_document: dict[str, Any]) -> None:
    schema_document["fields"]["comparison_rule"]["default"] = "almost_equal"

    with pytest.raises(SchemaError, match="is not one of allowed_values"):
        parse_schema(schema_document)


def test_rejects_default_of_the_wrong_type(schema_document: dict[str, Any]) -> None:
    schema_document["fields"]["timeout_seconds"]["default"] = "two minutes"

    with pytest.raises(SchemaError, match="default must be an integer"):
        parse_schema(schema_document)


def test_rejects_field_that_is_neither_read_nor_write(schema_document: dict[str, Any]) -> None:
    schema_document["fields"]["severity"]["read"] = False

    with pytest.raises(SchemaError, match="must declare read = true or write = true"):
        parse_schema(schema_document)


def test_rejects_field_that_is_both_read_and_write(schema_document: dict[str, Any]) -> None:
    schema_document["fields"]["severity"]["write"] = True

    with pytest.raises(SchemaError, match="declares both read and write"):
        parse_schema(schema_document)


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("../{timestamp}.xlsx", "must not contain"),
        ("/tmp/out_{timestamp}.xlsx", "must be a bare filename"),
        ("C:\\out\\{timestamp}.xlsx", "must be a bare filename"),
        ("{input_stem}_{timestamp}.xlsm", "must end with '.xlsx'"),
        ("{input_stem}_results.xlsx", "must include"),
        ("{input_stem}_{whoami}.xlsx", "unknown placeholder"),
        ("results_{timestamp.xlsx", "unbalanced"),
    ],
)
def test_rejects_unsafe_output_filename_pattern(
    schema_document: dict[str, Any], pattern: str, message: str
) -> None:
    schema_document["workbook"]["output_filename_pattern"] = pattern

    with pytest.raises(SchemaError, match=message):
        parse_schema(schema_document)


def test_rejects_missing_workbook_properties(schema_document: dict[str, Any]) -> None:
    del schema_document["workbook"]["test_case_sheet"]

    with pytest.raises(SchemaError, match="missing required keys: test_case_sheet"):
        parse_schema(schema_document)


def test_rejects_unknown_keys(schema_document: dict[str, Any]) -> None:
    schema_document["workbook"]["run_command"] = "powershell.exe"

    with pytest.raises(SchemaError, match="unknown key\\(s\\) in \\[workbook\\]: run_command"):
        parse_schema(schema_document)


def test_rejects_unsupported_timezone(schema_document: dict[str, Any]) -> None:
    schema_document["fields"]["executed_at"]["timezone"] = "Europe/Paris"

    with pytest.raises(SchemaError, match="results are always recorded in UTC"):
        parse_schema(schema_document)


def test_alternate_headers_produce_the_same_semantic_fields(
    schema_document: dict[str, Any],
) -> None:
    """A different template is a different TOML file, not different Python."""
    schema_document["profile_name"] = "fines-domain"
    schema_document["workbook"]["test_case_sheet"] = "Fines Recon"
    schema_document["fields"]["test_case_id"]["header"] = "Scenario Ref"
    schema_document["fields"]["source_sql"]["header"] = "Legacy SQL"

    schema = parse_schema(schema_document)

    assert schema.sheet_name == "Fines Recon"
    assert schema.field("test_case_id").header == "Scenario Ref"
    assert schema.field("source_sql").type is FieldType.SQL


def test_missing_file_reports_a_clear_error(tmp_path: Any) -> None:
    with pytest.raises(SchemaError, match="Cannot read schema file"):
        load_schema(tmp_path / "nope.toml")


def test_invalid_toml_reports_a_clear_error(tmp_path: Any) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("schema_version = ", encoding="utf-8")

    with pytest.raises(SchemaError, match="not valid TOML"):
        load_schema(bad)
