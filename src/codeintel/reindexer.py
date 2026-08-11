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
        self._executor = ThreadPoolExecutor(max_workers=2)

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
        except Exception as exc:
            logger.warning("Reindexer._do_reindex failed for %s: %s", project_root, exc)

    def _semantic_reindex(self, project_root: str) -> None:
        from codeintel.semantic_db import SemanticDb
        from codeintel.indexer import Indexer

        db_path = os.path.join(project_root, ".codeintel", "semantic.db")
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
            subprocess.run(
                ["codebase-memory-mcp", "cli", "detect_changes",
                 f'{{"project_root": "{project_root}"}}'],
                capture_output=True,
                timeout=120,
            )
        except Exception as exc:
            logger.warning("graph detect_changes failed: %s", exc)
