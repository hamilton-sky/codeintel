from __future__ import annotations

import hashlib
import logging
import os
import struct
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeintel.semantic_db import SemanticDb

logger = logging.getLogger(__name__)

_INDEXED_EXTS = frozenset({
    ".py", ".ts", ".js", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".md"
})
_SKIP_DIRS = frozenset({"__pycache__", ".git", "node_modules"})


class Indexer:
    def __init__(
        self,
        db: SemanticDb,
        model_name: str = "BAAI/bge-small-en-v1.5",
        window: int = 20,
        stride: int = 10,
        max_chunks: int = 500,
    ) -> None:
        self.db = db
        self.model_name = model_name
        self.window = window
        self.stride = stride
        self.max_chunks = max_chunks
        self._embedder = None

    def _get_embedder(self):
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(model_name=self.model_name)
        return self._embedder

    def index(self, project_root: str) -> int:
        """Return count of newly embedded chunks, or -1 on unrecoverable failure."""
        try:
            return self._index(project_root)
        except Exception as exc:
            logger.error("Indexer.index() unrecoverable failure: %s", exc)
            return -1

    def _cleanup_deleted(self, root: Path) -> None:
        conn = self.db.conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT file_path FROM chunk_hashes"
            ).fetchall()
            deleted_paths = [
                row[0] for row in rows if not (root / row[0]).exists()
            ]
            for fp in deleted_paths:
                chunk_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT chunk_id FROM chunk_hashes WHERE file_path = ?", (fp,)
                    ).fetchall()
                ]
                for cid in chunk_ids:
                    conn.execute(
                        "DELETE FROM code_embeddings WHERE chunk_id = ?", (cid,)
                    )
                    conn.execute(
                        "DELETE FROM chunk_hashes WHERE chunk_id = ?", (cid,)
                    )
            conn.commit()
        except Exception as exc:
            logger.warning("Cleanup pass failed: %s", exc)

    def _walk_files(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.endswith(".egg-info")
            ]
            for fname in filenames:
                if Path(fname).suffix.lower() in _INDEXED_EXTS:
                    yield Path(dirpath) / fname

    def _collect_new_chunks(
        self, root: Path
    ) -> list[tuple[str, str, str, int, str]]:
        """Walk files; return (chunk_id, text, rel_path, start, hash) for new/changed chunks."""
        conn = self.db.conn()
        new_chunks: list[tuple[str, str, str, int, str]] = []

        for filepath in self._walk_files(root):
            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except FileNotFoundError:
                logger.debug("file disappeared: %s", filepath)
                continue
            except Exception as exc:
                logger.debug("skipping %s: %s", filepath, exc)
                continue

            rel_path = str(filepath.relative_to(root))
            chunk_count = 0

            for chunk_start in range(0, len(lines), self.stride):
                if chunk_count >= self.max_chunks:
                    logger.debug(
                        "chunk cap hit for %s, truncating at %d",
                        rel_path,
                        self.max_chunks,
                    )
                    break

                chunk_lines = lines[chunk_start: chunk_start + self.window]
                if not chunk_lines:
                    break

                chunk_text = "".join(chunk_lines)
                chunk_id = f"{rel_path}:{chunk_start}"
                content_hash = hashlib.sha256(chunk_text.encode()).hexdigest()[:16]

                try:
                    row = conn.execute(
                        "SELECT content_hash FROM chunk_hashes WHERE chunk_id = ?",
                        (chunk_id,),
                    ).fetchone()
                    if row and row[0] == content_hash:
                        chunk_count += 1
                        continue
                except Exception as exc:
                    logger.debug("hash check failed for %s: %s", chunk_id, exc)

                new_chunks.append(
                    (chunk_id, chunk_text, rel_path, chunk_start, content_hash)
                )
                chunk_count += 1

        return new_chunks

    def _embed_and_write(
        self, new_chunks: list[tuple[str, str, str, int, str]]
    ) -> int:
        embedder = self._get_embedder()  # may raise → propagates to index() → returns -1
        conn = self.db.conn()
        embedded_count = 0
        batch_size = 32

        for i in range(0, len(new_chunks), batch_size):
            batch = new_chunks[i: i + batch_size]
            texts = [c[1] for c in batch]

            try:
                embeddings = list(embedder.embed(texts))
            except Exception as exc:
                logger.warning("embedding batch %d failed: %s", i // batch_size, exc)
                continue

            for j, (chunk_id, _, rel_path, chunk_start, content_hash) in enumerate(batch):
                if j >= len(embeddings):
                    break
                try:
                    vec = embeddings[j]
                    vec_bytes = struct.pack(f"{len(vec)}f", *vec)
                    conn.execute(
                        "INSERT OR REPLACE INTO code_embeddings(chunk_id, embedding)"
                        " VALUES (?, ?)",
                        (chunk_id, vec_bytes),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO chunk_hashes"
                        "(chunk_id, file_path, chunk_start, content_hash)"
                        " VALUES (?, ?, ?, ?)",
                        (chunk_id, rel_path, chunk_start, content_hash),
                    )
                    embedded_count += 1
                except Exception as exc:
                    logger.warning("writing chunk %s failed: %s", chunk_id, exc)

            try:
                conn.commit()
            except Exception as exc:
                logger.warning("commit failed after batch %d: %s", i // batch_size, exc)

        return embedded_count

    def _index(self, project_root: str) -> int:
        if not project_root:
            return 0

        root = Path(project_root)
        if not root.exists():
            return 0

        self._cleanup_deleted(root)

        new_chunks = self._collect_new_chunks(root)
        if not new_chunks:
            return 0

        return self._embed_and_write(new_chunks)
