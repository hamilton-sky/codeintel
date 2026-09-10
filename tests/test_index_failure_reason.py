"""An indexing failure must state its cause where the reader is looking.

The first command a new user runs failed with `Indexer.index() unrecoverable failure: 403
Forbidden` — true, and a dead end. It names no model, no host, and no fix; an external reviewer
who hit it had to read their proxy's own logs to learn the refused host was `huggingface.co`, and
never completed a single query.

The first repair for that inferred the explanation AFTER the fact, matching the raised exception
against a list of network-ish substrings. It was wrong in both directions: an unrelated network
failure during a long index pass got the embedding-model story, and a genuine blocked download
whose message contained none of those words got nothing. `EmbeddingModelUnavailable` classifies at
the operation instead — at the point where the model is loaded, there is nothing to infer — and
these tests pin both that message and the call sites that have to show it.
"""
from __future__ import annotations

import ast
import errno
import os
import re
from pathlib import Path

import pytest

from codeintel.semantic_db import (
    DEFAULT_MODEL,
    MODEL_CACHE_ENV,
    MODEL_HOST,
    EmbeddingModelUnavailable,
    model_cache_dir,
    model_is_cached,
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "codeintel"
README = ROOT / "README.md"


class ProxyError(Exception):
    """Stands in for `requests.exceptions.ProxyError` without importing requests."""


# The remedy the download path supplies. Spelled out here rather than imported so a change to the
# wording is a deliberate test edit, not something that silently rewrites what these assert.
_DOWNLOAD_REMEDY = (
    f"fastembed downloads it (~50 MB) from {MODEL_HOST} on first use and codeintel makes no "
    f"other outbound request; check network/proxy access, or set {MODEL_CACHE_ENV} to a "
    f"directory pre-seeded with the model on a connected machine (see docs/install.md, "
    f"'Offline / air-gapped install'), then re-run"
)


def _load_with(cause: BaseException, model_name: str = DEFAULT_MODEL):
    """`load_embedder` with fastembed's constructor forced to raise *cause* — the real path."""
    import builtins

    import codeintel.semantic_db as sdb

    real_import = builtins.__import__

    def _fake(name, *a, **k):
        if name == "fastembed":
            class _TE:
                def __init__(self, *_a, **_k):
                    raise cause
            return type("_M", (), {"TextEmbedding": _TE})
        return real_import(name, *a, **k)

    builtins.__import__ = _fake
    try:
        return sdb.load_embedder(model_name)
    finally:
        builtins.__import__ = real_import


# --------------------------------------------------------------------------------------------- #
# The remedy has to fit the cause — "classify at the operation" is not "assume one cause"
# --------------------------------------------------------------------------------------------- #

def test_an_unsupported_model_is_a_config_error_not_a_network_one():
    """fastembed raises ValueError from its own model list before any request (measured: 0.000s).

    Telling someone whose config names a model that does not exist to go and check their proxy is
    the same defect this whole change exists to remove, pointed the other way: there is no proxy
    to fix and no cache to pre-seed, because nothing was ever going to be fetched.
    """
    cause = ValueError("Model nonsense/x is not supported in TextEmbedding.")
    with pytest.raises(EmbeddingModelUnavailable) as caught:
        _load_with(cause, "nonsense/x")

    msg = str(caught.value)
    assert "check the `model` key" in msg
    assert "list_supported_models" in msg
    assert MODEL_HOST not in msg            # no invented network diagnosis
    assert "proxy" not in msg.lower()
    assert MODEL_CACHE_ENV not in msg       # nothing to pre-seed


@pytest.mark.parametrize(
    "errno_name",
    [
        "EACCES",   # permissions — the only family with its own exception class
        "EPERM",
        "ENOSPC",   # full disk        — a bare OSError
        "EROFS",    # read-only fs     — a bare OSError
        "EDQUOT",   # quota exceeded   — a bare OSError
        "ENOTDIR",  # the cache path is not a directory
        "EISDIR",
    ],
)
def test_every_unwritable_cache_names_the_cache_not_the_network(errno_name, monkeypatch, tmp_path):
    """The download would succeed and then have nowhere to land — a different fix entirely.

    Keyed on errno rather than exception class, because only EACCES/EPERM get a dedicated class.
    A full disk, a read-only filesystem and an exceeded quota all arrive as a bare `OSError`, and
    catching `PermissionError` alone sent those users off to check their proxy.
    """
    code = getattr(errno, errno_name, None)
    if code is None:
        pytest.skip(f"{errno_name} is not defined on this platform")

    monkeypatch.setenv(MODEL_CACHE_ENV, str(tmp_path / "cache"))
    with pytest.raises(EmbeddingModelUnavailable) as caught:
        _load_with(OSError(code, os.strerror(code)))

    msg = str(caught.value)
    assert "cannot be written" in msg
    assert str(tmp_path / "cache") in msg   # WHICH directory
    assert MODEL_HOST not in msg
    assert "proxy" not in msg.lower()


@pytest.mark.parametrize("exc", [
    ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
    TimeoutError(errno.ETIMEDOUT, "Connection timed out"),
    OSError(errno.EHOSTUNREACH, "No route to host"),
])
def test_network_oserrors_are_still_the_download(exc):
    """The other half of the same rule, and the one a broad `except OSError` would have broken.

    `ConnectionRefusedError` and `TimeoutError` ARE `OSError` subclasses. Classifying by class
    instead of errno would route the genuine network failures — the ones this whole change exists
    to explain — into the cache bucket.
    """
    with pytest.raises(EmbeddingModelUnavailable) as caught:
        _load_with(exc)

    msg = str(caught.value)
    assert MODEL_HOST in msg
    assert "cannot be written" not in msg


def test_everything_else_is_the_first_use_download():
    """The remaining case, and the one an external reviewer met as a bare `403 Forbidden`."""
    with pytest.raises(EmbeddingModelUnavailable) as caught:
        _load_with(ProxyError("403 Forbidden"))

    msg = str(caught.value)
    assert MODEL_HOST in msg
    assert MODEL_CACHE_ENV in msg
    assert "docs/install.md" in msg


def test_the_classification_reads_types_not_message_text():
    """A ValueError whose text looks like a network failure is still a ValueError.

    This is the line between classifying and guessing: exception TYPES are contracts, their text
    is not. Matching on the words would put this one back in the download bucket — which is
    precisely the machinery this change deleted.
    """
    cause = ValueError("proxy error 403 forbidden while contacting huggingface.co")
    with pytest.raises(EmbeddingModelUnavailable) as caught:
        _load_with(cause, "nonsense/x")
    assert "check the `model` key" in str(caught.value)


# --------------------------------------------------------------------------------------------- #
# The message: the three things "403 Forbidden" left the reader to guess
# --------------------------------------------------------------------------------------------- #

def test_the_message_names_the_model_the_cause_and_a_next_step():
    msg = str(EmbeddingModelUnavailable(DEFAULT_MODEL, ProxyError("403 Forbidden"), _DOWNLOAD_REMEDY))
    assert DEFAULT_MODEL in msg              # what was being downloaded
    assert MODEL_HOST in msg                 # from where
    assert MODEL_CACHE_ENV in msg            # and how to work around it
    assert "docs/install.md" in msg          # where the long form lives
    assert "ProxyError" in msg               # the type, not just "403 Forbidden"
    assert "403 Forbidden" in msg            # and the original text, intact


def test_the_message_is_one_line():
    """`onboarding` renders it inline (f"indexing failed — {reason}") and so does the `index` CLI.

    A newline here breaks the step table that exists to make the failure legible.
    """
    msg = str(EmbeddingModelUnavailable(DEFAULT_MODEL, ProxyError("403 Forbidden"), _DOWNLOAD_REMEDY))
    assert "\n" not in msg


def test_every_field_is_flattened_not_only_the_cause():
    """One-line has to hold for each interpolated field, not the one that was untrusted first.

    `model_name` arrives straight from config, and `config._coerce` only `strip()`s it — which
    removes surrounding whitespace but not an interior newline. A TOML multi-line string puts one
    right in the middle of the message, and only the cause was being normalised.
    """
    exc = EmbeddingModelUnavailable(
        "BAAI/\nbge-small-en-v1.5", RuntimeError("403\nForbidden"), "check\naccess",
    )
    assert "\n" not in str(exc)
    assert len(str(exc).splitlines()) == 1


def test_a_multiline_model_name_survives_the_real_config_path(tmp_path):
    """Not hypothetical: this is what a valid `.codeintel.toml` can hand the indexer."""
    from codeintel.config import load_config

    (tmp_path / ".codeintel.toml").write_text(
        'model = """BAAI/\nbge-small-en-v1.5"""\n', encoding="utf-8",
    )
    model = str(load_config(str(tmp_path)).get("model"))
    assert "\n" in model, "config still normalises it — this guard can retire"

    exc = EmbeddingModelUnavailable(model, ProxyError("403 Forbidden"), _DOWNLOAD_REMEDY)
    assert "\n" not in str(exc)


def test_a_causeless_exception_still_produces_a_message():
    """`str(exc)` is empty for a bare `Exception()`; the type name keeps the parenthetical from
    rendering as an empty '()'."""
    msg = str(EmbeddingModelUnavailable(DEFAULT_MODEL, RuntimeError(), _DOWNLOAD_REMEDY))
    assert "()" not in msg
    assert "RuntimeError" in msg


def test_it_reports_the_model_actually_configured():
    """A repo pointed at a different model must not be told the default's name."""
    msg = str(EmbeddingModelUnavailable("BAAI/bge-base-en", ProxyError("407"), _DOWNLOAD_REMEDY))
    assert "BAAI/bge-base-en" in msg
    assert DEFAULT_MODEL not in msg


def test_the_cause_is_chained_not_swallowed():
    """`raise ... from exc` — the original traceback stays reachable under CODEINTEL_DEBUG."""
    cause = ProxyError("403 Forbidden")
    with pytest.raises(EmbeddingModelUnavailable) as caught:
        _load_with(cause)
    assert caught.value.__cause__ is cause
    assert caught.value.cause is cause


# --------------------------------------------------------------------------------------------- #
# Classification happens at the operation, not by reading the exception text
# --------------------------------------------------------------------------------------------- #

def test_an_unrelated_failure_is_not_dressed_as_a_model_download(monkeypatch, tmp_path):
    """The false positive the substring approach produced.

    A `ConnectionError` raised deep in a long index pass — nothing to do with fastembed — matched
    "connection" and got the whole embedding-model story appended to it, sending the reader to
    check a proxy that was never involved.
    """
    from codeintel.indexer import Indexer

    indexer = Indexer.__new__(Indexer)
    indexer.model_name = DEFAULT_MODEL
    monkeypatch.setattr(
        Indexer, "_index",
        lambda self, root: (_ for _ in ()).throw(ConnectionError("connection reset by peer")),
    )

    assert indexer.index(str(tmp_path)) == -1
    assert indexer.last_error == "ConnectionError: connection reset by peer"
    assert MODEL_HOST not in indexer.last_error
    assert MODEL_CACHE_ENV not in indexer.last_error


def test_a_blocked_download_with_no_network_words_is_still_caught(monkeypatch, tmp_path):
    """The false negative. This message contains none of the substrings the old list looked for,
    so the explanation that mattered most was withheld exactly when it was needed."""
    from codeintel.indexer import Indexer

    indexer = Indexer.__new__(Indexer)
    indexer.model_name = DEFAULT_MODEL
    monkeypatch.setattr(
        Indexer, "_index",
        lambda self, root: (_ for _ in ()).throw(
            EmbeddingModelUnavailable(DEFAULT_MODEL, RuntimeError("request was denied by policy"),
                                      _DOWNLOAD_REMEDY)
        ),
    )

    assert indexer.index(str(tmp_path)) == -1
    assert MODEL_HOST in indexer.last_error
    assert MODEL_CACHE_ENV in indexer.last_error


def test_last_error_reports_the_model_failure_verbatim(monkeypatch, tmp_path):
    """No class-name prefix: the message is already the whole story, and
    `EmbeddingModelUnavailable:` buries the remedy behind noise."""
    from codeintel.indexer import Indexer

    exc = EmbeddingModelUnavailable(DEFAULT_MODEL, ProxyError("403 Forbidden"), _DOWNLOAD_REMEDY)
    indexer = Indexer.__new__(Indexer)
    indexer.model_name = DEFAULT_MODEL
    monkeypatch.setattr(Indexer, "_index", lambda self, root: (_ for _ in ()).throw(exc))

    assert indexer.index(str(tmp_path)) == -1
    assert indexer.last_error == str(exc)
    assert not indexer.last_error.startswith("EmbeddingModelUnavailable")


def test_other_failures_keep_their_type_prefix(monkeypatch, tmp_path):
    """For an arbitrary exception the TYPE is the information, so dropping it would make this fix
    a regression for every cause that is not the model."""
    from codeintel.indexer import Indexer

    indexer = Indexer.__new__(Indexer)
    indexer.model_name = DEFAULT_MODEL
    monkeypatch.setattr(
        Indexer, "_index",
        lambda self, root: (_ for _ in ()).throw(PermissionError("read-only file system")),
    )

    assert indexer.index(str(tmp_path)) == -1
    assert indexer.last_error == "PermissionError: read-only file system"


def test_the_searcher_reports_it_too(monkeypatch):
    """A query is the other way a cold cache is discovered — stage-qualified, message verbatim."""
    from codeintel.searcher import Searcher

    exc = EmbeddingModelUnavailable(DEFAULT_MODEL, ProxyError("403 Forbidden"), _DOWNLOAD_REMEDY)
    s = Searcher.__new__(Searcher)
    s.model_name = DEFAULT_MODEL
    s.last_query_error = None
    monkeypatch.setattr(Searcher, "_get_embedder", lambda self: (_ for _ in ()).throw(exc))

    assert s._embed_query("anything") is None
    assert s.last_query_error == f"embedding the query failed — {exc}"
    assert MODEL_HOST in s.last_query_error


# --------------------------------------------------------------------------------------------- #
# Every call site that runs an index must be able to SHOW why it failed
# --------------------------------------------------------------------------------------------- #

def _discarded_index_calls(root: Path) -> list[str]:
    """`file:line` for every `.index(...)` whose RESULT IS THROWN AWAY, in a module that builds an
    `Indexer`.

    The check is per call site, and it had to become so. The first version of this census asked
    whether the word `last_error` appeared anywhere in the module — and a module can contain one
    call site that handles the failure and another that discards it. `providers/semantic.py` was
    exactly that: the blocking inline pass read `last_error`, so the file "passed", while the
    background cold-index thread twenty lines from the top dropped the return value on the floor.
    The guard reported compliance for the precise pattern it exists to prevent.

    So the rule now encodes the defect itself rather than a proxy for it: `index()` returns -1 on
    an unrecoverable failure and parks the cause on `last_error`, so a call whose value nobody
    binds or tests is a pass whose outcome nobody can ever report. An `ast.Expr` statement is
    exactly that — a call evaluated for effect, its result discarded.
    """
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        builds_indexer = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Indexer"
            for n in ast.walk(tree)
        )
        if not builds_indexer:
            continue
        for node in ast.walk(tree):
            # A bare expression statement: the value is computed and dropped.
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr == "index":
                out.append(f"{path.relative_to(root)}:{node.lineno}")
    return sorted(out)


def test_no_index_pass_discards_its_outcome():
    """Every `.index()` in the tree binds or tests its result, so every failure can be reported."""
    assert _discarded_index_calls(SRC) == []


def test_the_census_actually_inspects_the_known_call_sites():
    """An empty domain would make the assertion above pass vacuously."""
    sites = {
        p.name
        for p in SRC.rglob("*.py")
        if "Indexer(" in p.read_text(encoding="utf-8")
    }
    assert {"index.py", "reindexer.py", "semantic.py"} <= sites, sites


def test_the_census_can_actually_fail(tmp_path):
    """A guard that cannot fail converts "we did not check" into "green"."""
    (tmp_path / "offender.py").write_text(
        "def go(db, root):\n"
        "    Indexer(db).index(root)\n",          # result discarded — the defect
        encoding="utf-8",
    )
    assert _discarded_index_calls(tmp_path) == ["offender.py:2"]


def test_the_census_does_not_flag_a_handled_call(tmp_path):
    """And it must not fire on the correct shape, or it would be noise rather than a guard."""
    (tmp_path / "ok.py").write_text(
        "def go(db, root):\n"
        "    idx = Indexer(db)\n"
        "    if idx.index(root) < 0:\n"
        "        report(idx.last_error)\n",
        encoding="utf-8",
    )
    assert _discarded_index_calls(tmp_path) == []


def test_the_background_cold_index_reports_its_failure():
    """The site the text-based census missed: a daemon thread whose pass nobody could hear fail.

    `index()` never raises, so the `except` wrapping this thread could not see the most likely
    failure here. The request that started the pass had already returned `indexing-in-progress`,
    and every later query got the same answer forever.
    """
    text = (SRC / "providers" / "semantic.py").read_text(encoding="utf-8")
    assert re.search(r"if\s+indexer\.index\(project_root\)\s*<\s*0:", text)
    assert "background cold index failed" in text


def test_the_cli_no_longer_leads_with_a_dead_pointer():
    """"see the warnings above" may remain as the fallback for a failure with no captured reason,
    but it must not be the only thing the command can say."""
    text = (SRC / "commands" / "index.py").read_text(encoding="utf-8")
    assert "indexer.last_error" in text
    assert 'print(f"index failed — {indexer.last_error}")' in text


def test_the_background_reindexer_does_not_fail_silently():
    """It discarded `index()`'s return entirely, so a failed pass logged nothing — while the
    generation bump went on invalidating every cached answer for an index that had not moved."""
    text = (SRC / "reindexer.py").read_text(encoding="utf-8")
    assert re.search(r"if\s+indexer\.index\(project_root\)\s*<\s*0:", text)
    assert "semantic reindex failed" in text


# --------------------------------------------------------------------------------------------- #
# doctor's model-cache probe (unchanged by this port, still guarded)
# --------------------------------------------------------------------------------------------- #

def test_model_cache_dir_matches_fastembeds_own_resolution(monkeypatch, tmp_path):
    """Pinned against fastembed rather than asserted from memory — it is duplicated on purpose,
    because `define_cache_dir` mkdirs as a side effect and a read-only probe must not."""
    fastembed_utils = pytest.importorskip("fastembed.common.utils")

    monkeypatch.setenv(MODEL_CACHE_ENV, str(tmp_path / "explicit"))
    assert model_cache_dir() == str(tmp_path / "explicit")

    monkeypatch.delenv(MODEL_CACHE_ENV)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    assert Path(model_cache_dir()).resolve() == Path(fastembed_utils.define_cache_dir()).resolve()


def test_probing_the_cache_never_creates_it(monkeypatch, tmp_path):
    target = tmp_path / "not-yet"
    monkeypatch.setenv(MODEL_CACHE_ENV, str(target))
    assert model_is_cached() is False
    assert not target.exists()


def test_a_different_models_weights_do_not_count_as_this_one(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    (cache / "models--qdrant--all-MiniLM-L6-v2-onnx").mkdir(parents=True)
    (cache / "models--qdrant--all-MiniLM-L6-v2-onnx" / "model.onnx").write_bytes(b"\x00")
    monkeypatch.setenv(MODEL_CACHE_ENV, str(cache))

    assert model_is_cached(DEFAULT_MODEL) is False
    assert model_is_cached("sentence-transformers/all-MiniLM-L6-v2") is True


def test_readme_does_not_promise_out_of_the_box_without_the_network_caveat():
    """The claim is true only where `huggingface.co` is reachable; the caveat has to travel with
    it, not sit 450 lines below the quickstart."""
    paragraphs = re.split(r"\n\s*\n", README.read_text(encoding="utf-8"))
    claims = [p for p in paragraphs if "out of the box" in p]
    assert claims, "the claim moved or was reworded — update this guard with it"
    for para in claims:
        assert "install.md#offline--air-gapped-install" in para, para
