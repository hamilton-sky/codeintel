from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class Reindexer:
    def __init__(self, debounce_seconds: float = 30, enabled: bool = True) -> None:
        self._debounce_seconds = debounce_seconds
        self._enabled = (
            os.environ.get("CODEINTEL_REINDEX", "on").strip().lower() != "off"
            and enabled
        )
        self._lock = threading.Lock()
        self._last_fired: dict[str, float] = {}
        # Per-project index generation — bumped when a reindex completes. The gateway
        # folds it into the cache key so a structural answer is invalidated once the
        # index actually moves (a symbol/free-text target has no file content to hash).
        self._generation: dict[str, int] = {}
        self._executor = ThreadPoolExecutor(max_workers=2)

    def generation(self, project_root: str) -> int:
        with self._lock:
            return self._generation.get(project_root, 0)

    def maybe_reindex(self, project_root: str) -> None:
        if not self._enabled:
            return
        if not project_root:
            return

        now = time.monotonic()
        with self._lock:
            last = self._last_fired.get(project_root, 0.0)
            if now - last < self._debounce_seconds:
                return
            self._last_fired[project_root] = now

        self._executor.submit(self._do_reindex, project_root)

    def _do_reindex(self, project_root: str) -> None:
        try:
            self._semantic_reindex(project_root)
            self._graph_reindex(project_root)
            with self._lock:
                self._generation[project_root] = self._generation.get(project_root, 0) + 1
        except Exception as exc:
            logger.warning("Reindexer._do_reindex failed for %s: %s", project_root, exc)

    def _semantic_reindex(self, project_root: str) -> None:
        import pathlib

        from codeintel.semantic_db import SemanticDb, default_db_path
        from codeintel.indexer import Indexer

        # Same per-machine cache the SemanticProvider reads — index and search must
        # never diverge onto different files.
        db_path = default_db_path()
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        db = SemanticDb(db_path)
        try:
            db.init()
            Indexer(db).index(project_root)
        finally:
            db.close()

    def _graph_reindex(self, project_root: str) -> None:
        if not shutil.which("codebase-memory-mcp"):
            return
        try:
            import json
            subprocess.run(
                ["codebase-memory-mcp", "cli", "detect_changes",
                 json.dumps({"project_root": project_root})],
                capture_output=True,
                timeout=120,
            )
        except Exception as exc:
            logger.warning("graph detect_changes failed: %s", exc)
