"""Loader and validator for the declarative workbook schema DSL.

The DSL is read with :mod:`tomllib`, which parses *data only*. No Python,
shell, Excel formula or SQL expression from a schema file is ever evaluated.
A schema binds stable semantic field names to the header text of one specific
Excel template, so supporting a new template means writing another TOML file,
not changing Python.
"""

from __future__ import annotations

import re
import tomllib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..errors import SchemaError
from ..models import (
    ComparisonRule,
    DatabaseType,
    ErrorSide,
    ExecutionStatus,
    FieldDefinition,
    FieldType,
    WorkbookSchema,
)

__all__ = [
    "MANDATORY_READ_FIELDS",
    "MANDATORY_WRITE_FIELDS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "load_schema",
    "parse_schema",
]

#: Schema versions this build understands. Anything else is refused up front.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})

#: Semantic fields the runner cannot work without when reading a workbook.
MANDATORY_READ_FIELDS: tuple[str, ...] = (
    "test_case_id",
    "source_type",
    "source_connection",
    "target_connection",
    "source_sql",
    "target_sql",
)

#: Semantic fields the runner must be able to write back.
MANDATORY_WRITE_FIELDS: tuple[str, ...] = (
    "source_result",
    "target_result",
    "variance",
    "status",
    "remarks",
    "executed_at",
    "run_id",
    "duration_ms",
    "error_side",
)

_ALLOWED_TOP_LEVEL_KEYS = frozenset({"schema_version", "profile_name", "workbook", "fields"})
_REQUIRED_WORKBOOK_KEYS = frozenset({"test_case_sheet", "header_row", "first_data_row"})
_ALLOWED_WORKBOOK_KEYS = _REQUIRED_WORKBOOK_KEYS | {
    "preserve_other_sheets",
    "output_filename_pattern",
}
_ALLOWED_FIELD_KEYS = frozenset(
    {
        "header",
        "type",
        "required",
        "read",
        "write",
        "default",
        "allowed_values",
        "format",
        "timezone",
    }
)

_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_ALLOWED_PLACEHOLDERS = frozenset({"input_stem", "timestamp", "run_id", "profile_name"})
#: Characters Windows forbids in a filename, plus path separators.
_UNSAFE_FILENAME_CHARS = set('/\\:*?"<>|')

#: Semantic fields whose allowed_values must stay inside a framework enum.
_ENUM_CONSTRAINTS: dict[str, frozenset[str]] = {
    "source_type": frozenset(m.value for m in DatabaseType),
    "comparison_rule": frozenset(m.value for m in ComparisonRule),
    "status": frozenset(m.value for m in ExecutionStatus),
    "error_side": frozenset(m.value for m in ErrorSide),
}

_SUPPORTED_TIMEZONES = frozenset({"UTC"})


def load_schema(path: str | Path) -> WorkbookSchema:
    """Read and validate a schema DSL file. Never touches a workbook or database."""
    schema_path = Path(path)
    try:
        raw = schema_path.read_bytes()
    except OSError as exc:
        raise SchemaError(f"Cannot read schema file '{schema_path}': {exc.strerror}") from None
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise SchemaError(f"Schema file '{schema_path}' is not valid UTF-8") from None
    except tomllib.TOMLDecodeError as exc:
        raise SchemaError(f"Schema file '{schema_path}' is not valid TOML: {exc}") from None
    return parse_schema(document, source=str(schema_path))


def parse_schema(document: dict[str, Any], *, source: str = "<schema>") -> WorkbookSchema:
    """Validate an already-parsed TOML document and build a :class:`WorkbookSchema`."""
    _reject_unknown_keys(document, _ALLOWED_TOP_LEVEL_KEYS, "top level", source)

    version = _require(document, "schema_version", str, source)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise SchemaError(
            f"{source}: unsupported schema_version '{version}' (supported: {supported})"
        )

    profile_name = _require(document, "profile_name", str, source)
    if not profile_name.strip():
        raise SchemaError(f"{source}: profile_name must not be empty")

    workbook = _require(document, "workbook", dict, source)
    _reject_unknown_keys(workbook, _ALLOWED_WORKBOOK_KEYS, "[workbook]", source)
    missing = sorted(_REQUIRED_WORKBOOK_KEYS - workbook.keys())
    if missing:
        raise SchemaError(f"{source}: [workbook] is missing required keys: {', '.join(missing)}")

    sheet_name = _require(workbook, "test_case_sheet", str, source, context="[workbook]")
    if not sheet_name.strip():
        raise SchemaError(f"{source}: [workbook] test_case_sheet must not be empty")

    header_row = _require_positive_int(workbook, "header_row", source)
    first_data_row = _require_positive_int(workbook, "first_data_row", source)
    if first_data_row <= header_row:
        raise SchemaError(
            f"{source}: [workbook] first_data_row ({first_data_row}) must be greater than "
            f"header_row ({header_row})"
        )

    preserve_other_sheets = workbook.get("preserve_other_sheets", True)
    if not isinstance(preserve_other_sheets, bool):
        raise SchemaError(f"{source}: [workbook] preserve_other_sheets must be a boolean")

    pattern = workbook.get("output_filename_pattern", "{input_stem}_results_{timestamp}.xlsx")
    if not isinstance(pattern, str):
        raise SchemaError(f"{source}: [workbook] output_filename_pattern must be a string")
    _validate_output_pattern(pattern, source)

    raw_fields = _require(document, "fields", dict, source)
    if not raw_fields:
        raise SchemaError(f"{source}: [fields] must define at least one field")

    fields: dict[str, FieldDefinition] = {}
    headers_seen: dict[str, str] = {}
    for name, definition in raw_fields.items():
        field = _parse_field(name, definition, source)
        normalized = _normalize_header(field.header)
        if normalized in headers_seen:
            raise SchemaError(
                f"{source}: duplicate Excel header '{field.header}' mapped by both "
                f"'{headers_seen[normalized]}' and '{name}'"
            )
        headers_seen[normalized] = name
        fields[name] = field

    _validate_mandatory_fields(fields, source)

    return WorkbookSchema(
        schema_version=version,
        profile_name=profile_name,
        sheet_name=sheet_name,
        header_row=header_row,
        first_data_row=first_data_row,
        preserve_other_sheets=preserve_other_sheets,
        output_filename_pattern=pattern,
        fields=fields,
    )


def _parse_field(name: str, definition: object, source: str) -> FieldDefinition:
    where = f"[fields.{name}]"
    if not _FIELD_NAME_RE.match(name):
        raise SchemaError(
            f"{source}: invalid field name '{name}' — use lowercase letters, digits and underscores"
        )
    if not isinstance(definition, dict):
        raise SchemaError(f"{source}: {where} must be a table")
    _reject_unknown_keys(definition, _ALLOWED_FIELD_KEYS, where, source)

    header = _require(definition, "header", str, source, context=where)
    if not header.strip():
        raise SchemaError(f"{source}: {where} header must not be empty")

    raw_type = _require(definition, "type", str, source, context=where)
    try:
        field_type = FieldType(raw_type)
    except ValueError:
        known = ", ".join(sorted(t.value for t in FieldType))
        raise SchemaError(f"{source}: {where} unknown type '{raw_type}' (known: {known})") from None

    required = _optional_bool(definition, "required", where, source, default=False)
    read = _optional_bool(definition, "read", where, source, default=False)
    write = _optional_bool(definition, "write", where, source, default=False)
    if read and write:
        raise SchemaError(
            f"{source}: {where} declares both read and write — a field is either an input "
            f"column or a result column, never both"
        )
    if not read and not write:
        raise SchemaError(f"{source}: {where} must declare read = true or write = true")

    allowed_values = _parse_allowed_values(name, definition, field_type, where, source)
    default = _parse_default(definition, field_type, allowed_values, where, source)

    fmt = definition.get("format")
    if fmt is not None and not isinstance(fmt, str):
        raise SchemaError(f"{source}: {where} format must be a string")

    timezone = definition.get("timezone")
    if timezone is not None:
        if not isinstance(timezone, str):
            raise SchemaError(f"{source}: {where} timezone must be a string")
        if timezone not in _SUPPORTED_TIMEZONES:
            raise SchemaError(
                f"{source}: {where} unsupported timezone '{timezone}' — results are always "
                f"recorded in UTC"
            )

    return FieldDefinition(
        name=name,
        header=header,
        type=field_type,
        required=required,
        read=read,
        write=write,
        default=default,
        allowed_values=allowed_values,
        format=fmt,
        timezone=timezone,
    )


def _parse_allowed_values(
    name: str,
    definition: dict[str, Any],
    field_type: FieldType,
    where: str,
    source: str,
) -> tuple[str, ...]:
    raw = definition.get("allowed_values")
    if field_type is not FieldType.ENUM:
        if raw is not None:
            raise SchemaError(f"{source}: {where} allowed_values is only valid for type = 'enum'")
        return ()
    if raw is None:
        raise SchemaError(f"{source}: {where} type = 'enum' requires allowed_values")
    if not isinstance(raw, list) or not raw:
        raise SchemaError(f"{source}: {where} allowed_values must be a non-empty array")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise SchemaError(f"{source}: {where} allowed_values must contain only strings")
        if item in values:
            raise SchemaError(f"{source}: {where} allowed_values contains duplicate '{item}'")
        values.append(item)
    constraint = _ENUM_CONSTRAINTS.get(name)
    if constraint is not None:
        unknown = [v for v in values if v not in constraint]
        if unknown:
            known = ", ".join(sorted(x for x in constraint if x))
            raise SchemaError(
                f"{source}: {where} allowed_values {unknown} are not valid for '{name}' "
                f"(supported: {known})"
            )
    return tuple(values)


def _parse_default(
    definition: dict[str, Any],
    field_type: FieldType,
    allowed_values: tuple[str, ...],
    where: str,
    source: str,
) -> object | None:
    if "default" not in definition:
        return None
    default = definition["default"]
    match field_type:
        case FieldType.BOOLEAN:
            if not isinstance(default, bool):
                raise SchemaError(f"{source}: {where} default must be a boolean")
        case FieldType.INTEGER:
            if not isinstance(default, int) or isinstance(default, bool):
                raise SchemaError(f"{source}: {where} default must be an integer")
        case FieldType.DECIMAL:
            if isinstance(default, bool) or not isinstance(default, int | float | str):
                raise SchemaError(f"{source}: {where} default must be a number")
            try:
                Decimal(str(default))
            except InvalidOperation:
                raise SchemaError(f"{source}: {where} default is not a valid number") from None
        case FieldType.ENUM:
            if not isinstance(default, str):
                raise SchemaError(f"{source}: {where} default must be a string")
            if default not in allowed_values:
                raise SchemaError(
                    f"{source}: {where} default '{default}' is not one of allowed_values"
                )
        case FieldType.DATETIME:
            raise SchemaError(f"{source}: {where} datetime fields must not declare a default")
        case _:
            if not isinstance(default, str):
                raise SchemaError(f"{source}: {where} default must be a string")
    return default


def _validate_mandatory_fields(fields: dict[str, FieldDefinition], source: str) -> None:
    missing_read = [n for n in MANDATORY_READ_FIELDS if n not in fields or not fields[n].read]
    if missing_read:
        raise SchemaError(
            f"{source}: missing mandatory readable field(s): {', '.join(missing_read)}"
        )
    missing_write = [n for n in MANDATORY_WRITE_FIELDS if n not in fields or not fields[n].write]
    if missing_write:
        raise SchemaError(
            f"{source}: missing mandatory writable field(s): {', '.join(missing_write)}"
        )


def _validate_output_pattern(pattern: str, source: str) -> None:
    where = "[workbook] output_filename_pattern"
    if not pattern.strip():
        raise SchemaError(f"{source}: {where} must not be empty")
    for placeholder in _PLACEHOLDER_RE.findall(pattern):
        if placeholder not in _ALLOWED_PLACEHOLDERS:
            allowed = ", ".join(sorted(_ALLOWED_PLACEHOLDERS))
            raise SchemaError(
                f"{source}: {where} uses unknown placeholder '{{{placeholder}}}' "
                f"(allowed: {allowed})"
            )
    skeleton = _PLACEHOLDER_RE.sub("", pattern)
    if "{" in skeleton or "}" in skeleton:
        raise SchemaError(f"{source}: {where} contains an unbalanced '{{' or '}}'")
    if ".." in skeleton:
        raise SchemaError(f"{source}: {where} must not contain '..'")
    unsafe = sorted(_UNSAFE_FILENAME_CHARS & set(skeleton))
    if unsafe:
        raise SchemaError(f"{source}: {where} must be a bare filename — remove {' '.join(unsafe)}")
    if any(ord(ch) < 32 for ch in skeleton):
        raise SchemaError(f"{source}: {where} must not contain control characters")
    if not pattern.lower().endswith(".xlsx"):
        raise SchemaError(f"{source}: {where} must end with '.xlsx'")
    if "{timestamp}" not in pattern and "{run_id}" not in pattern:
        raise SchemaError(
            f"{source}: {where} must include {{timestamp}} or {{run_id}} so each run writes "
            f"a new file and never overwrites the input workbook"
        )


def _normalize_header(header: str) -> str:
    """Headers compare case-insensitively with collapsed whitespace."""
    return " ".join(header.split()).casefold()


def _reject_unknown_keys(
    table: dict[str, Any], allowed: frozenset[str], where: str, source: str
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise SchemaError(f"{source}: unknown key(s) in {where}: {', '.join(unknown)}")


def _require[T](
    table: dict[str, Any],
    key: str,
    expected: type[T],
    source: str,
    *,
    context: str = "",
) -> T:
    prefix = f"{context} " if context else ""
    if key not in table:
        raise SchemaError(f"{source}: {prefix}missing required key '{key}'")
    value = table[key]
    if not isinstance(value, expected) or (expected is not bool and isinstance(value, bool)):
        raise SchemaError(f"{source}: {prefix}'{key}' must be of type {expected.__name__}")
    return value


def _require_positive_int(table: dict[str, Any], key: str, source: str) -> int:
    value = _require(table, key, int, source, context="[workbook]")
    if value < 1:
        raise SchemaError(f"{source}: [workbook] {key} must be 1 or greater (got {value})")
    return value


def _optional_bool(
    table: dict[str, Any], key: str, where: str, source: str, *, default: bool
) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise SchemaError(f"{source}: {where} {key} must be a boolean")
    return value
