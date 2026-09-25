"""Shared runtime singletons.

`store`, `worker` and the catalogue are process-wide by design: one gateway
serves one owner, so a single SQLite connection and one job store are correct.
Keeping them here (rather than constructing them in `api.py`) lets the admin
router and the API share them without an import cycle.

**`worker` is still one object, but it is no longer one thread.** It dispatches to
bounded concurrency — see `gateway/concurrency.py` for the three ceilings, and
`worker.Worker` for why a `ThreadPoolExecutor` was deliberately not used. The
connection stays shared: `sqlite3.threadsafety` is 3 (serialized) on this
interpreter, and every read-modify-write already holds the store's lock.
"""

from __future__ import annotations

import time

from .catalog import catalog
from .config import DB_PATH
from .providers import registry
from .store import JobStore
from .worker import Worker

#: When this process came up. The dashboard reports it as uptime, which is the
#: cheapest honest signal that the service is actually being supervised.
STARTED_AT = time.time()

store = JobStore(DB_PATH)
worker = Worker(store)

__all__ = ["STARTED_AT", "catalog", "registry", "store", "worker"]
