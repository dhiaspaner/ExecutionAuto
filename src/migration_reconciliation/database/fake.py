"""Offline fake executor and its TOML response book.

Milestone 1 ships no real driver. :class:`FakeQueryExecutor` implements the
:class:`~.base.QueryExecutor` protocol against scripted answers, so the whole
pipeline — read, validate, execute, compare, write — is exercised end to end
with no database, no credentials and no network.

The response book is plain TOML data read with :mod:`tomllib`; nothing in it is
evaluated.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import DatabaseExecutionError, ReconciliationError, SqlSyntaxError
from ..models import ConnectionIdentity, DatabaseType, ScalarValue
from .base import QuerySide, single_scalar

__all__ = [
    "FakeQueryExecutor",
    "FakeResponse",
    "FakeResultBook",
    "load_fake_results",
    "parse_fake_results",
]

SUPPORTED_FIXTURE_VERSIONS: frozenset[str] = frozenset({"1.0"})

_ALLOWED_TOP_LEVEL_KEYS = frozenset({"version", "defaults", "case"})
_ALLOWED_DEFAULT_KEYS = frozenset({"on_unknown_case"})
_ALLOWED_CASE_KEYS = frozenset(
    {
        "test_case_id",
        "source_result",
        "target_result",
        "source_error",
        "target_error",
        "source_syntax_error",
        "target_syntax_error",
        "source_row_count",
        "target_row_count",
        "source_column_count",
        "target_column_count",
    }
)
_UNKNOWN_CASE_POLICIES = frozenset({"error", "null"})


@dataclass(frozen=True, slots=True)
class FakeResponse:
    """The scripted answer for one test case on one side.

    ``error`` wins over ``value``. ``row_count`` and ``column_count`` let a
    fixture reproduce the "query returned the wrong shape" condition that real
    adapters must also reject, and ``syntax_error`` reproduces a query the
    database refuses to compile, which a run must find before it executes
    anything.
    """

    value: ScalarValue = None
    error: str | None = None
    syntax_error: str | None = None
    row_count: int = 1
    column_count: int = 1

    def check(self, label: str) -> None:
        """Raise if this query is scripted not to compile."""
        if self.syntax_error is not None:
            raise SqlSyntaxError(f"{label} was rejected before execution: {self.syntax_error}")

    def resolve(self, label: str) -> ScalarValue:
        if self.error is not None:
            raise DatabaseExecutionError(self.error)
        rows = [[self.value] * self.column_count for _ in range(self.row_count)]
        return single_scalar(rows, label=label)


@dataclass(frozen=True, slots=True)
class FakeResultBook:
    """All scripted answers for a run, keyed by test case id."""

    responses: dict[str, dict[QuerySide, FakeResponse]] = field(default_factory=dict)
    on_unknown_case: str = "error"

    def response_for(self, test_case_id: str, side: QuerySide) -> FakeResponse:
        case = self.responses.get(test_case_id)
        if case is None:
            if self.on_unknown_case == "null":
                return FakeResponse(value=None)
            raise DatabaseExecutionError(f"No fake result defined for test case '{test_case_id}'")
        return case.get(side, FakeResponse(value=None))


class FakeQueryExecutor:
    """A :class:`~.base.QueryExecutor` backed by scripted answers."""

    def __init__(
        self,
        *,
        connection_name: str,
        database_type: DatabaseType,
        side: QuerySide,
        test_case_id: str,
        response: FakeResponse,
    ) -> None:
        self.connection_name = connection_name
        self.database_type = database_type
        self.side = side
        self.test_case_id = test_case_id
        self.response = response
        #: Queries this executor was asked to run, for test assertions.
        self.executed_sql: list[str] = []
        #: Queries this executor was asked to check, for test assertions.
        self.validated_sql: list[str] = []
        self.closed = False
        self.close_count = 0

    def test_connection(self) -> ConnectionIdentity:
        self._assert_open()
        return ConnectionIdentity(
            connection_name=self.connection_name,
            database_type=self.database_type,
            server_description="offline-fake",
            database_name=self.connection_name,
            account_name="",
            product_version="fake-executor/1.0",
        )

    def validate_syntax(self, sql: str, timeout_seconds: int) -> None:
        """Answer from the script, and execute nothing whatever the answer is."""
        self._assert_open()
        if timeout_seconds <= 0:
            raise DatabaseExecutionError(
                f"Timeout must be greater than 0 seconds (got {timeout_seconds})"
            )
        self.validated_sql.append(sql)
        self.response.check(label=f"{self.side.value.capitalize()} query")

    def execute_scalar(self, sql: str, timeout_seconds: int) -> ScalarValue:
        self._assert_open()
        if timeout_seconds <= 0:
            raise DatabaseExecutionError(
                f"Timeout must be greater than 0 seconds (got {timeout_seconds})"
            )
        self.executed_sql.append(sql)
        return self.response.resolve(label=f"{self.side.value.capitalize()} query")

    def close(self) -> None:
        """Idempotent, mirroring the cleanup contract real adapters must honour."""
        self.closed = True
        self.close_count += 1

    def _assert_open(self) -> None:
        if self.closed:
            raise DatabaseExecutionError(f"Connection '{self.connection_name}' is already closed")


def load_fake_results(path: str | Path) -> FakeResultBook:
    """Read and validate a fake-results fixture."""
    fixture_path = Path(path)
    try:
        raw = fixture_path.read_bytes()
    except OSError as exc:
        raise ReconciliationError(
            f"Cannot read fake results file '{fixture_path}': {exc.strerror}"
        ) from None
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ReconciliationError(f"'{fixture_path}' is not valid UTF-8") from None
    except tomllib.TOMLDecodeError as exc:
        raise ReconciliationError(f"'{fixture_path}' is not valid TOML: {exc}") from None
    return parse_fake_results(document, source=str(fixture_path))


def parse_fake_results(document: dict[str, Any], *, source: str = "<fixture>") -> FakeResultBook:
    """Validate an already-parsed fake-results document."""
    unknown = sorted(set(document) - _ALLOWED_TOP_LEVEL_KEYS)
    if unknown:
        raise ReconciliationError(f"{source}: unknown key(s): {', '.join(unknown)}")

    version = document.get("version")
    if not isinstance(version, str) or version not in SUPPORTED_FIXTURE_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_FIXTURE_VERSIONS))
        raise ReconciliationError(
            f"{source}: version must be one of: {supported} (got {version!r})"
        )

    defaults = document.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ReconciliationError(f"{source}: [defaults] must be a table")
    unknown = sorted(set(defaults) - _ALLOWED_DEFAULT_KEYS)
    if unknown:
        raise ReconciliationError(f"{source}: unknown key(s) in [defaults]: {', '.join(unknown)}")
    on_unknown_case = defaults.get("on_unknown_case", "error")
    if on_unknown_case not in _UNKNOWN_CASE_POLICIES:
        allowed = ", ".join(sorted(_UNKNOWN_CASE_POLICIES))
        raise ReconciliationError(f"{source}: [defaults] on_unknown_case must be one of: {allowed}")

    cases = document.get("case", [])
    if not isinstance(cases, list):
        raise ReconciliationError(f"{source}: [[case]] must be an array of tables")

    responses: dict[str, dict[QuerySide, FakeResponse]] = {}
    for index, entry in enumerate(cases, start=1):
        where = f"[[case]] #{index}"
        if not isinstance(entry, dict):
            raise ReconciliationError(f"{source}: {where} must be a table")
        unknown = sorted(set(entry) - _ALLOWED_CASE_KEYS)
        if unknown:
            raise ReconciliationError(f"{source}: unknown key(s) in {where}: {', '.join(unknown)}")
        test_case_id = entry.get("test_case_id")
        if not isinstance(test_case_id, str) or not test_case_id.strip():
            raise ReconciliationError(f"{source}: {where} requires a non-empty test_case_id")
        test_case_id = test_case_id.strip()
        if test_case_id in responses:
            raise ReconciliationError(f"{source}: duplicate test_case_id '{test_case_id}'")
        responses[test_case_id] = {
            side: _parse_response(entry, side, where, source) for side in QuerySide
        }
    return FakeResultBook(responses=responses, on_unknown_case=on_unknown_case)


def _parse_response(
    entry: dict[str, Any], side: QuerySide, where: str, source: str
) -> FakeResponse:
    prefix = side.value
    error = entry.get(f"{prefix}_error")
    if error is not None and not isinstance(error, str):
        raise ReconciliationError(f"{source}: {where} {prefix}_error must be a string")
    syntax_error = entry.get(f"{prefix}_syntax_error")
    if syntax_error is not None and not isinstance(syntax_error, str):
        raise ReconciliationError(f"{source}: {where} {prefix}_syntax_error must be a string")
    value = entry.get(f"{prefix}_result")
    if value is not None and not isinstance(value, str | int | float | bool):
        raise ReconciliationError(
            f"{source}: {where} {prefix}_result must be a scalar (string, number or boolean)"
        )
    return FakeResponse(
        value=value,
        error=error,
        syntax_error=syntax_error,
        row_count=_parse_count(entry, f"{prefix}_row_count", where, source),
        column_count=_parse_count(entry, f"{prefix}_column_count", where, source),
    )


def _parse_count(entry: dict[str, Any], key: str, where: str, source: str) -> int:
    value = entry.get(key, 1)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReconciliationError(f"{source}: {where} {key} must be an integer of 0 or more")
    return value
