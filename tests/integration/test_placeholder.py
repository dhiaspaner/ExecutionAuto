"""Placeholder for future database integration tests.

Nothing here connects to anything. Real integration tests require, in order:

1. Written approval from the person who owns the databases.
2. A read-only account on both the source and the target.
3. A local, uncommitted connection configuration.
4. The ``MIGRATION_RECONCILIATION_ALLOW_DB_TESTS=1`` opt-in below.

They must never run automatically, never be run by an agent, and never contain
credentials or client data.
"""

from __future__ import annotations

import os

import pytest

ALLOW_FLAG = "MIGRATION_RECONCILIATION_ALLOW_DB_TESTS"

pytestmark = pytest.mark.skipif(
    os.environ.get(ALLOW_FLAG) != "1",
    reason=(
        f"Database integration tests are opt-in: set {ALLOW_FLAG}=1 only with explicit "
        "DBA approval and read-only accounts."
    ),
)


def test_database_integration_is_not_enabled_yet() -> None:
    pytest.skip("No database adapter exists in milestone 1.")
