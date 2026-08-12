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
        # `context` (fan-out op) → semantic's contribution is a similarity search on the target.
        if op not in ("search", "context"):
            return safe_null_result(op, target, engine="semantic", reason="op-not-supported")
        if not self.available:
            return safe_null_result(op, target, engine="semantic", reason="engine-unavailable")
        if not project_root:
            return safe_null_result(op, target, engine="semantic", reason="no-project-root")

        try:
            from codeintel.config import load_config
            from codeintel.semantic_db import SemanticDb
            from codeintel.indexer import Indexer
            from codeintel.searcher import Searcher

            cfg = load_config(project_root)
            model = str(cfg.get("model") or "BAAI/bge-small-en-v1.5")

            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            db = SemanticDb(str(_DB_PATH))
            db.init()

            Indexer(
                db,
                model_name=model,
                window=int(cfg.get("window", 20)),
                stride=int(cfg.get("stride", 10)),
                max_chunks=int(cfg.get("max_chunks", 500)),
            ).index(project_root)

            searcher = Searcher(db, model_name=model)
            if not searcher.has_index(project_root):
                return safe_null_result(op, target, engine="semantic", reason="no-index")

            matches = searcher.search(
                target, project_root, cosine_floor=float(cfg.get("cosine_floor", 0.25))
            )
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
