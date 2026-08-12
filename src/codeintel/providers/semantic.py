from __future__ import annotations

import os
import pathlib

from codeintel.provider import Result, log_swallowed, safe_null_result

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

    def probe(self, project_root: str) -> dict:
        """Never-raise health check for the doctor. READ-ONLY and MODEL-FREE: it opens the db
        read-only and counts this repo's chunks — it must NOT call SemanticDb.init() (a schema
        write) or load fastembed. ``repo_indexed`` is project-scoped (mirrors Searcher.has_index)."""
        if not self.available:
            return {
                "installed": False, "runnable": False, "repo_indexed": False,
                "detail": "fastembed / sqlite-vec not importable",
                "remediation": "pip install fastembed sqlite-vec  (or: pip install -e .)",
            }
        import os
        import sqlite3

        try:
            from codeintel.semantic_db import default_db_path
            db_path = default_db_path()
        except Exception:
            db_path = ""
        if not db_path or not os.path.exists(db_path):
            return {
                "installed": True, "runnable": True, "repo_indexed": False,
                "detail": "no semantic index database yet",
                "remediation": f"codeintel index {project_root}",
            }
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                real = os.path.realpath(project_root) if project_root else ""
                row = conn.execute(
                    "SELECT COUNT(*) FROM chunk_hashes WHERE project_root = ?", (real,)
                ).fetchone()
            finally:
                conn.close()
            count = int(row[0]) if row else 0
        except Exception as exc:
            return {
                "installed": True, "runnable": False, "repo_indexed": False,
                "detail": f"semantic.db present but unreadable ({type(exc).__name__})",
                "remediation": "rm ~/.codeintel/semantic.db && codeintel index <root>",
            }
        if count > 0:
            return {
                "installed": True, "runnable": True, "repo_indexed": True,
                "detail": f"{count} indexed chunks for this repo", "remediation": None,
            }
        return {
            "installed": True, "runnable": True, "repo_indexed": False,
            "detail": "semantic.db present but 0 chunks for this repo",
            "remediation": f"codeintel index {project_root}",
        }

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

            searcher = Searcher(db, model_name=model)

            # A full index pass walks and hashes every file — too expensive to run on every query.
            # The background Reindexer (gated by the CODEINTEL_REINDEX env) already keeps a warm repo
            # fresh, so we only pay the inline pass on a COLD repo (nothing indexed yet). The one
            # exception: when that background reindexer is turned off, the inline pass is the only
            # thing keeping the index current, so we run it every query to preserve freshness.
            background_reindex_off = (
                os.environ.get("CODEINTEL_REINDEX", "on").strip().lower() == "off"
            )
            if background_reindex_off or not searcher.has_index(project_root):
                Indexer(
                    db,
                    model_name=model,
                    window=int(cfg.get("window", 20)),
                    stride=int(cfg.get("stride", 10)),
                    max_chunks=int(cfg.get("max_chunks", 500)),
                    max_total_chunks=int(cfg.get("max_total_chunks", 100000)),
                ).index(project_root)

            if not searcher.has_index(project_root):
                return safe_null_result(
                    op, target, engine="semantic", reason="no-index",
                    hint=f"run: codeintel index {project_root}  (or: codeintel doctor)",
                )

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
        except Exception as exc:
            log_swallowed("SemanticProvider.build_result", exc)
            return safe_null_result(op, target, engine="semantic", reason="provider-error")
