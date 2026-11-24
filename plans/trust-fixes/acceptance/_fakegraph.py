"""An in-memory stand-in for the codebase-memory-mcp backend, faked at `GraphProvider._run`.

The backend is not installed in this container, so the acceptance tests talk to the graph engine
through its transport seam (`_run(method, payload, timeout_ms)`), the seam the spec's Risks section
names ("unit tests use the monkeypatched `_run` fake"). The fake understands enough of the backend's
methods to answer whatever a reasonable implementation asks:

* `list_projects` -> the one project registered at the test's root;
* `query_graph` -> a small Cypher subset evaluated over the edges and nodes held here:
  `MATCH (a)-[c:K1|K2]->(b) WHERE x.name="n" [AND x.qualified_name="q"] [AND x.file_path IN [..]]
  RETURN <exprs> [LIMIT n]` and `MATCH (n[:Label]) WHERE n.name="n" RETURN <exprs>`;
* `search_graph` -> nodes whose name matches `name_pattern`/`query`/`qn_pattern`;
* `trace_path` -> the BFS neighbourhood of `function_name` over CALLS edges, with `hop` numbers.

Rows are returned in the real 0.9 wire shape (`{"columns": [...], "rows": [[...]]}`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from codeintel.engines.graph.provider import GraphProvider

PROJECT = "tf-proj"


def slug(root: str) -> str:
    """The id codebase-memory-mcp gives a project, and the stem of its `<id>.db` cache file."""
    return re.sub(r"[^A-Za-z0-9]+", "-", os.path.realpath(root)).strip("-")


@dataclass
class Node:
    name: str
    qn: str           # qualified name WITHOUT the project prefix, e.g. "core.helper"
    file: str         # repo-relative path
    label: str = "Function"

    project: str = PROJECT

    @property
    def full_qn(self) -> str:
        return f"{self.project}.{self.qn}"


@dataclass
class Edge:
    src: Node
    dst: Node
    kind: str = "CALLS"
    strategy: str | None = "import_map"
    confidence: str | None = "0.95"
    callee_text: str | None = None


@dataclass
class FakeGraph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    trace_strategy: bool = False      # whether trace_path hops carry `strategy` (0.10.x) or not (0.9.x)
    project: str = PROJECT
    root: str = ""

    # ---------------------------------------------------------------- building

    def node(self, qn: str, file: str, label: str = "Function") -> Node:
        for n in self.nodes:
            if n.qn == qn and n.file == file:
                return n
        n = Node(qn.rsplit(".", 1)[-1], qn, file, label, self.project)
        self.nodes.append(n)
        return n

    def module(self, file: str) -> Node:
        """The module-scope pseudo-node the backend emits for code at module level."""
        qn = file[:-len(file.rsplit(".", 1)[-1]) - 1].replace("/", ".")
        for n in self.nodes:
            if n.label == "Module" and n.file == file:
                return n
        n = Node(file, qn, file, "Module", self.project)
        self.nodes.append(n)
        return n

    def edge(self, src: Node, dst: Node, kind: str = "CALLS", strategy: str | None = "import_map",
             confidence: str | None = "0.95", text: str | None = None) -> Edge:
        e = Edge(src, dst, kind, strategy, confidence, text)
        self.edges.append(e)
        return e

    # ---------------------------------------------------------------- wiring

    def install(self, monkeypatch, root: str | None = None) -> GraphProvider:
        """A GraphProvider whose backend transport is this graph."""
        root = root or self.root
        monkeypatch.setattr("codeintel.engines.graph.provider.shutil.which",
                            lambda x: "/fake/codebase-memory-mcp")
        p = GraphProvider()
        monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms=0:
                            self.run(root, method, payload))
        return p

    def run(self, root: str, method: str, payload: dict) -> Any:
        self.calls.append((method, dict(payload or {})))
        if method == "list_projects":
            return {"projects": [{"name": self.project, "root_path": os.path.realpath(root)}]}
        if method == "query_graph":
            return self._query(str(payload.get("query") or ""))
        if method == "search_graph":
            return self._search(payload)
        if method == "trace_path":
            return self._trace(payload)
        if method == "detect_changes":
            return self._detect_changes(root)
        if method == "search_code":
            return self._search_code(payload)
        if method == "get_architecture":
            return {"project": self.project, "total_nodes": len(self.nodes),
                    "total_edges": len(self.edges), "node_labels": [], "edge_types": [],
                    "languages": []}
        return None

    # ---------------------------------------------------------------- query_graph

    @staticmethod
    def _value(node: Node, attr: str) -> Any:
        return {"name": node.name, "qualified_name": node.full_qn, "file_path": node.file,
                "label": node.label}.get(attr)

    def _match_node(self, var: str, node: Node, where: str) -> bool:
        for m in re.finditer(rf'\b{var}\.(name|qualified_name|file_path)\s*=\s*"((?:[^"\\]|\\.)*)"',
                             where):
            if self._value(node, m.group(1)) != m.group(2):
                return False
        for m in re.finditer(rf"\b{var}\.(name|qualified_name|file_path)\s+IN\s+\[([^\]]*)\]", where):
            allowed = set(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(2)))
            if self._value(node, m.group(1)) not in allowed:
                return False
        for m in re.finditer(rf'\b{var}\.(name|qualified_name|file_path)\s*=~\s*"((?:[^"\\]|\\.)*)"',
                             where):
            try:
                if not re.fullmatch(m.group(2), str(self._value(node, m.group(1)) or "")):
                    return False
            except re.error:
                return False
        for m in re.finditer(
                rf"\b{var}\.(name|qualified_name|file_path)\s+(STARTS WITH|ENDS WITH|CONTAINS)\s+"
                rf'"((?:[^"\\]|\\.)*)"', where):
            v = str(self._value(node, m.group(1)) or "")
            op, lit = m.group(2), m.group(3)
            if op == "STARTS WITH" and not v.startswith(lit):
                return False
            if op == "ENDS WITH" and not v.endswith(lit):
                return False
            if op == "CONTAINS" and lit not in v:
                return False
        return True

    @staticmethod
    def _returns(cypher: str) -> list[tuple[str, str]]:
        m = re.search(r"\bRETURN\s+(?:DISTINCT\s+)?(.*?)(?:\s+ORDER BY.*?)?(?:\s+LIMIT\s+\d+)?\s*$",
                      cypher, re.S)
        if not m:
            return []
        out = []
        for part in [p.strip() for p in m.group(1).split(",")]:
            alias = re.match(r"(.*?)\s+AS\s+(\w+)$", part, re.I)
            out.append((alias.group(1).strip(), alias.group(2)) if alias else (part, part))
        return out

    def _eval(self, expr: str, env: dict[str, Any]) -> Any:
        lab = re.fullmatch(r"labels\((\w)\)", expr)
        if lab:
            node = env.get(lab.group(1))
            return json.dumps([node.label]) if isinstance(node, Node) else None
        if re.fullmatch(r"type\((\w)\)", expr):
            e = env.get("c")
            return e.kind if isinstance(e, Edge) else None
        attr = re.fullmatch(r"(\w)\.(\w+)", expr)
        if attr:
            obj = env.get(attr.group(1))
            if isinstance(obj, Node):
                return self._value(obj, attr.group(2))
            if isinstance(obj, Edge):
                return {"confidence": obj.confidence, "strategy": obj.strategy,
                        "callee": obj.callee_text if obj.callee_text is not None else obj.dst.name,
                        }.get(attr.group(2))
        return None

    def _query(self, cypher: str) -> dict:
        returns = self._returns(cypher)
        limit_m = re.search(r"LIMIT\s+(\d+)\s*$", cypher)
        limit = int(limit_m.group(1)) if limit_m else None
        where_m = re.search(r"\bWHERE\s+(.*?)\s+RETURN\b", cypher, re.S)
        where = where_m.group(1) if where_m else ""
        envs: list[dict[str, Any]] = []
        edge_m = re.search(r"MATCH\s*\((\w)(?::\w+)?\)\s*-\s*\[(\w)?(?::([\w|]+))?\]\s*->\s*\((\w)(?::\w+)?\)",
                           cypher)
        if edge_m:
            sv, cv, kinds, dv = edge_m.group(1), edge_m.group(2) or "c", edge_m.group(3), edge_m.group(4)
            allowed = set(kinds.split("|")) if kinds else None
            for e in self.edges:
                if allowed is not None and e.kind not in allowed:
                    continue
                if not self._match_node(sv, e.src, where) or not self._match_node(dv, e.dst, where):
                    continue
                envs.append({sv: e.src, dv: e.dst, cv: e})
        else:
            node_m = re.search(r"MATCH\s*\((\w)(?::(\w+))?\)", cypher)
            if node_m:
                v, label = node_m.group(1), node_m.group(2)
                for n in self.nodes:
                    if label and n.label != label:
                        continue
                    if self._match_node(v, n, where):
                        envs.append({v: n})
        aggregate = any("count(" in e.lower() for e, _ in returns)
        cols = [alias for _, alias in returns]
        rows: list[list[Any]] = []
        if aggregate:
            groups: dict[tuple, int] = {}
            plain = [(e, a) for e, a in returns if "count(" not in e.lower()]
            for env in envs:
                key = tuple(self._eval(e, env) for e, _ in plain)
                groups[key] = groups.get(key, 0) + 1
            for key, n in groups.items():
                vals = dict(zip([a for _, a in plain], key))
                rows.append([vals.get(a, str(n)) if "count(" not in e.lower() else str(n)
                             for e, a in returns])
        else:
            for env in envs:
                rows.append([self._eval(e, env) for e, _ in returns])
        if limit is not None:
            rows = rows[:limit]
        return {"columns": cols, "rows": rows, "total": len(rows)}

    # ---------------------------------------------------------------- detect_changes

    def _detect_changes(self, root: str) -> dict:
        """What the backend reports for uncommitted changes: the changed files per `git status`
        (nothing at all outside a git work tree), and the INDEXED symbols defined in them."""
        out = subprocess.run(["git", "-C", root, "status", "--porcelain", "--untracked-files=all"],
                             capture_output=True)
        files: list[str] = []
        if out.returncode == 0:
            for line in out.stdout.decode().splitlines():
                path = line[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                files.append(path)
        syms = [{"name": n.name, "qualified_name": n.full_qn, "file_path": n.file, "label": n.label}
                for n in self.nodes if n.file in files and n.label != "Module"]
        return {"changed_files": files, "changed_count": len(files), "impacted_symbols": syms}

    # ---------------------------------------------------------------- search_graph / trace_path

    def _search(self, payload: dict) -> dict:
        pat = str(payload.get("name_pattern") or payload.get("query") or payload.get("qn_pattern")
                  or payload.get("pattern") or "")
        try:
            rx = re.compile(pat)
        except re.error:
            rx = re.compile(re.escape(pat))
        hits = [n for n in self.nodes if n.label != "Module" and (rx.search(n.name) or rx.search(n.qn))]
        return {"results": [{"name": n.name, "node": n.name, "qualified_name": n.full_qn,
                             "label": n.label, "file": n.file, "file_path": n.file,
                             "start_line": 1, "end_line": 2} for n in hits],
                "total": len(hits)}

    def _search_code(self, payload: dict) -> dict:
        """Text search: every node whose name contains the pattern (case-insensitive)."""
        pat = str(payload.get("pattern") or payload.get("query") or "").lower()
        hits = [n for n in self.nodes if n.label != "Module" and pat and pat in n.name.lower()]
        return {"results": [{"node": n.name, "qualified_name": n.full_qn, "label": n.label,
                             "file": n.file, "start_line": 1, "end_line": 2,
                             "match_lines": [1]} for n in hits]}

    def _resolve(self, name: str) -> list[Node]:
        return [n for n in self.nodes if n.label != "Module"
                and (n.name == name or n.qn == name or n.full_qn == name
                     or n.qn.endswith("." + name))]

    def _trace(self, payload: dict) -> dict:
        fn = str(payload.get("function_name") or "")
        found = self._resolve(fn)
        if not found:
            return {"error": f"function not found: {fn}", "status": "not_found"}
        if len(found) > 1:
            return {"status": "ambiguous",
                    "suggestions": [{"name": n.name, "qualified_name": n.full_qn,
                                     "file_path": n.file} for n in found]}
        start = found[0]
        depth = int(payload.get("depth") or payload.get("max_depth") or 5)
        kinds = set(payload.get("edge_types") or ["CALLS"])
        direction = str(payload.get("direction") or "both")

        def walk(forward: bool) -> list[dict]:
            seen = {id(start)}
            out = []
            q = deque([(start, 0)])
            while q:
                cur, d = q.popleft()
                if d >= depth:
                    continue
                for e in self.edges:
                    if e.kind not in kinds:
                        continue
                    a, b = (e.src, e.dst) if forward else (e.dst, e.src)
                    if a is not cur or id(b) in seen:
                        continue
                    seen.add(id(b))
                    item = {"name": b.name, "qualified_name": b.full_qn, "file_path": b.file,
                            "hop": d + 1}
                    if self.trace_strategy and e.strategy:
                        item["strategy"] = e.strategy
                    out.append(item)
                    q.append((b, d + 1))
            return out

        return {"function": start.name,
                "direction": direction,
                "mode": "calls",
                "callees": walk(True) if direction in ("both", "outbound") else [],
                "callers": walk(False) if direction in ("both", "inbound") else []}


# ------------------------------------------------------------------------------------ a test repo

class Repo:
    """A repository on disk, its graph in memory, and the graph's `<id>.db` file whose mtime is
    what "when the index was last written" means to codeintel.

    `CODEBASE_MEMORY_HOME` is pointed at a private directory, so the db file the tests stamp is
    the one codeintel looks up for this root."""

    def __init__(self, tmp_path, monkeypatch, *, git: bool = True, name: str = "repo") -> None:
        self.root = str(tmp_path / name)
        os.makedirs(self.root, exist_ok=True)
        self.cache_dir = str(tmp_path / "cbm-cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        monkeypatch.setenv("CODEBASE_MEMORY_HOME", self.cache_dir)
        monkeypatch.setenv("CODEINTEL_REINDEX", "off")
        self.monkeypatch = monkeypatch
        self.graph = FakeGraph(project=slug(self.root), root=self.root)
        self.git = git
        if git:
            self.run_git("init", "-q")
            self.run_git("config", "user.email", "t@example.com")
            self.run_git("config", "user.name", "t")
            self.run_git("config", "commit.gpgsign", "false")

    # -- files

    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def write(self, rel: str, text: str, mtime: float | None = None) -> str:
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mtime is not None:
            os.utime(full, (mtime, mtime))
        return full

    def run_git(self, *args: str, env: dict | None = None) -> str:
        e = dict(os.environ)
        e.update(env or {})
        out = subprocess.run(["git", "-C", self.root, *args], check=True, capture_output=True,
                             env=e)
        return out.stdout.decode()

    def commit(self, message: str = "c", when: float | None = None) -> None:
        env = {}
        if when is not None:
            stamp = f"@{int(when)} +0000"
            env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        self.run_git("add", "-A")
        self.run_git("commit", "-q", "--no-verify", "-m", message, env=env)

    # -- the index

    @property
    def db_path(self) -> str:
        return os.path.join(self.cache_dir, f"{self.graph.project}.db")

    def stamp_index(self, when: float) -> None:
        """Pretend `codeintel index` wrote the graph db at *when* (epoch seconds)."""
        if not os.path.exists(self.db_path):
            with open(self.db_path, "wb") as fh:
                fh.write(b"SQLite format 3\x00")
        os.utime(self.db_path, (when, when))

    def age_all(self, when: float) -> None:
        """Set every file in the work tree (not .git) to mtime *when*."""
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for f in filenames:
                os.utime(os.path.join(dirpath, f), (when, when))

    def fresh_index(self) -> float:
        """Files a minute old, the index written after them: a fresh index. Returns its time."""
        now = time.time()
        self.age_all(now - 120)
        self.stamp_index(now - 60)
        return now - 60

    def provider(self) -> GraphProvider:
        return self.graph.install(self.monkeypatch, self.root)

    def ask(self, op: str, target: str, provider: GraphProvider | None = None) -> dict:
        p = provider or self.provider()
        return p.build_result(op, target, [], 30000, self.root)


def gap_kinds(env: dict) -> list[str]:
    return [str(g.get("kind")) for g in (env.get("gaps") or []) if isinstance(g, dict)]


def safe_for_destructive(env: dict) -> bool:
    """`safe_for_destructive` wherever the envelope states it (top level or under `evidence`)."""
    if "safe_for_destructive" in env:
        return bool(env["safe_for_destructive"])
    return bool((env.get("evidence") or {}).get("safe_for_destructive"))


def row_names(env: dict) -> list[str]:
    return [str(r.get("qualified_name") or r.get("name") or "") for r in (env.get("rows") or [])]
