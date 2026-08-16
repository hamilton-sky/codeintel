from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from typing import Optional

from codeintel.provider import Result


def _compute_hash(target: str, project_root: str) -> str:
    try:
        root = os.path.realpath(project_root) if project_root else ""
        path = os.path.realpath(target)
        if (root and path.startswith(root + os.sep)) or path == root:
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    return hashlib.sha256(fh.read()).hexdigest()
    except Exception:
        pass
    return hashlib.sha256(target.encode()).hexdigest()


class ContentHashCache:
    # Bounded: the server builds ONE gateway and reuses it across every request, so an unbounded
    # store would grow for the life of the process. Capped as an LRU — past _max_entries the
    # least-recently-used entry is evicted. Sized generously; an agent session touches at most a
    # few hundred distinct (op, target, engine, root) keys, each holding a small Result.
    def __init__(self, max_entries: int = 1024) -> None:
        self._lock = threading.Lock()
        self._max_entries = max(1, int(max_entries))
        # key → (content_hash, Result); OrderedDict preserves insertion/access order for LRU.
        self._store: "OrderedDict[tuple[str, str, str, str], tuple[str, Result]]" = OrderedDict()

    def get(
        self,
        op: str,
        target: str,
        engine: str,
        project_root: str,
        freshness: int = 0,
    ) -> Optional[Result]:
        key = (op, target, engine, project_root)
        with self._lock:
            entry = self._store.get(key)
        if entry is None:
            return None
        stored_hash, result = entry
        # _compute_hash may read the target file — keep it OUTSIDE the lock (original intent).
        current_hash = f"{_compute_hash(target, project_root)}:{freshness}"
        if current_hash != stored_hash:
            return None
        with self._lock:
            if key in self._store:  # may have been evicted between the two locked sections
                self._store.move_to_end(key)  # mark most-recently-used
        return result

    def put(
        self,
        op: str,
        target: str,
        engine: str,
        project_root: str,
        result: Optional[Result],
        freshness: int = 0,
    ) -> None:
        if result is None or result.get("result") is None:
            return
        key = (op, target, engine, project_root)
        # Fold the project's index generation into the stored hash so a completed reindex
        # (which bumps `freshness`) forces a refresh even when the target isn't a file whose
        # content hash would change — i.e. every symbol/free-text query.
        content_hash = f"{_compute_hash(target, project_root)}:{freshness}"
        with self._lock:
            self._store[key] = (content_hash, result)
            self._store.move_to_end(key)  # most-recently-used
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        """Drop every entry. For events that change what an engine *can* answer rather than what
        the content says — the freshness token and the content hash can't see those. Currently:
        a new engine being attached to the gateway mid-process (see ``Gateway.adopt_provider``)."""
        with self._lock:
            self._store.clear()
