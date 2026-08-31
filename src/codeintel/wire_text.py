"""Reader for codebase-memory-mcp 0.10.x's human-readable reply format.

0.9.x answered every CLI call in JSON. 0.10.x replaced that with a compact text layout for all but
`list_projects`, so a provider written against `{"columns": [...], "rows": [...]}` gets back
something it cannot read and — before this module — reported a fully indexed repository as "not in
the graph index". Passing `--json` does not undo it: that flag wraps the SAME text in an MCP
envelope (`{"content":[{"type":"text","text":"rows: 2 …"}]}`), so the structured rows are genuinely
gone rather than hidden behind a switch.

This module translates that text back into the 0.9.x-shaped dicts the rest of the provider already
parses, so exactly one layer knows two dialects exist and no op above the transport changes.

**The format is not a contract.** It is human-readable output, and it can change in a patch release
without anything announcing it. So every function here returns ``None`` rather than a half-filled
dict the moment the text stops matching what it expects, and the caller keeps its existing
"backend-incompatible" safe-null for that case. A wrong answer assembled from a format we no longer
understand is far worse than the honest refusal this replaces — that refusal is what made the
0.9→0.10 break diagnosable in the first place.

The grammar, pinned against real captures from 0.10.8:

    scalar          ``key: value``
    list section    ``key: N`` followed by two-space-indented bare lines
    row section     ``key: N  (cols: c1 c2 …)``  — or ``(rows: …)``; the count may be absent
    group line      ``<qualified-prefix> (<file path>):`` introducing the rows beneath it, where
                    each row's qualified name is ``prefix + "." + name`` (the header says so)
    row             two-space indent, values separated by spaces, Go ``%q``-quoted when the value
                    contains a space or a quote, bare otherwise
    null            a single ``-``

The `-` for null is the one genuine ambiguity: a value that is *literally* a hyphen is
indistinguishable from an absent one. Nothing in the graph's own vocabulary (identifiers, paths,
labels, line ranges) is a bare hyphen, so this is accepted rather than worked around, and recorded
here so the next reader does not have to rediscover it.
"""
from __future__ import annotations

import re
from typing import Any

# `key: N  (cols: a b c)` / `(rows: a b c; qn = group prefix + "." + name)`. The count is optional
# (`impacted_modules:` ships without one). Greedy to the LAST `)` on the line, because column names
# are Cypher expressions carrying their own parens — `labels(a)`, `type(c)` — and stopping at the
# first one truncated the name and silently dropped every column after it. Anything past the first
# `;` inside the parens is prose for a human and is discarded where the group is read.
_SECTION_RE = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<count>\d+)?\s*\((?:cols|rows):\s*(?P<cols>.*)\)\s*$")
_SCALAR_RE = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<value>.*)$")
# `<qualified.name> (<path>):` — a group header. The path may contain spaces, so anchor on the
# trailing `):` rather than splitting on whitespace.
_GROUP_RE = re.compile(r"^(?P<prefix>\S+)\s+\((?P<file>.+)\):$")
_INDENT = "  "

_NULL = "-"


def _unquote(tok: str) -> str:
    """A Go ``%q`` literal back to its value; a bare token unchanged."""
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        body, out, i = tok[1:-1], [], 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    return tok


def split_row(line: str) -> list[str]:
    """Split one row into its values, honouring ``%q`` quoting.

    Not `shlex`: that treats a single quote as an opener, and these payloads carry apostrophes
    inside quoted prose (`"Prompts — all six are done"` is fine, but a docstring with `don't` is
    not). Only the double-quote form the backend actually emits opens a token here.
    """
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i] == " ":
            i += 1
        if i >= n:
            break
        if line[i] == '"':
            j = i + 1
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == '"':
                    break
                j += 1
            out.append(_unquote(line[i:min(j + 1, n)]))
            i = j + 1
        else:
            j = line.index(" ", i) if " " in line[i:] else n
            out.append(_unquote(line[i:j]))
            i = j
    return out


def _cell(value: str) -> Any:
    """One parsed value, with the backend's null marker mapped to ``None``."""
    return None if value == _NULL else value


def is_text_dialect(text: str) -> bool:
    """Whether *text* looks like a 0.10.x reply at all.

    Deliberately weak — it only has to separate "the backend answered in the newer layout" from
    "the backend produced something else entirely" (an error page, a usage banner, an empty
    stream). Deciding whether the specific reply is USABLE is each parser's job below, and that
    decision is the one allowed to fail.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return bool(_SCALAR_RE.match(line) or _SECTION_RE.match(line))
    return False


class _Doc:
    """One parsed reply: its top-level scalars, its list sections and its row sections."""

    def __init__(self, text: str) -> None:
        self.scalars: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.rows: dict[str, list[dict]] = {}
        self._parse(text.splitlines())

    def _parse(self, lines: list[str]) -> None:
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            sec = _SECTION_RE.match(line)
            if sec:
                cols = sec.group("cols").split(";")[0].split()
                i = self._read_rows(lines, i + 1, sec.group("key"), cols)
                continue
            scalar = _SCALAR_RE.match(line)
            if scalar and not line.startswith(_INDENT):
                key, value = scalar.group("key"), scalar.group("value").strip()
                # `changed_files: 2` followed by indented bare lines is a LIST, not a scalar. The
                # count alone is never what a caller wants, so the payload wins and the count is
                # dropped — it is recoverable from the list, and keeping both invites them to
                # disagree.
                if i + 1 < n and lines[i + 1].startswith(_INDENT) and value.isdigit():
                    i = self._read_list(lines, i + 1, key)
                    continue
                self.scalars[key] = _unquote(value)
            i += 1

    def _read_list(self, lines: list[str], i: int, key: str) -> int:
        out: list[str] = []
        while i < len(lines) and lines[i].startswith(_INDENT):
            out.append(_unquote(lines[i].strip()))
            i += 1
        self.lists[key] = out
        return i

    def _read_rows(self, lines: list[str], i: int, key: str, cols: list[str]) -> int:
        """Rows under one section header, flattening any group lines into per-row fields.

        A group line carries the qualified-name prefix and the file path for every row beneath it —
        the header states the rule (`qn = group prefix + "." + name`) — so each row is given a
        `_qn` and `_file` here rather than leaving every adapter below to re-derive it and one of
        them to forget.
        """
        out: list[dict] = []
        prefix = file = ""
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            if not line.startswith(_INDENT):
                group = _GROUP_RE.match(line.strip())
                if group:
                    prefix, file = group.group("prefix"), group.group("file")
                    i += 1
                    continue
                break                                   # a new top-level key ends this section
            values = split_row(line.strip())
            # NOT strict: a row with fewer values than columns is a format drift, and raising
            # here would escape into a transport whose whole contract is that it never does. The
            # short row simply yields fewer keys, and the adapter's required-key check decides.
            row: dict[str, Any] = {c: _cell(v)
                                   for c, v in zip(cols, values, strict=False)}
            name = row.get(cols[0]) if cols else None
            row["_qn"] = f"{prefix}.{name}" if prefix and name else (name or "")
            row["_file"] = file
            out.append(row)
            i += 1
        self.rows[key] = out
        return i


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _span(value: Any) -> tuple[int, int]:
    """`"60-111"` → `(60, 111)`; a bare number → `(n, n)`; anything else → `(0, 0)`."""
    text = str(value or "")
    if "-" in text:
        head, _, tail = text.partition("-")
        return _int(head), _int(tail)
    return _int(text), _int(text)


def _query_graph(doc: _Doc, text: str) -> dict | None:
    """`rows: N  (cols: …)` → the 0.9.x `{"columns": [...], "rows": [[…]]}` payload."""
    rows = doc.rows.get("rows")
    if rows is None:
        return None
    cols = [c for c in (rows[0].keys() if rows else []) if not c.startswith("_")]
    if not cols:
        # A zero-row answer still carries its column list in the header, and an empty result is a
        # real answer — returning None here would turn "nothing matched" back into "backend
        # unreadable", which is the whole confusion this module exists to end.
        header = next((line for line in text.splitlines() if _SECTION_RE.match(line)), "")
        match = _SECTION_RE.match(header)
        cols = match.group("cols").split(";")[0].split() if match else []
    return {"columns": cols,
            "rows": [[r.get(c) for c in cols] for r in rows],
            "total": _int(doc.scalars.get("total"), len(rows))}


def _search_graph(doc: _Doc) -> dict | None:
    """`results: N  (rows: name label lines in out)` → `{"results": [ … ]}`."""
    rows = doc.rows.get("results")
    if rows is None:
        return None
    out = []
    for r in rows:
        start, end = _span(r.get("lines"))
        out.append({
            "name": r.get("name"), "qualified_name": r.get("_qn"),
            "label": r.get("label"), "file_path": r.get("_file"),
            "in_degree": _int(r.get("in")), "out_degree": _int(r.get("out")),
            # `complexity`/`cognitive`/`is_test` are NOT core columns in 0.10.x — they arrive only
            # when the caller asks for them through `fields`. `hotspots` does (it ranks on
            # complexity, and `is_test` is how it drops spec files), so they are read by their real
            # names here and default to 0/False when a caller did not request them.
            "complexity": _int(r.get("complexity")), "cognitive": _int(r.get("cognitive")),
            "is_test": r.get("is_test") == "true",
            "lines": max(0, end - start + 1) if end >= start else 0,
        })
    return {"total": _int(doc.scalars.get("total"), len(out)), "results": out,
            "has_more": doc.scalars.get("has_more") == "true"}


def _search_code(doc: _Doc) -> dict | None:
    """`results: N  (cols: qn label file lines matches in out)` → `{"results": [ … ]}`."""
    rows = doc.rows.get("results")
    if rows is None:
        return None
    out = []
    for r in rows:
        start, end = _span(r.get("lines"))
        qn = r.get("qn") or r.get("_qn") or ""
        raw = str(r.get("matches") or "")
        out.append({
            "node": qn.rsplit(".", 1)[-1], "qualified_name": qn,
            "label": r.get("label"), "file": r.get("file"),
            "start_line": start, "end_line": end,
            "match_lines": [_int(p) for p in raw.split(";") if p.strip()],
            "in_degree": _int(r.get("in")), "out_degree": _int(r.get("out")),
        })
    return {"results": out, "total_results": _int(doc.scalars.get("total_results"), len(out))}


def _trace_path(doc: _Doc) -> dict | None:
    """`callees:`/`callers:` sections → `{"function", "callees": [...], "callers": [...]}`."""
    if "callees" not in doc.rows and "callers" not in doc.rows:
        return None

    def hops(key: str) -> list[dict]:
        out = []
        for r in doc.rows.get(key, []):
            qn = r.get("qn") or r.get("_qn") or ""
            out.append({"name": qn.rsplit(".", 1)[-1], "qualified_name": qn,
                        "hop": _int(r.get("hop")), "risk": r.get("risk")})
        return out

    return {"function": doc.scalars.get("function", ""),
            "direction": doc.scalars.get("direction", ""),
            "mode": doc.scalars.get("mode", ""),
            "callees": hops("callees"), "callers": hops("callers")}


def _get_architecture(doc: _Doc) -> dict | None:
    """Scalars plus the `node_labels` / `edge_types` / `languages` sections."""
    if "total_nodes" not in doc.scalars and "node_labels" not in doc.rows:
        return None
    def pairs(key: str, name_key: str, count_key: str, out_name: str, out_count: str) -> list[dict]:
        return [{out_name: r.get(name_key), out_count: _int(r.get(count_key))}
                for r in doc.rows.get(key, [])]
    return {
        "project": doc.scalars.get("project", ""),
        "total_nodes": _int(doc.scalars.get("total_nodes")),
        "total_edges": _int(doc.scalars.get("total_edges")),
        "node_labels": pairs("node_labels", "label", "count", "label", "count"),
        "edge_types": pairs("edge_types", "type", "count", "type", "count"),
        "languages": pairs("languages", "language", "files", "language", "file_count"),
    }


def _detect_changes(doc: _Doc) -> dict | None:
    """`changed_files` list plus the grouped `impacted` rows.

    0.10.x's `impacted` is already a transitive walk (it carries a `hop`), where 0.9.x returned only
    the symbols defined in the changed files. Both are handed back under the key the provider
    already reads; the extra reach is a straight improvement and needs no translation.
    """
    if "changed_files" not in doc.lists and "changed_files" not in doc.scalars:
        return None
    impacted = [
        {"name": r.get("name"), "qualified_name": r.get("_qn"), "file_path": r.get("_file"),
         "hop": _int(r.get("hop"))}
        for r in doc.rows.get("impacted", [])
    ]
    files = doc.lists.get("changed_files", [])
    return {"changed_files": files, "changed_count": len(files),
            "impacted_symbols": impacted, "depth": _int(doc.scalars.get("depth"), 2)}


_PARSERS = {
    "query_graph": lambda doc, text: _query_graph(doc, text),
    "search_graph": lambda doc, text: _search_graph(doc),
    "search_code": lambda doc, text: _search_code(doc),
    "trace_path": lambda doc, text: _trace_path(doc),
    "get_architecture": lambda doc, text: _get_architecture(doc),
    "detect_changes": lambda doc, text: _detect_changes(doc),
}


def parse(method: str, text: str) -> dict | None:
    """A 0.10.x text reply as the 0.9.x-shaped dict *method*'s caller expects.

    ``None`` for anything this module cannot vouch for — an unknown method, a reply that is not in
    the text layout, a shape that no longer matches, or any exception at all. The caller treats
    that exactly as it treated an unreadable reply before this module existed, which keeps the
    honest "your backend and this release do not agree" answer available as the floor.
    """
    try:
        handler = _PARSERS.get(method)
        if handler is None or not text or not is_text_dialect(text):
            return None
        return handler(_Doc(text), text)
    except Exception:
        return None
