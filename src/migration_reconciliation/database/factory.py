"""Executor factories.

The factory is the seam between the runner and any real driver. The runner asks
for an executor by logical connection name; how that connection is established —
fake, ``pyodbc``, ``oracledb`` — is decided here and nowhere else.

Milestone 1 registers no real adapter. When they arrive, each becomes an entry
in :data:`_REAL_ADAPTERS`; the runner does not change.
"""

from __future__ import annotations

from collections.abc import Callable

from ..errors import ReconciliationError
from ..models import DatabaseType
from .base import ExecutorFactory, QueryExecutor, QuerySide
from .fake import FakeQueryExecutor, FakeResultBook

__all__ = [
    "AdapterBuilder",
    "FakeExecutorFactory",
    "create_executor_factory",
    "real_adapters_available",
]

#: Signature a real adapter builder must satisfy when it is registered.
type AdapterBuilder = Callable[[str], QueryExecutor]

#: Deliberately empty in milestone 1. Populating this dict — and nothing else —
#: is what enables real connectivity.
_REAL_ADAPTERS: dict[DatabaseType, AdapterBuilder] = {}


def real_adapters_available() -> bool:
    """True once real database adapters are registered. Always False offline."""
    return bool(_REAL_ADAPTERS)


class FakeExecutorFactory:
    """Hands out :class:`~.fake.FakeQueryExecutor` instances and owns their lifetime.

    One executor is cached per ``(connection, test case, side)`` so that a test
    can assert on exactly the queries a case ran, and so ``close_all`` can prove
    every executor was cleaned up.
    """

    def __init__(self, book: FakeResultBook) -> None:
        self._book = book
        self._executors: dict[tuple[str, str, QuerySide], FakeQueryExecutor] = {}

    @property
    def created(self) -> tuple[FakeQueryExecutor, ...]:
        return tuple(self._executors.values())

    def get_executor(
        self,
        *,
        connection_name: str,
        database_type: DatabaseType,
        test_case_id: str,
        side: QuerySide,
    ) -> QueryExecutor:
        key = (connection_name, test_case_id, side)
        executor = self._executors.get(key)
        if executor is None:
            executor = FakeQueryExecutor(
                connection_name=connection_name,
                database_type=database_type,
                side=side,
                test_case_id=test_case_id,
                response=self._book.response_for(test_case_id, side),
            )
            self._executors[key] = executor
        return executor

    def close_all(self) -> None:
        """Close every executor. One failing close never skips the rest."""
        for executor in self._executors.values():
            try:
                executor.close()
            except Exception:  # cleanup must not mask the error that triggered it
                continue


def create_executor_factory(
    *,
    fake_results: FakeResultBook | None = None,
) -> ExecutorFactory:
    """Build the factory for a run.

    Offline milestone: ``fake_results`` is required. Passing nothing is an
    explicit error rather than a silent fallback to a real connection.
    """
    if fake_results is not None:
        return FakeExecutorFactory(fake_results)
    raise ReconciliationError(
        "No fake results supplied and no real database adapter is registered. "
        "Milestone 1 runs offline only — pass --fake-results <file.toml>."
    )
