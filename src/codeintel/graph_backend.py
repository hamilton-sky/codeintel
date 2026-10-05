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

**Why a call failed is part of the transport's job.** A failed launch used to collapse to a bare
sentinel, and `_run` then made things worse in two ways: it discarded the exit code and the stderr
that had been captured for exactly this purpose, and it launched the same failing command a second
time through the raw-JSON form before recording a generic "did not answer". A backend that printed
`CBM CLI could not start because a pre-coordination or unverified CBM generation is active` and
exited 1 inside a second was therefore reported as "failed/timed out" by the doctor, with the one
sentence that explained it already in hand. Each attempt now returns a `_Failed` that carries the
cause when the transport can tell, and `_run` keeps the FIRST specific one: four causes are told
apart — not installed, refused (a non-zero exit inside the time budget, with the backend's own
message), timed out, and unreadable — and see `docs/doctor.md` for how each is reported.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

from codeintel import wire_text
from codeintel.outcome import Missing
from codeintel.per_thread import PerThread
from codeintel.redact import redact_text


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


# --------------------------------------------------------------------------------------------- #
# Why a launch failed
# --------------------------------------------------------------------------------------------- #

# The backend's own log lines (`level=info msg=version_cohort.claimed_unheld build=...`) share
# stderr with its error messages and say nothing about what went wrong. Only the quiet levels are
# dropped: a `level=warn` or `level=error` line may well BE the reason, and a filter that guessed
# wrong in the other direction would hide it.
_LOG_NOISE = re.compile(r"^\s*(?:\w+=\S+\s+)*level=(?:info|debug)\b", re.IGNORECASE)
# The ceiling on what a report carries of the backend's stderr. A message is a sentence or two; a
# crash can print a page, and a doctor line the size of a page is not read.
_STDERR_CAP = 400


def _stderr_tail(raw: Any) -> str:
    """The backend's stderr as one short line a person can read, with the home directory removed.

    Log noise dropped, whitespace collapsed, then the TAIL kept — the refusal is the last thing a
    process says before it exits, and a leading cut would keep the startup chatter and lose the
    reason. Redacted HERE, at capture, rather than trusted to the envelope boundary: this text is
    stored on a `Missing` that reaches the CLI's own doctor output, which does not go through
    `redact`, and a backend that fails to open a file under `~/.cache` names the path in full."""
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    kept = [line for line in text.splitlines() if not _LOG_NOISE.match(line)]
    flat = redact_text(" ".join(" ".join(kept).split()))
    if len(flat) > _STDERR_CAP:
        flat = "…" + flat[-(_STDERR_CAP - 1):]
    return flat


# Two refusals, deliberately separate. Both mean a second launch cannot help; only the first has a
# known fix. "Could not start" is the backend declining to run at all — as opposed to a usage error
# (an older backend rejecting the piped-stdin form), which is exactly what the raw-JSON fallback
# exists for and must keep falling back. "Coordination" is the specific state observed on
# 2026-10-04: a pre-coordination or unverified generation of the backend was still alive, and every
# CLI call refused to start until its lock files were cleared.
_COULD_NOT_START = re.compile(r"could not start", re.IGNORECASE)
_COORDINATION = re.compile(r"pre-coordination|CBM generation", re.IGNORECASE)


def is_coordination_refusal(message: str) -> bool:
    """Whether *message* is the backend declining to run because another generation of it is live."""
    return bool(_COORDINATION.search(message or ""))


def is_launch_refusal(message: str) -> bool:
    """Whether *message* says the backend will not run AT ALL, so launching it again is pointless."""
    return bool(_COULD_NOT_START.search(message or "")) or is_coordination_refusal(message)


def coordination_remediation() -> str:
    """The recovery for a coordination refusal — the procedure that was verified to work.

    Stated in terms of the platform's temp directory rather than a path: the observed location was
    `/private/tmp/cbm-daemon-501` on macOS, which is a macOS fact, and the backend does not use the
    per-user `$TMPDIR` there, so a name is built from BOTH candidates and the directory that really
    exists is quoted when one does. The lock files outlive the processes, which is why closing the
    sessions alone does not clear it."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    name = f"cbm-daemon-{uid if uid is not None else '<uid>'}"
    bases: list[str] = []
    # `/tmp` is named because the observed backend does NOT follow `$TMPDIR` (macOS's per-user
    # temp dir is under /var/folders, and the daemon directory was in /private/tmp), so asking only
    # `gettempdir()` would name a directory that is not there.
    for base in (tempfile.gettempdir(),
                 os.path.realpath("/tmp") if os.name == "posix" else ""):  # noqa: S108
        if base and base not in bases:
            bases.append(base)
    found = [os.path.join(b, name) for b in bases if os.path.isdir(os.path.join(b, name))]
    where = (f"found at {', '.join(found)}" if found
             else f"look under {' or '.join(bases)}")
    return redact_text(
        "close every codebase-memory-mcp process (this also closes the graph tool in any agent "
        f"session that owns one), then move the `{name}` directory under the system temp dir "
        f"aside ({where}) — its lock files outlive the processes — then retry"
    )


@dataclass(frozen=True)
class BackendRefused(Missing):
    """The backend ran and exited non-zero inside the time budget — with its own words attached.

    A `Missing` in every respect the callers above this module care about (`kind` is still
    `backend-error`, so envelopes keep their existing `reason` vocabulary); the extra fields are for
    the one reader that has to tell a refusal from a crash from a timeout, which is the doctor."""

    returncode: int = 0
    # Cleaned, redacted and bounded — see `_stderr_tail`. Empty when the backend exited silently.
    message: str = ""


def _refused(returncode: int, message: str) -> BackendRefused:
    detail = (f"the graph backend refused to run (exit {returncode}): {message}" if message
              else f"the graph backend exited {returncode} without saying why")
    return BackendRefused("backend-error", detail, None, returncode, message)


_NOT_INSTALLED = "codebase-memory-mcp is not installed, or is no longer on PATH"
_TIMED_OUT = "the graph backend did not respond within the time budget"


class _Failed:
    """One launch that produced no reply — and why, when the transport could tell.

    `BackendClient._FAIL` is the bare instance (no cause known), which is what a test stub returns
    and what an unclassified exception becomes. `retryable` is False when launching the same
    binary again cannot differ: the budget is spent, the binary is gone, or the backend has said it
    will not run at all."""

    __slots__ = ("retryable", "why")

    def __init__(self, why: Missing | None = None, *, retryable: bool = True) -> None:
        self.why = why
        self.retryable = retryable


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
    # Per thread: the provider is shared by concurrent `serve-http` requests, and one request's
    # failure is not another's — see `codeintel/per_thread.py`.
    _last_failure: PerThread[Missing | None] = PerThread(None)

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
        self._last_failure = None
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
    # An INSTANCE of `_Failed` with no cause, not a bare `object()`: a transport that knows why it
    # failed returns its own `_Failed(why)`, and every check is `isinstance(out, _Failed)`, so this
    # one — what a stubbed transport returns, and what an unclassified exception becomes — still
    # reads as "failed, reason unknown".
    _FAIL = _Failed()
    # Sentinel: the backend ran and exited 0, but did not speak JSON — a protocol/version
    # mismatch rather than a failure. Kept separate from _FAIL so it survives to the caller.
    _UNPARSABLE = object()

    def _run(self, method: str, payload: dict, timeout_ms: int) -> Any | None:
        # Prefer PIPED STDIN — the stable, non-deprecated form the backend documents
        # (`echo '<json>' | codebase-memory-mcp cli <method>`; no deprecation warning). Fall back
        # to the deprecated raw-JSON positional arg for one release so an older backend still
        # works. The two attempts SHARE one deadline (the caller's timeout_ms) so total wall time
        # can't double. Never raises. `_run` stays the single seam existing tests patch.
        #
        # The fallback exists for a backend that rejects the stdin FORM, so it runs only when the
        # first attempt could have failed for that reason. A budget already spent, a binary that is
        # gone, and a backend that says it will not run at all (`is_launch_refusal`) each end the
        # call: a second launch of the same command gets the same answer and costs a full process
        # start against a backend measured at seconds per launch. Before this it ALSO overwrote
        # what the first attempt had learned with a generic "did not answer" — the refusal was
        # captured and then lost in the retry.
        body = json.dumps(payload)
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
        out = self._run_stdin(method, body, timeout_ms)
        if out is self._UNPARSABLE:
            # Retrying the deprecated positional form would only get the same dialect back.
            self._last_failure = Missing("unparsable", "the graph backend's reply could not be read")
            return None
        if not isinstance(out, _Failed):
            return out  # success (including a legit null) → no fallback
        first = out.why  # the FIRST specific failure — the retry below may not replace it
        if not out.retryable:
            self._last_failure = first or Missing("backend-error", "the graph backend did not answer")
            return None
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            self._last_failure = first or Missing("timeout", _TIMED_OUT)
            return None
        out = self._run_rawjson(method, body, remaining_ms)
        if out is self._UNPARSABLE:
            self._last_failure = Missing("unparsable", "the graph backend's reply could not be read")
            return None
        if isinstance(out, _Failed):
            self._last_failure = (
                first or out.why or Missing("backend-error", "the graph backend did not answer"))
            return None
        return out

    def _run_stdin(self, method: str, body: str, timeout_ms: int) -> Any:
        if self._cmd is None:                  # backend not on PATH — nothing to exec
            return _Failed(Missing("backend-error", _NOT_INSTALLED), retryable=False)
        return self._launch([self._cmd, "cli", method], method, timeout_ms, stdin=body.encode())

    def _run_rawjson(self, method: str, body: str, timeout_ms: int) -> Any:
        if self._cmd is None:
            return _Failed(Missing("backend-error", _NOT_INSTALLED), retryable=False)
        # Deprecated-but-working bridge for older backends; remove once the live stdin test
        # (tests/test_graph_stdin.py::test_live_stdin_list_projects) is green in CI.
        return self._launch([self._cmd, "cli", method, body], method, timeout_ms)

    def _launch(self, argv: list[str], method: str, timeout_ms: int,
                stdin: bytes | None = None) -> Any:
        """One subprocess call, classified. Never raises.

        Returns the decoded reply, `_UNPARSABLE`, or a `_Failed` carrying the cause. The causes are
        told apart by what the process DID, not by how long it took: a non-zero exit is a refusal
        whatever the clock says, and only an expired `subprocess.run` timeout is a timeout. The old
        code inferred "timed out" from the shared deadline having passed, which is how a backend
        that exited in a second came to be reported as one that never answered."""
        kwargs: dict[str, Any] = {} if stdin is None else {"input": stdin}
        try:
            result = subprocess.run(
                argv, capture_output=True, timeout=timeout_ms / 1000, **kwargs)
        except subprocess.TimeoutExpired:
            return _Failed(Missing("timeout", _TIMED_OUT), retryable=False)
        except FileNotFoundError:
            return _Failed(Missing("backend-error", _NOT_INSTALLED), retryable=False)
        except OSError as exc:
            # Present but not launchable (no execute bit, a corrupt binary): the same file will not
            # launch the second time either.
            reason = redact_text(exc.strerror or type(exc).__name__)
            return _Failed(
                Missing("backend-error", f"the graph backend could not be launched: {reason}"),
                retryable=False)
        except Exception:
            return self._FAIL
        try:
            if result.returncode != 0:
                # `getattr`: the doubles tests substitute for `subprocess.run` carry a return code
                # and stdout and nothing else.
                message = _stderr_tail(getattr(result, "stderr", None))
                return _Failed(_refused(result.returncode, message),
                               retryable=not is_launch_refusal(message))
            return self._decode(method, result.stdout)
        except Exception:
            return self._FAIL

    def _decode(self, method: str, stdout: bytes) -> Any:
        """One backend reply, in whichever dialect the installed backend speaks.

        Two are supported. 0.9.x answers in JSON. 0.10.x answers all but `list_projects` in a
        compact human-readable text layout — passing `--json` does not undo that, it only wraps the
        same text in an MCP envelope — and `wire_text` translates it back into the 0.9.x-shaped
        dict every caller above this line already parses. Neither the ops nor the renderers learn
        that a second dialect exists.

        The ordering matters: JSON is tried first, so a 0.9.x backend never pays for the text path
        and never risks a misparse. And the text path must still be able to FAIL — a reply that is
        no longer in a shape this release understands has to reach `_UNPARSABLE`, because the
        "your backend and this release do not agree on a wire format" answer is the only thing that
        made the 0.9→0.10 break diagnosable instead of looking like an unindexed repository. A
        parser that guessed rather than refused would take that away.
        """
        try:
            return json.loads(stdout)
        except ValueError:
            pass
        parsed = wire_text.parse(method, stdout.decode("utf-8", "replace"))
        if parsed is not None:
            return parsed
        self._saw_unparsable = True
        return self._UNPARSABLE

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
