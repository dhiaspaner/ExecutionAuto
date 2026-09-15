"""The ``Observation Rules`` sheet: wording, never verdicts.

An observation explains a status that Python has *already* decided. Nothing on
this sheet can change whether a test passes, and nothing on it is executed —
a template is filled by replacing known ``{placeholders}`` with values, with no
``eval``, no format-string evaluation of arbitrary expressions, and no access
to anything but the fields listed in :data:`PLACEHOLDERS`.

Rules are selected deterministically from the logical key
``(Status, Platform, Error_Code)``, most specific first:

1. exact ``(status, platform, error_code)``
2. ``(status, platform, ANY)``
3. ``(status, ANY, error_code)``
4. ``(status, ANY, ANY)``
5. the built-in Python fallback

There is no priority column, and there is deliberately no way to add one. Two
enabled rules that match the same key are a contradiction rather than a race,
so they are rejected when the sheet is read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import ConfigurationError
from ..models import ErrorCode, Platform, TestStatus
from ..security.redaction import sanitize_text
from .sheetio import (
    cell,
    find_sheet,
    is_blank_row,
    locate_header_row,
    map_headers,
    sheet_rows,
    text_of,
)

__all__ = [
    "BUILT_IN_OBSERVATIONS",
    "PLACEHOLDERS",
    "ObservationRule",
    "ObservationRules",
    "read_observation_rules",
    "render_template",
]

#: The only names a template may use. Anything else is a typo, and a typo that
#: silently rendered as empty text would be worse than being told about it.
PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "test_id",
        "test_name",
        "domain",
        "status",
        "platform",
        "error_code",
        "error_detail",
        "source_result",
        "target_result",
        "actual_value",
        "expected_value",
        "variance",
        "variance_percentage",
        "absolute_tolerance",
        "percentage_tolerance",
        "timeout_seconds",
        "execution_scope",
        "database",
    }
)

#: What is said when the workbook has nothing to say. Deliberately plain: these
#: are the words that appear when a sheet is missing, so they have to stand on
#: their own.
BUILT_IN_OBSERVATIONS: Mapping[TestStatus, str] = {
    TestStatus.PASS: "{test_id} passed. {error_detail}",
    TestStatus.FAIL: "{test_id} failed ({error_code}). {error_detail}",
    TestStatus.PROFILED: "{test_id} profiled: actual {actual_value}. {error_detail}",
    TestStatus.ERROR: "{test_id} errored on {platform} ({error_code}). {error_detail}",
    TestStatus.SYNTAX_ERROR: (
        "{test_id} was not executed: {platform} refused to compile its SQL. {error_detail}"
    ),
    TestStatus.BLOCKED: "{test_id} was blocked ({error_code}). {error_detail}",
    TestStatus.CONFIG_ERROR: "{test_id} is misconfigured ({error_code}). {error_detail}",
    TestStatus.NOT_EXECUTED: "{test_id} was not executed. {error_detail}",
    TestStatus.DISABLED: "{test_id} is disabled in the workbook and was not executed.",
}

_ANY_WORDS = frozenset({"", "any", "*", "all", "-"})
_TRUE_WORDS = frozenset({"yes", "y", "true", "t", "1", "enabled"})
_FALSE_WORDS = frozenset({"no", "n", "false", "f", "0", "disabled"})

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_RULE_HEADERS = (
    "Enabled",
    "Status",
    "Platform",
    "Error_Code",
    "Observation_Template",
    "Template",
    "Observation",
    "Rule_ID",
    "Notes",
)


@dataclass(frozen=True, slots=True)
class ObservationRule:
    """One row of the sheet. ``None`` in a key field means ANY."""

    row_number: int
    status: TestStatus
    platform: Platform | None
    error_code: str | None
    template: str
    enabled: bool = True

    @property
    def key(self) -> tuple[TestStatus, Platform | None, str | None]:
        return (self.status, self.platform, self.error_code)


@dataclass(frozen=True, slots=True)
class ObservationRules:
    """Every enabled rule, indexed by its matching key."""

    rules: tuple[ObservationRule, ...] = ()

    def _index(self) -> dict[tuple[TestStatus, Platform | None, str | None], ObservationRule]:
        return {rule.key: rule for rule in self.rules if rule.enabled}

    def select(
        self,
        status: TestStatus,
        platform: Platform,
        error_code: str | None,
    ) -> ObservationRule | None:
        """The most specific enabled rule for this outcome, or ``None``."""
        index = self._index()
        code = error_code or None
        for candidate in (
            (status, platform, code),
            (status, platform, None),
            (status, None, code),
            (status, None, None),
        ):
            rule = index.get(candidate)
            if rule is not None:
                return rule
        return None

    def template_for(self, status: TestStatus, platform: Platform, error_code: str | None) -> str:
        rule = self.select(status, platform, error_code)
        if rule is not None:
            return rule.template
        return BUILT_IN_OBSERVATIONS.get(status, "{test_id}: {status} ({error_code}).")


def read_observation_rules(
    workbook: Any, *, sheet_name: str = "Observation Rules"
) -> ObservationRules:
    """Read the sheet. An absent sheet simply means "use the built-in wording"."""
    sheet = find_sheet(workbook, sheet_name)
    if sheet is None:
        return ObservationRules()

    rows = sheet_rows(sheet)
    header_row = locate_header_row(rows, _RULE_HEADERS, must_contain="Status")
    if header_row is None:
        return ObservationRules()

    columns = map_headers(rows[header_row - 1])
    template_column = _first_column(columns, ("Observation_Template", "Observation", "Template"))
    if template_column is None:
        raise ConfigurationError(
            f"Sheet '{sheet.title}' has no Observation_Template column, so its rules "
            f"cannot say anything.",
            code=ErrorCode.INVALID_PROFILE,
            platform=Platform.WORKBOOK,
        )

    rules: list[ObservationRule] = []
    for row_number in range(header_row + 1, len(rows) + 1):
        row = rows[row_number - 1]
        if is_blank_row(row):
            continue
        rule = _read_rule(row, row_number, columns, template_column, sheet.title)
        if rule is not None:
            rules.append(rule)

    _reject_duplicates(rules, sheet.title)
    return ObservationRules(tuple(rules))


def _first_column(columns: Mapping[str, int], names: Sequence[str]) -> int | None:
    for name in names:
        index = columns.get(" ".join(name.split()).casefold())
        if index is not None:
            return index
    return None


def _read_rule(
    row: Sequence[Any],
    row_number: int,
    columns: Mapping[str, int],
    template_column: int,
    sheet_title: str,
) -> ObservationRule | None:
    status_text = text_of(cell(row, columns.get("status")))
    template = text_of(cell(row, template_column))
    if not status_text and not template:
        return None

    status = _as_status(status_text)
    if status is None:
        allowed = ", ".join(s.value for s in TestStatus)
        raise ConfigurationError(
            f"Sheet '{sheet_title}' row {row_number}: Status '{status_text}' is not one "
            f"of: {allowed}.",
            code=ErrorCode.INVALID_PROFILE,
            platform=Platform.WORKBOOK,
        )
    if not template:
        raise ConfigurationError(
            f"Sheet '{sheet_title}' row {row_number}: the rule has no observation template.",
            code=ErrorCode.INVALID_PROFILE,
            platform=Platform.WORKBOOK,
        )

    unknown = sorted(set(_PLACEHOLDER_RE.findall(template)) - PLACEHOLDERS)
    if unknown:
        allowed = ", ".join(sorted(PLACEHOLDERS))
        raise ConfigurationError(
            f"Sheet '{sheet_title}' row {row_number}: unknown placeholder(s) "
            f"{', '.join('{' + name + '}' for name in unknown)}. Allowed: {allowed}.",
            code=ErrorCode.INVALID_PROFILE,
            platform=Platform.WORKBOOK,
        )

    return ObservationRule(
        row_number=row_number,
        status=status,
        platform=_as_platform(text_of(cell(row, columns.get("platform"))), sheet_title, row_number),
        error_code=_as_code(text_of(cell(row, columns.get("error_code")))),
        template=template,
        enabled=_as_enabled(text_of(cell(row, columns.get("enabled")))),
    )


def _reject_duplicates(rules: Sequence[ObservationRule], sheet_title: str) -> None:
    """Two enabled rules with one key would make the wording depend on row order."""
    seen: dict[tuple[TestStatus, Platform | None, str | None], int] = {}
    for rule in rules:
        if not rule.enabled:
            continue
        first = seen.get(rule.key)
        if first is not None:
            status, platform, code = rule.key
            raise ConfigurationError(
                f"Sheet '{sheet_title}' rows {first} and {rule.row_number} both match "
                f"(Status={status.value}, Platform={platform.value if platform else 'ANY'}, "
                f"Error_Code={code or 'ANY'}). Remove or disable one: rule selection is "
                f"deterministic and has no priority order.",
                code=ErrorCode.INVALID_PROFILE,
                platform=Platform.WORKBOOK,
            )
        seen[rule.key] = rule.row_number


def render_template(template: str, values: Mapping[str, object], *, max_length: int) -> str:
    """Fill ``template`` from ``values``, replacing only known placeholders.

    Substitution is a regex replacement, not ``str.format``: a stray brace in
    an observation must render as a brace, and no expression, attribute access
    or indexing from a spreadsheet is ever evaluated.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in PLACEHOLDERS:
            return match.group(0)
        value = values.get(name)
        return "" if value is None else str(value)

    filled = _PLACEHOLDER_RE.sub(replace, template)
    return sanitize_text(filled, max_length=max_length)


def _as_status(text: str) -> TestStatus | None:
    candidate = " ".join(text.split()).upper()
    for status in TestStatus:
        if status.value == candidate or status.name == candidate.replace(" ", "_"):
            return status
    return None


def _as_platform(text: str, sheet_title: str, row_number: int) -> Platform | None:
    """``None`` means ANY. An unrecognised word is a mistake, not a wildcard."""
    candidate = text.strip().upper()
    if candidate.casefold() in _ANY_WORDS:
        return None
    for platform in Platform:
        if platform.value == candidate and platform is not Platform.NONE:
            return platform
    allowed = ", ".join(p.value for p in Platform if p is not Platform.NONE)
    raise ConfigurationError(
        f"Sheet '{sheet_title}' row {row_number}: Platform '{text}' is not one of: "
        f"{allowed}, or ANY.",
        code=ErrorCode.INVALID_PROFILE,
        platform=Platform.WORKBOOK,
    )


def _as_code(text: str) -> str | None:
    candidate = text.strip().upper()
    return None if candidate.casefold() in _ANY_WORDS else candidate


def _as_enabled(text: str) -> bool:
    folded = text.strip().casefold()
    if folded in _FALSE_WORDS:
        return False
    if folded in _TRUE_WORDS:
        return True
    return True  # a blank Enabled column means the rule counts
