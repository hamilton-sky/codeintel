from __future__ import annotations

import hashlib
import os
import threading
from typing import Optional

from codeintel.provider import Result


def _compute_hash(target: str, project_root: str) -> str:
    try:
        root = os.path.realpath(project_root) if project_root else ""
        path = os.path.realpath(target)
        if root and path.startswith(root + os.sep) or path == root:
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    return hashlib.sha256(fh.read()).hexdigest()
    except Exception:
        pass
    return hashlib.sha256(target.encode()).hexdigest()


class ContentHashCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key → (content_hash, Result)
        self._store: dict[tuple[str, str, str, str], tuple[str, Result]] = {}

    def get(
        self,
        op: str,
        target: str,
        engine: str,
        project_root: str,
    ) -> Optional[Result]:
        key = (op, target, engine, project_root)
        with self._lock:
            entry = self._store.get(key)
        if entry is None:
            return None
        stored_hash, result = entry
        current_hash = _compute_hash(target, project_root)
        if current_hash == stored_hash:
            return result
        return None

    def put(
        self,
        op: str,
        target: str,
        engine: str,
        project_root: str,
        result: Optional[Result],
    ) -> None:
        if result is None or result.get("result") is None:
            return
        key = (op, target, engine, project_root)
        content_hash = _compute_hash(target, project_root)
        with self._lock:
            self._store[key] = (content_hash, result)
