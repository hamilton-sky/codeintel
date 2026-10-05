"""Graphify as two more arms — the same oracle, the same targets, the same scorer, a different engine.

Off unless `CODEINTEL_BENCH_GRAPHIFY` names a `graphify` executable (the `graphifyy` package on PyPI).
With it set, `bench/run.py <repo>` adds two rows to its table:

  graphify            every `calls` edge Graphify records into the target
  graphify_extracted  only the edges Graphify labels EXTRACTED — its own "certain" label, and so the
                      counterpart of `graph_verified`: the arm that reads the engine's trust signal
                      the way an agent would

Two properties are what make the comparison fair, and both are enforced here rather than hoped for:

* **Graphify reads a COPY of the tree.** It writes `graphify-out/` into whatever it is pointed at, and
  a benchmark that edits the repository it measures has changed the thing it measured.
* **Graphify cannot reach a model.** `graphify update --no-cluster` is its documented code-only,
  no-LLM path; it is also run with no API keys in the environment, a scratch `HOME`, and a `PATH`
  holding only system directories and Graphify's OWN install — `claude` and `ollama`, two of the
  backends it auto-detects, are therefore not findable. A private repository measured here never
  leaves the machine, and a code edge never comes from a model.

This module only builds and reads the graph. Turning an edge into an oracle key happens in
`score.py`, next to the keys of every other arm, so that no arm is scored by a rule another escapes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

ENV = "CODEINTEL_BENCH_GRAPHIFY"
# Trees a source graph must not include: VCS state, dependencies, build output, caches, and any
# earlier Graphify output. Copying `node_modules` would also make a TypeScript build take minutes.
_EXCLUDE = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "graphify-out", "dist",
                      "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".serena"})
_BUILD_TIMEOUT_S = 1800
# The relations that answer "who uses this". `calls` is a call. `indirect_call` is Graphify's edge for
# a function passed or stored as a value — a callback, a dispatch table — and `references` any other
# use: both are change impact, not calls, which is how the oracle labels such a site, and Graphify's
# own blast-radius walk (`graphify affected`) counts both. `imports` / `re_exports` are left out on
# purpose: the oracle does not count an import as a caller or as change impact, nor do other arms.
_USE_RELATIONS = ("calls", "indirect_call", "references")


def executable() -> str | None:
    """The `graphify` the arm should run, or None when the arm is off."""
    path = os.environ.get(ENV, "").strip()
    return path if path and os.path.isfile(path) and os.access(path, os.X_OK) else None


def sealed_env(exe: str, home: str) -> dict[str, str]:
    """The whole environment Graphify runs in — nothing inherited.

    The executable's directory is resolved through symlinks first: a `uv tool install` puts a shim in
    `~/.local/bin`, which is exactly where a `claude` binary lives too, and putting THAT directory on
    `PATH` would hand Graphify a model backend it can auto-detect. The real directory holds only
    Graphify's own environment."""
    own = os.path.dirname(os.path.realpath(exe))
    return {"HOME": home, "PATH": f"/usr/bin:/bin:{own}", "LANG": "en_US.UTF-8"}


def version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30,
                             env=sealed_env(exe, tempfile.gettempdir()))
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as exc:                                   # provenance, never a crash
        return f"unknown ({type(exc).__name__})"


@dataclass
class Edge:
    relation: str
    confidence: str
    source_file: str
    line: int | None


@dataclass
class GraphifyIndex:
    """One built graph, and the two lookups the arm needs from it."""

    nodes: dict[str, dict]
    edges: list[dict]
    summary: str = ""
    methods: dict[str, list[str]] = field(default_factory=dict)   # class node id -> method node ids

    @classmethod
    def from_graph(cls, graph: dict, summary: str = "") -> GraphifyIndex:
        nodes = {n["id"]: n for n in graph.get("nodes") or []}
        gi = cls(nodes, list(graph.get("links") or graph.get("edges") or []), summary)
        # A method is its own node, joined to its class by a `method` edge and labelled `.name()`.
        # The edge's direction is not documented, so either end may be the class.
        for e in gi.edges:
            if e.get("relation") != "method":
                continue
            a, b = e.get("source"), e.get("target")
            if str(nodes.get(b, {}).get("label", "")).startswith("."):
                gi.methods.setdefault(a, []).append(b)
            elif str(nodes.get(a, {}).get("label", "")).startswith("."):
                gi.methods.setdefault(b, []).append(a)
        return gi

    def find(self, def_file: str, symbol: str) -> list[str]:
        """The node ids that denote *symbol* defined in *def_file*; empty when Graphify has none.

        `Cls.method` is the method node hanging off class `Cls` in that file. A bare name is a
        function node (`name()`) or a non-callable one (`name`) in that file — never a method, whose
        label starts with `.`."""
        cls_name, _, leaf = symbol.rpartition(".")
        if cls_name:
            short = cls_name.rsplit(".", 1)[-1]
            return [m for nid, n in self.nodes.items()
                    if n.get("source_file") == def_file and n.get("label") == short
                    for m in self.methods.get(nid, []) if self.nodes[m].get("label") == f".{leaf}()"]
        return [nid for nid, n in self.nodes.items()
                if n.get("source_file") == def_file and n.get("label") in (f"{leaf}()", leaf)
                and not str(n.get("label", "")).startswith(".")]

    def uses_of(self, ids: list[str]) -> list[Edge]:
        """Every `calls` / `references` edge into one of *ids*, with its call-site line."""
        wanted = set(ids)
        out = []
        for e in self.edges:
            if e.get("target") not in wanted or e.get("relation") not in _USE_RELATIONS:
                continue
            m = re.match(r"L(\d+)", str(e.get("source_location") or ""))
            out.append(Edge(str(e["relation"]), str(e.get("confidence") or ""),
                            str(e.get("source_file") or ""), int(m.group(1)) if m else None))
        return out


def build(root: str, exe: str) -> GraphifyIndex:
    """Build Graphify's code graph for *root* from a throwaway copy, and read it back.

    Raises on failure — the caller turns that into "arm skipped", never into an empty answer."""
    with tempfile.TemporaryDirectory(prefix="codeintel-bench-graphify-") as tmp:
        copy = os.path.join(tmp, "tree")
        shutil.copytree(root, copy, symlinks=True,
                        ignore=lambda _d, names: [n for n in names if n in _EXCLUDE])
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        proc = subprocess.run([exe, "update", copy, "--no-cluster"], env=sealed_env(exe, home),
                              capture_output=True, text=True, timeout=_BUILD_TIMEOUT_S)
        graph_json = os.path.join(copy, "graphify-out", "graph.json")
        tail = (proc.stdout + proc.stderr).strip()[-300:]
        # A graph a FAILED process left behind may be partial: scoring it would charge the engine for
        # a half-built index. A clean build exits 0, so anything else is a failed build, graph or not.
        if proc.returncode != 0:
            raise RuntimeError(f"graphify exited {proc.returncode}: {tail}")
        if not os.path.exists(graph_json):
            raise RuntimeError(f"graphify exited 0 but wrote no graph: {tail}")
        with open(graph_json, encoding="utf-8") as fh:
            graph = json.load(fh)
        summary = next((ln.strip() for ln in proc.stdout.splitlines() if "Rebuilt" in ln), "")
        return GraphifyIndex.from_graph(graph, summary)
