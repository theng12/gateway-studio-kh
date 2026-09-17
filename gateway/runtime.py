"""Shared runtime singletons.

`store`, `worker` and the catalogue are process-wide by design: one gateway
serves one owner, so a single SQLite connection and a single worker thread are
correct. Keeping them here (rather than constructing them in `api.py`) lets the
admin router and the API share them without an import cycle.
"""

from __future__ import annotations

from .catalog import catalog
from .config import DB_PATH
from .providers import registry
from .store import JobStore
from .worker import Worker

store = JobStore(DB_PATH)
worker = Worker(store)

__all__ = ["catalog", "registry", "store", "worker"]
