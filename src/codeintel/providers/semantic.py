from __future__ import annotations

import pathlib

from codeintel.provider import Result, safe_null_result

try:
    import fastembed  # noqa: F401
    import sqlite_vec  # noqa: F401
    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False

_DB_PATH = pathlib.Path.home() / ".codeintel" / "semantic.db"


class SemanticProvider:
    """Real semantic search provider backed by SemanticDb and Searcher."""

    @property
    def available(self) -> bool:
        return _DEPS_OK

    def build_result(
        self,
        op: str,
        target: str,
        files: list[str],
        budget: int,
        project_root: str,
    ) -> Result:
        if op != "search":
            return safe_null_result(op, target, engine="semantic", reason="op-not-supported")
        if not self.available:
            return safe_null_result(op, target, engine="semantic", reason="engine-unavailable")
        if not project_root:
            return safe_null_result(op, target, engine="semantic", reason="no-project-root")

        try:
            from codeintel.semantic_db import SemanticDb
            from codeintel.indexer import Indexer
            from codeintel.searcher import Searcher

            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            db = SemanticDb(str(_DB_PATH))
            db.init()

            Indexer(db).index(project_root)
            matches = Searcher(db).search(target, project_root)
            if not matches:
                return safe_null_result(op, target, engine="semantic", reason="below-floor")

            lines = [
                f"{m['path']}:{m['line']} | {m['snippet'].splitlines()[0] if m['snippet'].splitlines() else m['snippet']}"
                for m in matches
            ]
            result: Result = {
                "ok": True,
                "op": op,
                "target": target,
                "result": "\n".join(lines),
                "engine": "semantic",
                "cached": False,
            }
            return result
        except Exception:
            return safe_null_result(op, target, engine="semantic", reason="provider-error")
