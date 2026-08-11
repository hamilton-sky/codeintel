"""End-to-end smoke tests driving the full codeintel pipeline on a fixture repo."""
from __future__ import annotations

import shutil
from unittest.mock import patch

import numpy as np
import pytest

import codeintel.providers.semantic as _sem_mod
from codeintel.gateway import Gateway
from codeintel.providers.graph import GraphProvider
from codeintel.providers.lsp import LspProvider
from codeintel.providers.semantic import SemanticProvider


class _FakeTextEmbedding:
    """Deterministic stub: yields 384-dim numpy arrays filled with 0.1."""

    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        texts = list(texts)
        return [np.full(384, 0.1, dtype=np.float32) for _ in texts]


@pytest.fixture
def fixture_repo(tmp_path):
    (tmp_path / "auth.py").write_text(
        "def authenticate(user, password):\n"
        "    \"\"\"Authenticate a user and return a token.\"\"\"\n"
        "    token = generate_token(user)\n"
        "    return token\n"
        "\n"
        "def generate_token(user):\n"
        "    return f'tok_{user}'\n"
    )
    (tmp_path / "parser.py").write_text(
        "def parse(text):\n"
        "    \"\"\"Parse text using grammar rules.\"\"\"\n"
        "    grammar = load_grammar()\n"
        "    return grammar.parse(text)\n"
        "\n"
        "def load_grammar():\n"
        "    return None\n"
    )
    (tmp_path / "db.py").write_text(
        "def connect(host, port):\n"
        "    \"\"\"Open a database connection.\"\"\"\n"
        "    return Connection(host, port)\n"
        "\n"
        "def query(conn, sql):\n"
        "    \"\"\"Execute a SQL query on the connection.\"\"\"\n"
        "    return conn.execute(sql)\n"
    )
    return tmp_path


def test_e2e_search_returns_ranked_result(fixture_repo, tmp_path, monkeypatch):
    db_path = tmp_path / "e2e_semantic.db"
    monkeypatch.setattr(_sem_mod, "_DB_PATH", db_path)
    monkeypatch.setattr(_sem_mod, "_DEPS_OK", True)

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        result = SemanticProvider().build_result(
            "search", "authenticate", [], 0, str(fixture_repo)
        )

    assert result is not None
    assert result["result"] is not None
    assert "auth.py" in result["result"]


def test_e2e_gateway_lsp_never_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a: None)
    gw = Gateway(lsp=LspProvider())
    r = gw.query(op="symbol", target="authenticate", engine="lsp")
    assert r["ok"] is True


def test_e2e_gateway_graph_never_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a: None)
    gw = Gateway(graph=GraphProvider())
    r = gw.query(op="callers", target="authenticate", engine="graph")
    assert r["ok"] is True


def test_e2e_full_pipeline_ok_envelope():
    gw = Gateway()
    r = gw.query(op="nonsense_op", target="XYZXYZ_no_such_symbol", engine="auto")
    assert {"ok", "result", "engine", "cached"}.issubset(r.keys())
