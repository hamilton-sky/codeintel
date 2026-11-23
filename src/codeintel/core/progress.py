"""The progress-reporting seam for indexing.

This is a deliberately tiny, dependency-free contract that lets the indexer say *how far it is*
without knowing anything about terminals, TTYs, or ANSI. The renderer that turns these calls into
a live line (``term.LiveCounter``) lives on the far side of this line: it never sees a chunk, and
the indexer never sees an escape byte. Neither imports the other — the renderer merely *matches*
``ProgressSink``'s shape.

The never-raise guarantee for progress lives in exactly ONE place — ``_Guard``. The indexer calls
the guard's methods unguarded, so a broken, slow, or half-written sink can never abort an index
pass or change the count it returns. That single-choke-point is the whole reason this module exists
rather than the indexer poking a console directly.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProgressSink(Protocol):
    """What the indexer emits while it works. Three calls, because the phases genuinely differ:

    * ``scan`` — the file walk. The file list is a generator, so there is no total; this is a
      running count only (files seen, chunks collected so far).
    * ``load_model`` — a bare marker fired just before the embedding model is materialised. On a
      cold cache that step downloads hundreds of MB and is a *separate* multi-minute stall that
      precedes the first batch; without its own signal it reads as a fresh hang the moment the scan
      counter stops moving.
    * ``embed`` — the batch loop. The total is known up front (``len(new_chunks)``), so this is a
      real ``done``/``total`` and can drive a true percentage.

    Implementations should not raise, but they are not trusted to honour that: every call the
    indexer makes is routed through :class:`_Guard`.
    """

    def scan(self, files: int, chunks: int) -> None: ...
    def load_model(self) -> None: ...
    def embed(self, done: int, total: int) -> None: ...


class _Guard:
    """Null-safe, never-raise adapter around an optional :class:`ProgressSink`.

    ``Indexer`` holds one of these instead of the raw sink and calls it directly. When the sink is
    ``None`` (the default — the MCP server, ``Reindexer``, and every test that doesn't opt in) each
    method is a no-op; when it is present, any exception it throws is swallowed here. This is the
    only spot the contract "progress can never break indexing" is enforced, so it is the only spot
    that needs a test to prove it.
    """

    __slots__ = ("_sink",)

    def __init__(self, sink: ProgressSink | None) -> None:
        self._sink = sink

    def scan(self, files: int, chunks: int) -> None:
        if self._sink is None:
            return
        try:
            self._sink.scan(files, chunks)
        except Exception:
            pass

    def load_model(self) -> None:
        if self._sink is None:
            return
        try:
            self._sink.load_model()
        except Exception:
            pass

    def embed(self, done: int, total: int) -> None:
        if self._sink is None:
            return
        try:
            self._sink.embed(done, total)
        except Exception:
            pass
