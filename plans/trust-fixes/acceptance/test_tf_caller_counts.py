"""Story: an agent reading caller counts.

Module-scope rows are imports, not direct calls; a narrowed target drops USAGE rows that merely share
its name; the ambiguity banner says how many symbols have edges and how many more exist.
"""
from __future__ import annotations

import re

from _fakegraph import Repo, gap_kinds


# Spec: A caller row at module scope ("module scope of x.py") counts as an import, not a direct
# call. The heading reads like "4 direct calls, 10 files import it (call sites inside not
# resolved)", and the answer has an `import-only-callers` gap.
def test_module_scope_caller_rows_count_as_imports_not_direct_calls(tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch)
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/core.py", "def safe_null_result():\n    return None\n")
    repo.write("pkg/t1.py", "from pkg.core import safe_null_result\n\n\n"
                            "def test_one():\n    return safe_null_result()\n")
    repo.write("pkg/t2.py", "from pkg.core import safe_null_result\n\n\n"
                            "def test_two():\n    return safe_null_result()\n")
    for name in ("gateway", "server", "provider"):
        repo.write(f"pkg/{name}.py", "from pkg.core import safe_null_result\n\n\n"
                                     f"class X:\n    def go(self):\n"
                                     f"        return safe_null_result()\n")
    repo.commit()
    g = repo.graph
    target = g.node("pkg.core.safe_null_result", "pkg/core.py")
    g.edge(g.node("pkg.t1.test_one", "pkg/t1.py"), target)
    g.edge(g.node("pkg.t2.test_two", "pkg/t2.py"), target)
    for name in ("gateway", "server", "provider"):
        g.edge(g.module(f"pkg/{name}.py"), target, kind="USAGE", strategy=None, confidence=None)
    repo.fresh_index()

    env = repo.ask("callers", "safe_null_result")
    text = str(env.get("result") or "")
    assert re.search(r"2 direct calls?, 3 files? import it", text), text
    assert "call sites inside not resolved" in text, text
    assert "import-only-callers" in gap_kinds(env), gap_kinds(env)


# Spec: Under a narrowed target (`Gateway.query`), a USAGE row is dropped when its file neither
# mentions the qualifier nor imports the target's module.
def test_narrowed_target_drops_usage_rows_from_files_that_never_mention_or_import_it(
        tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch)
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/gateway.py",
               "class Gateway:\n    def query(self, op):\n        return op\n")
    repo.write("pkg/server.py",
               "from pkg.gateway import Gateway\n\n\n"
               "def handle(op):\n    return Gateway().query(op)\n")
    repo.write("pkg/searcher.py",
               "class Searcher:\n"
               "    def _embed_query(self, query):\n        return [query]\n\n"
               "    def search(self, query):\n        return self._embed_query(query)\n")
    repo.commit()
    g = repo.graph
    target = g.node("pkg.gateway.Gateway.query", "pkg/gateway.py", "Method")
    g.edge(g.node("pkg.server.handle", "pkg/server.py"), target,
           strategy="import_map", confidence="0.95")
    g.edge(g.node("pkg.searcher.Searcher._embed_query", "pkg/searcher.py", "Method"), target,
           kind="USAGE", strategy=None, confidence=None)
    g.edge(g.node("pkg.searcher.Searcher.search", "pkg/searcher.py", "Method"), target,
           kind="USAGE", strategy=None, confidence=None)
    repo.fresh_index()

    env = repo.ask("callers", "Gateway.query")
    text = str(env.get("result") or "")
    assert "pkg.server.handle" in text, text
    names = [str(r.get("qualified_name") or "") for r in env.get("rows") or []]
    assert not any("Searcher" in n for n in names), names
    assert "Searcher._embed_query" not in text and "Searcher.search" not in text, text


# Spec: The ambiguity banner reads "N symbols named `run` have caller edges; M more named `run`
# exist with none". It never prints a "Settle it …" qualifier for an unqualified target.
def test_ambiguity_banner_counts_symbols_with_and_without_caller_edges_and_never_says_settle_it(
        tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch)
    for mod in ("score", "canary", "serve"):
        repo.write(f"{mod}.py", "def run():\n    return 0\n")
    repo.write("main.py", "import score\nimport canary\n\n\n"
                          "def main():\n    score.run()\n    canary.run()\n\n\n"
                          "def guess(x):\n    return x.run()\n")
    repo.commit()
    g = repo.graph
    score = g.node("score.run", "score.py")
    canary = g.node("canary.run", "canary.py")
    g.node("serve.run", "serve.py")                       # named `run`, no caller edge
    main = g.node("main.main", "main.py")
    guess = g.node("main.guess", "main.py")
    g.edge(main, score, strategy="import_map", confidence="0.95")
    g.edge(main, canary, strategy="import_map", confidence="0.95")
    g.edge(guess, canary, strategy="suffix_match", confidence="0.55")
    repo.fresh_index()

    env = repo.ask("callers", "run")
    text = str(env.get("result") or "")
    assert "2 symbols named `run` have caller edges; 1 more named `run` exist with none" in text, text
    assert "Settle it" not in text, text
