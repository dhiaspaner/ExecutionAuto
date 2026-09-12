"""Database access layer: protocols, the offline fake, and the factory seam."""

from .base import ExecutorFactory, QueryExecutor, QuerySide, single_scalar
from .factory import FakeExecutorFactory, create_executor_factory, real_adapters_available
from .fake import FakeQueryExecutor, FakeResponse, FakeResultBook, load_fake_results

__all__ = [
    "ExecutorFactory",
    "FakeExecutorFactory",
    "FakeQueryExecutor",
    "FakeResponse",
    "FakeResultBook",
    "QueryExecutor",
    "QuerySide",
    "create_executor_factory",
    "load_fake_results",
    "real_adapters_available",
    "single_scalar",
]
