"""Backend transport for the graph provider.

The second slice extracted from `GraphProvider` under docs/refactor-graph-provider.md: everything
that speaks the codebase-memory-mcp wire protocol and never raises — running a call, falling back
between the two subprocess forms the backend supports, naming WHY a call failed, and probing whether
the installed backend speaks a wire format this release can parse. `GraphProvider` composes a
`BackendClient` instance and exposes `available`/`_cmd`/`_saw_unparsable`/`_last_failure` as
properties over it, and keeps `_run`/`_run_stdin`/`_run_rawjson`/`_probe_wire_format` as thin
delegators to the matching `BackendClient` method — so the ~27 internal references to that state and
the tests that stub these methods on a provider instance keep working unchanged.

`_query_rows`/`_search_symbols` parse a raw response into rows via the module-level
`_parse_query_rows`/`_parse_search_results` below, kept separate from the `self._run(...)` call that
fetches it: `GraphProvider` still has its own `_query_rows`/`_search_symbols`, calling `self._run`
(its own overridable delegator) and this module's parser — not `self._backend._query_rows(...)`
wholesale — because tests stub the transport at `_run` alone and expect `callers`/`callees`/
`hotspots` (which read through `_query_rows`/`_search_symbols`) to honour that stub; routing the parse
through `self._backend`'s own `_run` would silently bypass it. Project resolution and op orchestration
stay on `GraphProvider`; this module owns transport only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Any

from codeintel.outcome import Missing


def _parse_query_rows(raw: Any) -> list[dict]:
    """Parse a `query_graph` response into rows as column→value dicts.

    The real backend returns ``{"columns": [...], "rows": [[v, ...], ...]}`` where each row is
    a value-array aligned to ``columns``. Tolerates the legacy/mocked list-of-dicts shape and
    any malformed response by returning ``[]`` (never raises)."""
    if isinstance(raw, list):
        # Legacy/mocked shape: already a list of dicts.
        return [r for r in raw if isinstance(r, dict)]
    if not isinstance(raw, dict):
        return []
    cols = raw.get("columns")
    rows = raw.get("rows")
    if not isinstance(cols, list) or not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if isinstance(row, list):
            out.append({str(cols[i]): row[i] for i in range(min(len(cols), len(row)))})
        elif isinstance(row, dict):
            out.append(row)
    return out


def _parse_search_results(raw: Any) -> list[dict] | None:
    """Parse a `search_graph` response into result dicts. ``None`` = backend failed/malformed
    (→ safe-null upstream); ``[]`` = backend answered but nothing matched (→ an informative empty
    render). Preserving that distinction is why the repo-scan ops return a string on empty-success
    but ``None`` on can't-answer. Never raises."""
    if raw is None:
        return None
    results = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(results, list):
        return None
    return [r for r in results if isinstance(r, dict)]


class BackendClient:
    """Speaks the codebase-memory-mcp CLI over a subprocess. Never raises."""

    # Class-level defaults so a `BackendClient.__new__(BackendClient)` instance — built directly by
    # tests that stub the transport, and reached via the `GraphProvider.__new__(GraphProvider)`
    # sites that skip `__init__` — starts from a known, safe state rather than raising
    # AttributeError.
    available: bool = False
    _cmd: str | None = None
    # Declared at class level, not only in __init__: several call sites (and the test helpers)
    # build a provider with `GraphProvider.__new__(GraphProvider)` to stub the subprocess seam,
    # which skips __init__ entirely. An instance-only attribute then raises AttributeError deep in
    # build_result, where the never-raise handler turns it into a generic "error" — a fault
    # injected by the fix itself.
    _saw_unparsable: bool = False
    # Why the most recent backend call failed, or None if none did. Set at `_run` — the one seam all
    # nine ops funnel through — so a single check downstream covers the whole op population instead
    # of each op having to remember. `_run` used to collapse four distinguishable states (binary
    # absent, non-zero exit, unparsable payload, timeout) into a bare `None`, `_query_rows` turned
    # that `None` into `[]`, the ops turned `[]` into `None`, and `_op_impact` turned `None` into
    # "(none found)" — B1's exact bytes, reproduced in the graph engine after it had been fixed in
    # the LSP engine and declared closed. Fixing it per-op is what produced that miss; this is the
    # population-level equivalent.
    _last_failure: Missing | None = None

    def __init__(self) -> None:
        # Set once the backend answers something that is not JSON — i.e. it speaks a dialect this
        # provider cannot read. Sticky for the provider's lifetime: the condition is a version
        # mismatch, not a transient, and it is the difference between "your symbol is not indexed"
        # and "your backend and this release do not agree on a wire format".
        self._saw_unparsable = False
        # Why the most recent backend call failed, or None if none did. Also declared at class
        # level (see the attribute below) for callers that bypass __init__ via __new__; set here
        # too so every entry point that DOES run __init__ starts from a known state rather than
        # the class-level default it happens to share.
        self._last_failure: Missing | None = None
        self._detect_backend()

    def _detect_backend(self) -> None:
        path = shutil.which("codebase-memory-mcp")
        if path:
            self.available = True
            self._cmd: str | None = path
        else:
            self.available = False
            self._cmd = None

    # Sentinel: distinguishes "the subprocess call failed" from "it succeeded and returned JSON
    # null". Overloading None for both would make a legit null result wrongly trigger the fallback.
    _FAIL = object()
    # Sentinel: the backend ran and exited 0, but did not speak JSON — a protocol/version
    # mismatch rather than a failure. Kept separate from _FAIL so it survives to the caller.
    _UNPARSABLE = object()

    def _run(self, method: str, payload: dict, timeout_ms: int) -> Any | None:
        # Prefer PIPED STDIN — the stable, non-deprecated form the backend documents
        # (`echo '<json>' | codebase-memory-mcp cli <method>`; no deprecation warning). Fall back
        # to the deprecated raw-JSON positional arg for one release so an older backend still
        # works. The two attempts SHARE one deadline (the caller's timeout_ms) so total wall time
        # can't double. Never raises. `_run` stays the single seam existing tests patch.
        body = json.dumps(payload)
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
        out = self._run_stdin(method, body, timeout_ms)
        if out is self._UNPARSABLE:
            # Retrying the deprecated positional form would only get the same dialect back.
            self._last_failure = Missing("unparsable", "the graph backend's reply could not be read")
            return None
        if out is not self._FAIL:
            return out  # success (including a legit null) → no fallback
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            self._last_failure = Missing(
                "timeout", "the graph backend did not respond within the time budget")
            return None
        out = self._run_rawjson(method, body, remaining_ms)
        if out is self._UNPARSABLE:
            self._last_failure = Missing("unparsable", "the graph backend's reply could not be read")
            return None
        if out is self._FAIL:
            self._last_failure = Missing("backend-error", "the graph backend did not answer")
            return None
        return out

    def _run_stdin(self, method: str, body: str, timeout_ms: int) -> Any:
        if self._cmd is None:                  # backend not on PATH — nothing to exec
            return self._FAIL
        try:
            result = subprocess.run(
                [self._cmd, "cli", method],
                input=body.encode(),
                capture_output=True,
                timeout=timeout_ms / 1000,
            )
            if result.returncode != 0:
                return self._FAIL  # unsupported / error → let the raw-JSON fallback try
            try:
                return json.loads(result.stdout)
            except ValueError:
                # The backend RAN and exited 0 — it simply did not answer in JSON. That is a
                # dialect mismatch, not a failure, and it must not be folded into the same `None`
                # as a crash: `codebase-memory-mcp` 0.10.x replaced the `{columns, rows}` payload
                # this provider parses with a compact human-readable text format, so every op that
                # is not `list_projects` (still JSON) silently returned "not in graph index" — on a
                # repository that was fully indexed. Distinguishing it is what lets the caller say
                # so instead of sending the user to re-index for the third time.
                self._saw_unparsable = True
                return self._UNPARSABLE
        except Exception:
            return self._FAIL

    def _run_rawjson(self, method: str, body: str, timeout_ms: int) -> Any:
        if self._cmd is None:
            return self._FAIL
        # Deprecated-but-working bridge for older backends; remove once the live stdin test
        # (tests/test_graph_stdin.py::test_live_stdin_list_projects) is green in CI.
        try:
            result = subprocess.run(
                [self._cmd, "cli", method, body],
                capture_output=True,
                timeout=timeout_ms / 1000,
            )
            if result.returncode != 0:
                return self._FAIL
            try:
                return json.loads(result.stdout)
            except ValueError:
                self._saw_unparsable = True
                return self._UNPARSABLE
        except Exception:
            return self._FAIL

    def _clear_failure(self) -> None:
        """Reset the per-query failure record.

        Cleared through a method rather than an inline ``self._last_failure = None`` for the same
        reason ``lsp.py`` clears its backend error through ``_clear_backend_error`` — a lesson that
        module learned and this one then repeated. ``_dispatch`` sets the attribute as a SIDE
        EFFECT, which a type checker cannot see, so an inline assignment narrows it to ``None`` for
        the rest of the function and makes both "did a backend call fail?" checks below read as
        unreachable code. The checks are the entire point of the attribute."""
        self._last_failure = None

    # Process-wide, because the answer is a property of the INSTALLED BACKEND, not of a provider
    # instance — and providers are constructed per call in several paths. Without this, every
    # `doctor`/`status` paid an extra `query_graph` round trip against a backend that takes
    # seconds per invocation, which turned a health check into a visible stall.
    _wire_format_ok: bool | None = None
    _wire_format_lock = threading.Lock()

    @classmethod
    def _reset_wire_format_cache(cls) -> None:
        """Forget the cached compatibility verdict.

        Process-wide caches need an explicit way back or they leak between callers — in tests, one
        real backend call would otherwise fix the verdict for every later case in the run. Also the
        hook to call if the backend is upgraded under a long-lived server."""
        with cls._wire_format_lock:
            cls._wire_format_ok = None

    def _probe_wire_format(self, project: str) -> bool | None:
        """Whether the backend answers a real QUERY in a shape this release can read.

        `list_projects` alone is not enough to judge compatibility — it is the one call that stayed
        JSON across the 0.9→0.10 change, so a probe based on it reports a perfectly healthy engine
        that cannot answer a single question. It must therefore be a genuine `query_graph`, and
        against a REAL project name: an empty or unknown project is rejected before the backend
        ever formats a response, so the reply says nothing about which dialect it speaks.
        ``None`` when the check could not run, so an unrelated hiccup is never called an
        incompatibility.
        """
        if not project:
            return None
        with BackendClient._wire_format_lock:
            if BackendClient._wire_format_ok is not None:
                return BackendClient._wire_format_ok
        self._saw_unparsable = False
        raw = self._run(
            "query_graph", {"project": project, "query": "MATCH (a) RETURN a.name LIMIT 1"}, 15000,
        )
        verdict = False if self._saw_unparsable else (None if raw is None else True)
        if verdict is not None:                 # don't cache "could not tell"
            with BackendClient._wire_format_lock:
                BackendClient._wire_format_ok = verdict
        return verdict

    @staticmethod
    def _any_project_name(raw: Any) -> str:
        """Any registered project name, to give the wire-format probe something real to ask about."""
        entries = raw.get("projects", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return ""
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name"):
                return str(entry["name"])
        return ""

    def _query_rows(self, cypher: str, project: str, timeout_ms: int) -> list[dict]:
        """Run a Cypher query and return rows as column→value dicts.

        The real backend returns ``{"columns": [...], "rows": [[v, ...], ...]}`` where each row is
        a value-array aligned to ``columns``. Tolerates the legacy/mocked list-of-dicts shape and
        any malformed response by returning ``[]`` (never raises)."""
        raw = self._run("query_graph", {"project": project, "query": cypher}, timeout_ms)
        return _parse_query_rows(raw)

    def _search_symbols(self, extra: dict, project: str, timeout_ms: int) -> list[dict] | None:
        """``search_graph`` → parsed result dicts. ``None`` = backend failed/malformed (→ safe-null
        upstream); ``[]`` = backend answered but nothing matched (→ an informative empty render).
        Preserving that distinction is why the repo-scan ops return a string on empty-success but
        ``None`` on can't-answer. Never raises."""
        raw = self._run("search_graph", {"project": project, **extra}, timeout_ms)
        return _parse_search_results(raw)
