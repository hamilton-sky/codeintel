"""The first-run experience when the embedding model cannot be downloaded.

`fastembed` fetches ~50 MB of weights from `huggingface.co` the first time the semantic engine
runs, and that is the only outbound request codeintel makes. Behind a proxy it fails, and what a
new user saw was:

    Indexer.index() unrecoverable failure: 403 Forbidden

That is a true statement and a dead end. It names no model, no host, and no fix — a reviewer who
hit it had to read their proxy's logs to learn which host was refused, and never ran a successful
query at all. The project already knew: `docs/install.md` has documented this exact failure and
the `FASTEMBED_CACHE_PATH` workaround since the 2026-08-23 status eval. The knowledge simply never
reached the runtime message, which is the only text a person in that situation actually reads.

These tests guard the three halves of the repair: the hint says the useful things, it stays quiet
about failures it cannot explain, and the README stops promising "out of the box" without saying
which box.
"""
from __future__ import annotations

import re
from pathlib import Path

from codeintel.semantic_db import (
    DEFAULT_MODEL,
    MODEL_CACHE_ENV,
    MODEL_HOST,
    model_fetch_hint,
)

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


class ProxyError(Exception):
    """Stands in for `requests.exceptions.ProxyError` without importing requests."""


def test_hint_names_model_host_and_cache_variable():
    """The three facts the bare exception withheld."""
    hint = model_fetch_hint(ProxyError("403 Forbidden"))
    assert hint is not None
    assert DEFAULT_MODEL in hint          # what is being downloaded
    assert MODEL_HOST in hint             # from where
    assert MODEL_CACHE_ENV in hint        # and how to work around it
    assert "docs/install.md" in hint      # where the long form lives


def test_hint_reports_the_model_actually_configured():
    """A repo pointed at a different model must not be told the default's name."""
    hint = model_fetch_hint(ProxyError("407 Proxy Authentication Required"), "BAAI/bge-base-en")
    assert hint is not None
    assert "BAAI/bge-base-en" in hint
    assert DEFAULT_MODEL not in hint


def test_hint_is_silent_on_failures_it_cannot_explain():
    """Explaining an unrelated failure wrongly is worse than the bare exception.

    An unwritable cache directory and a corrupt database are real index failures with real, and
    different, fixes. Attaching a network explanation to them would send the reader to a proxy
    that was never the problem.
    """
    for exc in (
        PermissionError("cannot write cache directory"),
        OSError("sqlite disk image is malformed"),
        ValueError("chunk exceeds maximum size"),
        MemoryError(),
    ):
        assert model_fetch_hint(exc) is None, exc


def test_hint_never_raises_from_inside_an_except_handler():
    """It is called while another failure is being handled; a second one there is unrecoverable."""
    class Hostile(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no string for you")

    assert model_fetch_hint(Hostile()) is None


def test_indexer_last_error_carries_the_hint(monkeypatch, tmp_path):
    """The wiring, not just the helper: a blocked download must reach `last_error`.

    `last_error` is what `setup --all` prints in its step table and what the semantic provider
    interpolates into its `index-failed` envelope, so this is the string that actually reaches a
    human and an agent respectively.
    """
    from codeintel.indexer import Indexer

    indexer = Indexer.__new__(Indexer)
    indexer.model_name = DEFAULT_MODEL
    monkeypatch.setattr(
        Indexer, "_index",
        lambda self, root: (_ for _ in ()).throw(ProxyError("403 Forbidden")),
    )

    assert indexer.index(str(tmp_path)) == -1
    assert indexer.last_error is not None
    assert "ProxyError: 403 Forbidden" in indexer.last_error   # the original, still intact
    assert MODEL_HOST in indexer.last_error                    # plus what it means
    assert MODEL_CACHE_ENV in indexer.last_error


def test_indexer_last_error_stays_bare_for_an_unrelated_failure(monkeypatch, tmp_path):
    from codeintel.indexer import Indexer

    indexer = Indexer.__new__(Indexer)
    indexer.model_name = DEFAULT_MODEL
    monkeypatch.setattr(
        Indexer, "_index",
        lambda self, root: (_ for _ in ()).throw(PermissionError("read-only file system")),
    )

    assert indexer.index(str(tmp_path)) == -1
    assert indexer.last_error == "PermissionError: read-only file system"


def test_readme_does_not_promise_out_of_the_box_without_the_network_caveat():
    """"Works out of the box" is true only where `huggingface.co` is reachable.

    It is false in restricted CI, on a corporate network, and air-gapped — the difference between
    "pip install and go" and "pip install, then talk to your network team". The caveat existed,
    ~450 lines below the quickstart, which is not where anyone reads it: the claim and its
    condition have to travel together, so every occurrence of the phrase must carry a link to the
    offline install guide within the same paragraph.
    """
    text = README.read_text(encoding="utf-8")
    paragraphs = re.split(r"\n\s*\n", text)
    claims = [p for p in paragraphs if "out of the box" in p]
    assert claims, "the claim moved or was reworded — update this guard with it"
    for para in claims:
        assert "install.md#offline--air-gapped-install" in para, (
            f"unqualified 'out of the box' claim with no offline caveat:\n{para}"
        )
