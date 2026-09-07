"""An indexing failure must state its cause where the reader is looking.

`Indexer.index()` returns -1 and parks the cause on `last_error` precisely so callers can SHOW a
reason rather than log one. Two call sites had already been fixed for discarding it — the semantic
provider and `run_setup` both carry comments naming "a blocked model download, an unwritable cache
directory" as the cause that goes missing. The `index` CLI was the third, and printed

    index failed - the indexer could not complete (see the warnings above)

while the reason it had been handed sat unread on the instance it threw away. Pointing at "the
warnings above" fails twice over: under a live progress line those warnings are routed through the
counter, and the underlying exception is often a bare transport error ("403 Forbidden") that
explains nothing on its own.

So there are two tiers here: the message itself, and a census that asserts the invariant over every
call site instead of over the last one that broke it.
"""
from __future__ import annotations

import ast
import pathlib

from codeintel.indexer import EmbeddingModelUnavailable, Indexer

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "codeintel"


# --------------------------------------------------------------------------- the message

def test_model_failure_names_the_model_the_cause_and_a_next_step():
    """The three things "403 Forbidden" left the reader to guess."""
    msg = str(EmbeddingModelUnavailable("BAAI/bge-small-en-v1.5", Exception("403 Forbidden")))
    assert "BAAI/bge-small-en-v1.5" in msg, "names the model"
    assert "403 Forbidden" in msg, "preserves the underlying cause verbatim"
    assert "huggingface.co" in msg, "names where the download comes from"
    assert "re-run" in msg, "ends on an action"


def test_the_message_is_one_line():
    """`onboarding` renders it inline (f"indexing failed - {reason}") and so does the CLI. A
    newline here would break the step table that exists to make the failure legible."""
    msg = str(EmbeddingModelUnavailable("m", Exception("boom")))
    assert "\n" not in msg


def test_a_causeless_exception_still_produces_a_message():
    """`str(exc)` is empty for e.g. a bare `Exception()`; falling back to the type name keeps the
    parenthetical from rendering as an empty '()'."""
    msg = str(EmbeddingModelUnavailable("m", TimeoutError()))
    assert "TimeoutError" in msg
    assert "()" not in msg


def test_index_reports_the_model_failure_verbatim(monkeypatch, tmp_path):
    """`last_error` must not re-wrap this one as "EmbeddingModelUnavailable: ..." — the message is
    already the whole story, and a class-name prefix buries the remedy behind noise."""
    idx = Indexer.__new__(Indexer)
    idx.last_error = None

    def _boom(_self, _root):
        raise EmbeddingModelUnavailable("BAAI/bge-small-en-v1.5", Exception("403 Forbidden"))

    monkeypatch.setattr(Indexer, "_index", _boom, raising=True)
    assert Indexer.index(idx, str(tmp_path)) == -1
    assert idx.last_error is not None
    assert not idx.last_error.startswith("EmbeddingModelUnavailable")
    assert "BAAI/bge-small-en-v1.5" in idx.last_error


def test_other_failures_keep_their_type_prefix(monkeypatch, tmp_path):
    """The un-classified path is unchanged: for an arbitrary exception the type IS the information,
    so dropping it would make this fix a regression for every other cause."""
    idx = Indexer.__new__(Indexer)
    idx.last_error = None

    def _boom(_self, _root):
        raise PermissionError("cache dir not writable")

    monkeypatch.setattr(Indexer, "_index", _boom, raising=True)
    assert Indexer.index(idx, str(tmp_path)) == -1
    assert idx.last_error == "PermissionError: cache dir not writable"


# --------------------------------------------------------------------------- the census

def _modules_that_run_an_index() -> list[pathlib.Path]:
    """Every module that both constructs an `Indexer` and calls `.index(...)` — i.e. every module
    that can be handed a `last_error`. Derived, never hand-listed: a hand-listed population is what
    let this defect reach a third call site."""
    found: list[pathlib.Path] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        constructs = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Indexer"
            for n in ast.walk(tree)
        )
        indexes = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "index"
            for n in ast.walk(tree)
        )
        if constructs and indexes and path.name != "indexer.py":
            found.append(path)
    return found


def test_every_index_call_site_consults_last_error():
    sites = _modules_that_run_an_index()
    assert len(sites) >= 3, f"census looks broken, found only {[p.name for p in sites]}"
    silent = [p.name for p in sites if "last_error" not in p.read_text(encoding="utf-8")]
    assert not silent, (
        "these run an index pass but never read `last_error`, so a failure there can only be "
        f"reported as 'something went wrong': {silent}"
    )


def test_the_census_can_actually_fail(tmp_path):
    """A guard that cannot fail converts 'we did not check' into 'green'. Point the same census
    logic at a tree containing a violation and confirm it reports it."""
    rogue = tmp_path / "rogue.py"
    rogue.write_text("def go(db, root):\n    return Indexer(db).index(root)\n")
    tree = ast.parse(rogue.read_text())
    constructs = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "Indexer" for n in ast.walk(tree))
    indexes = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "index" for n in ast.walk(tree))
    assert constructs and indexes, "the fixture must look like a real call site"
    assert "last_error" not in rogue.read_text(), "and it is exactly what the census flags"


def test_the_cli_no_longer_leads_with_a_dead_pointer():
    """The "see the warnings above" fallback may remain for the case where no reason was captured,
    but it must not be the only thing the command can say.

    Counted over STRING LITERALS rather than raw file text: the phrase also appears in a comment
    explaining why it is now a fallback, and a guard that a comment can trip is a guard that gets
    deleted rather than fixed."""
    tree = ast.parse((SRC / "commands" / "index.py").read_text(encoding="utf-8"))
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    dead_pointer = [t for t in literals if "see the warnings above" in t]
    assert len(dead_pointer) <= 1, (
        f"the dead pointer survives only as a last-resort fallback, found {len(dead_pointer)}")
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "last_error" in names, "the CLI must read the reason it was handed"
