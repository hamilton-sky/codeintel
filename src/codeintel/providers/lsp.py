from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, ClassVar

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from codeintel.graph_render import _FILE_EXTENSIONS
from codeintel.loc import loc, span
from codeintel.outcome import Missing, Ok, Outcome
from codeintel.provider import Result, attach_confidence, log_swallowed, safe_null_result

logger = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 60
# A cold serena has to boot the language server AND let it load the workspace before the first
# query can be answered. Measured against a real 841-file TypeScript repo, that first `symbol`
# call took 11.65s — more than twice the 5s this used to allow, so it timed out every time and the
# empty reference list that fell out of it was rendered as "(none)". The budget is now sized for
# the cold path; a warm call returns in ~1s and never approaches it.
_DEFAULT_TIMEOUT_S = 30.0

# How long a query may wait for a WARMING session to finish booting before it gives up and returns
# the `warming` safe-null (see `build_result`). Sized from the measured boot, not guessed: the
# `initialize` handshake settled in 2.30s / 2.50s / 1.85s on daycap (70 files), brightsky-ai (803
# TypeScript files) and pathly-adapters respectively — flat with repo size, because the handshake
# does not load the workspace. That load is the 11.65s the dispatch budget above is sized for, and
# it happens on the far side of READY, so this wait and that timeout do not overlap.
#
# 8s is ~3x the worst measured boot. It is deliberately NOT sized for a cold `uvx` that must still
# resolve and download serena-agent; that case exceeds any bound worth holding an agent's call open
# for, and degrading to `warming` — with `retry_after_s` in the envelope's gaps — is the right
# answer for it.
_WARM_WAIT_S = 8.0

# Serena ships as the `serena-agent` package (executable name `serena`); `uvx serena` does NOT
# work ("Package `serena` does not provide any executables"). The working invocation — verified
# against the installed serena and the machine's own serena MCP config — pulls it straight from
# the upstream repo and starts the stdio MCP server, binding the project via `--project`.
#
# Pin the commit, not a tag or git HEAD. Serena is a live wire dependency: command names and MCP
# payloads have changed upstream, and an unreviewed update can otherwise break every LSP answer in
# an already-released codeintel build. v1.7.0 resolves to this commit; bump only with the live
# contract job green against the replacement.
_SERENA_REV = "949a27ef1e5fda1a6e7b561e777bcece345c6ffd"
_SERENA_GIT = f"git+https://github.com/oraios/serena@{_SERENA_REV}"

# The "LSP toolchain is not installed" remediation, stated ONCE — see the matching block in
# providers/graph.py for why the query envelope and `doctor` must not keep separate copies.
_UNAVAILABLE_DETAIL = "neither `serena` nor `uvx` found on PATH"
_UNAVAILABLE_REMEDIATION = (
    "install uv (provides uvx): `codeintel setup --install-uv` (or `brew install uv` / "
    "`pip install uv`) — serena is then fetched on first use"
)
_UNAVAILABLE_HINT = (
    f"{_UNAVAILABLE_DETAIL}; {_UNAVAILABLE_REMEDIATION}. "
    "This is NOT evidence the symbol is absent: the engine was never asked."
)


# Prefixes serena uses when a tool call fails. Checked in addition to the MCP `isError` flag,
# which is not set by every server or version — and the cost of missing one is that a failure is
# served to an agent as an answer.
_BACKEND_ERROR_MARKERS = (
    "error executing tool",
    "exception:",
    "traceback (most recent call last)",
    "the language server manager is not initialized",
)


def _split_target_file_hint(target: str) -> tuple[str, str, str]:
    """Return Serena's symbol pattern and an optional repo-relative file hint.

    Graph operations accept ``name@path`` to disambiguate duplicate symbols. The gateway may pass
    that exact target to the LSP for an independent reference cross-check, but Serena understands
    only the name-path portion. Keep the syntax aligned here without coupling the LSP provider to
    graph.py's private target parser.
    """
    raw = str(target or "").strip()
    if "@" not in raw:
        return raw, "", ""
    head, _, tail = raw.rpartition("@")
    if not head.strip() or not tail.strip():
        return raw, "", ""
    symbol, file_hint = head.strip(), tail.strip()
    # Query broadly by leaf and filter client-side. Serena splits module identity differently by
    # language, while the file hint and complete symbol-path suffix together are stable.
    suffix = symbol.rpartition(".")[2].lower()
    qualified_hint = (
        symbol.replace(".", "/")
        if "." in symbol and suffix not in _FILE_EXTENSIONS
        else ""
    )
    leaf = symbol.rpartition(".")[2] if qualified_hint else symbol
    return leaf, file_hint, qualified_hint


def _name_path_suffix_score(name_path: Any, qualified_hint: str) -> int:
    """Number of trailing graph/Serena symbol segments that agree."""
    have = str(name_path or "").strip("/").split("/")
    want = str(qualified_hint or "").strip("/").split("/")
    score = 0
    for left, right in zip(reversed(have), reversed(want), strict=False):
        if left != right:
            break
        score += 1
    return score


def _qualified_definition_score(match: dict, qualified_hint: str) -> int:
    """Score a Serena symbol path within an already exact-file-filtered candidate set."""
    symbol_score = _name_path_suffix_score(match.get("name_path"), qualified_hint)
    name_parts = str(match.get("name_path") or "").strip("/").split("/")
    # The whole Serena name path must be a suffix of the graph qualifier. An extra enclosing symbol
    # is never a module and would make selection ambiguous. Require container + leaf: accepting a
    # top-level ``resolve`` would leave ``MissingClass`` entirely unverified.
    wanted_parts = str(qualified_hint or "").strip("/").split("/")
    return (symbol_score if symbol_score == len(name_parts) == len(wanted_parts)
            and symbol_score >= 2 else 0)


def _file_hint_matches(file_path: Any, hint: str) -> bool:
    """Match a file hint on path-segment boundaries, consistently with the graph provider."""
    have = str(file_path or "").replace("\\", "/").strip().strip("/").lower()
    want = str(hint or "").replace("\\", "/").strip().strip("/").lower()
    return bool(have and want and (have == want or have.endswith("/" + want)))


def _looks_like_backend_error(text: str) -> bool:
    """Whether *text* is a backend failure message rather than a result.

    Anchored to the START of the payload, and only after ruling out JSON. Both guards are load
    bearing, and the second was added because the first was not enough: a `symbol` lookup quotes
    real source back, so a perfectly good JSON response whose body contained
    `raise RuntimeError('Exception: bad input')` matched a substring search inside its first few
    hundred characters. Hiding real answers to catch errors would just trade one silent wrong
    answer for another — serena's failures are plain prose beginning with a known phrase, and a
    successful response is JSON, so the two never overlap.
    """
    head = text.lstrip()
    if head[:1] in ("[", "{"):
        return False                     # a structured response, whatever it happens to quote
    return head.lower().startswith(_BACKEND_ERROR_MARKERS)


def _summarize_backend_error(text: str | None) -> str:
    """A short, SAFE description of a backend failure — never the backend's own prose.

    The raw text is not forwarded anywhere a caller can see it. serena's failure messages contain
    instructions addressed to a language model ("do not attempt workarounds. Inform the user and
    wait for further instructions before you continue!") plus a dump of LSP initialisation
    parameters. Passing that through would hand a backend's error path a direct line to the
    calling agent's instructions, and leak internals in the same breath. The full text is logged
    for the operator instead; the caller gets a fixed, boring summary.
    """
    if text:
        logger.warning("serena returned an error result: %s", text[:2000])
    return "the language server reported an error for this query"


def _open_errlog():
    """Where serena's own stderr goes. Serena logs ~30 lines of INFO on every boot; inherited,
    that noise lands on top of `codeintel doctor --deep`'s report and any CLI query that warms
    the LSP — making the diagnostic command the least readable output in the tool. Discard it by
    default; set ``CODEINTEL_DEBUG=1`` to pass it through when debugging a boot failure."""
    if os.environ.get("CODEINTEL_DEBUG", "").strip().lower() in ("1", "true", "on", "yes"):
        return sys.stderr
    try:
        return open(os.devnull, "w", encoding="utf-8")
    except Exception:
        return sys.stderr


def _serena_launch_args(cmd: str, project_root: str) -> list[str]:
    """Build the real serena start-mcp-server argv. Kept pure + module-level so the exact
    contract (the thing that had drifted) can be asserted without launching a subprocess."""
    common = [
        "start-mcp-server",
        "--context", "ide-assistant",       # tool set tuned for a coding agent, no chat scaffolding
        "--enable-web-dashboard", "false",  # headless: don't pop a browser from a background thread
        "--project", project_root,          # bind the project at launch (tools take no project arg)
    ]
    if cmd == "uvx":
        return ["uvx", "--from", _SERENA_GIT, "serena", *common]
    # A directly-installed `serena` (or serena-mcp-server shim) on PATH.
    return [cmd, *common]


def _project_access_failure(project_root: str) -> tuple[str, str] | None:
    """Return a safe diagnosis when the host cannot enumerate the project root.

    Serena's stdio transport wraps an early subprocess exit in an ``ExceptionGroup``.  When the
    real cause is a macOS Files & Folders denial, reporting that wrapper as a network/bootstrap
    failure sends the user in exactly the wrong direction.  A one-entry scan is cheap, local, and
    establishes the prerequisite Serena itself needs without forwarding any backend-controlled
    prose to the calling agent.
    """
    try:
        with os.scandir(project_root) as entries:
            next(entries, None)
    except OSError as exc:
        location = str(getattr(exc, "filename", None) or project_root)
        detail = f"repository root is not readable by codeintel ({type(exc).__name__}: {location})"
        remediation = (
            "grant the codeintel host read access to the repository, then retry; on macOS check "
            "System Settings > Privacy & Security > Files and Folders (or Full Disk Access)"
        )
        return detail, remediation
    return None


class _State(enum.Enum):
    WARMING = "WARMING"
    READY = "READY"
    FAILED = "FAILED"


class _LspSession:
    # Class-level defaults so a session built via `__new__` (the test stubs do this) still answers
    # these rather than raising AttributeError from inside a failure handler — the same reason
    # `LspProvider._last_backend_error` carries one.
    attempt: int = 1
    boot_error: str | None = None

    def __init__(self, project_root: str, cmd: str, attempt: int = 1) -> None:
        self.state = _State.WARMING
        self.cooldown_until: float = 0.0
        # Which boot this is for this repo (1 = the first one this process has tried). Read by
        # `_boot_failed_hint` to tell a cold `uvx` — which must resolve and download serena-agent
        # from git before it can even start — apart from an install that is genuinely broken.
        # Set once and never mutated, so it is safe to read without `_lock`.
        self.attempt = attempt
        # Exception TYPE that ended the boot, for the caller's hint. The type only: a boot failure
        # can carry subprocess output, and `_summarize_backend_error` explains at length why this
        # provider does not forward backend prose to an agent. The full exception is logged.
        self.boot_error: str | None = None
        self._lock = threading.Lock()
        # Set once the boot resolves either way (READY or FAILED), so a caller can wait for the
        # outcome instead of polling the state behind `_lock`. See `wait_until_settled`.
        self.settled = threading.Event()
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._mcp_session: ClientSession | None = None
        self._thread = threading.Thread(
            target=self._run,
            args=(project_root, cmd),
            daemon=True,
        )
        self._thread.start()

    def wait_until_settled(self, timeout_s: float) -> _State:
        """Block up to *timeout_s* for the boot to resolve, then report the state.

        Returns `WARMING` when the wait expired with the boot still in flight — the caller then
        degrades exactly as it did before there was a wait at all.
        """
        if timeout_s > 0:
            self.settled.wait(timeout_s)
        with self._lock:
            return self.state

    def _run(self, project_root: str, cmd: str) -> None:
        try:
            self._loop.run_until_complete(self._warmup(project_root, cmd))
        except Exception as exc:
            logger.warning("serena boot failed for %s (attempt %d): %s",
                           project_root, self.attempt, exc)
            with self._lock:
                # Written BEFORE the state. A reader takes `_lock` to learn the state and then
                # reads `boot_error` without it; publishing FAILED first leaves a window where the
                # cause is still None and the hint silently degrades to its generic form.
                self.boot_error = type(exc).__name__
                self.state = _State.FAILED
                self.cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
        finally:
            # Unblock any waiter whatever happened — including the path where `_warmup` returns
            # without ever reaching READY. A waiter that is never woken would burn its whole
            # timeout on a session that already settled.
            self.settled.set()
            try:
                self._loop.close()
            except Exception:
                pass

    async def _warmup(self, project_root: str, cmd: str) -> None:
        from mcp import StdioServerParameters

        launch_args = _serena_launch_args(cmd, project_root)
        errlog = _open_errlog()
        try:
            async with stdio_client(
                StdioServerParameters(command=launch_args[0], args=launch_args[1:]),
                errlog=errlog,
            ) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                with self._lock:
                    self._mcp_session = session
                    self.state = _State.READY
                self.settled.set()
                # Keep the loop (and the subprocess/session it owns) alive so _call_tool can
                # schedule coroutines onto it. Resolves only when the thread/loop is torn down.
                await asyncio.get_running_loop().create_future()
        finally:
            if errlog is not sys.stderr:
                try:
                    errlog.close()
                except Exception:
                    pass


class LspProvider:
    """Wraps serena's LSP-over-MCP bridge. Never raises.

    Serena tool contract (verified live, not assumed):
      * ``find_symbol``             — arg ``name_path_pattern``; returns a JSON list of
                                      ``{name_path, kind, relative_path, body_location, body?}``.
      * ``find_referencing_symbols`` — args ``name_path`` AND ``relative_path`` (both required);
                                      returns ``{file: {kind: [{name_path, content_around_reference}]}}``.
      * ``get_symbols_overview``    — arg ``relative_path``; returns ``{kind: [names]}``.
    No tool takes a ``project_root`` — the project is bound once at launch via ``--project``.
    Finding references therefore needs two steps: locate the symbol, then query with its path.
    """

    # Read by `Gateway._dispatch_single`, which short-circuits on `available is False` and so never
    # reaches this provider's `build_result`. See the note on GraphProvider.unavailable_hint.
    unavailable_hint = _UNAVAILABLE_HINT

    # Class-level default so a provider built via `__new__` (the test stubs do this) still has it
    # rather than raising AttributeError inside the never-raise handler.
    _backend_error_state = threading.local()
    _backend_missing_state = threading.local()
    # Sections of the current answer that are known to be short of an answer. An op appends here
    # instead of quietly rendering an empty section, and `build_result` turns them into the
    # envelope's `gaps` / `confidence`. Class-level default for the same __new__ reason as above.
    _gap_state = threading.local()
    # Same __new__ reason: `_boot_failed_hint` reads this, and a session injected straight into
    # `_sessions` reaches it without `_detect_backend` having run.
    _cmd: str | None = None

    @property
    def _last_backend_error(self) -> str | None:
        value = getattr(self._backend_error_state, "value", None)
        return value if isinstance(value, str) else None

    @_last_backend_error.setter
    def _last_backend_error(self, value: str | None) -> None:
        self._backend_error_state.value = value

    @property
    def _pending_gaps(self) -> tuple[dict[str, Any], ...]:
        value = getattr(self._gap_state, "value", ())
        return value if isinstance(value, tuple) else ()

    @_pending_gaps.setter
    def _pending_gaps(self, value: tuple[dict[str, Any], ...]) -> None:
        self._gap_state.value = value

    def __init__(self) -> None:
        self._sessions: dict[str, _LspSession] = {}
        self._sessions_lock = threading.Lock()
        self._last_backend_error = None
        self._set_backend_missing(None)
        self._pending_gaps = ()
        self._detect_backend()

    def _clear_backend_error(self) -> None:
        self._last_backend_error = None
        self._set_backend_missing(None)
        self._pending_gaps = ()

    def _set_backend_missing(self, missing: Missing | None) -> None:
        self._backend_missing_state.value = missing

    def _get_backend_missing(self) -> Missing | None:
        value = getattr(self._backend_missing_state, "value", None)
        return value if isinstance(value, Missing) else None

    def _add_gap(self, section: str, missing: Missing) -> None:
        """Record that a named part of the answer could not be retrieved. The body text says so
        too — this is the machine-readable half of the same statement."""
        gap: dict[str, Any] = {
            "section": section,
            "kind": missing.kind,
            "detail": missing.describe(),
        }
        if missing.retry_after_s is not None:
            gap["retry_after_s"] = missing.retry_after_s
        self._pending_gaps = (*self._pending_gaps, gap)

    def _detect_backend(self) -> None:
        # Prefer a directly-installed serena; otherwise drive it through uvx.
        if shutil.which("serena"):
            self.available = True
            self._cmd: str | None = "serena"
        elif shutil.which("uvx"):
            self.available = True
            self._cmd = "uvx"
        else:
            self.available = False
            self._cmd = None

    def _get_or_create_session(self, root: str) -> _LspSession:
        with self._sessions_lock:
            # Counted across respawns rather than stored per-session, so a SECOND boot is never
            # excused as a cold cache: `uvx` populates its cache on the first attempt, so the
            # "still downloading" reading is only ever true once.
            attempt = 1
            existing = self._sessions.get(root)
            if existing is not None:
                with existing._lock:
                    if existing.state == _State.FAILED:
                        if time.monotonic() > existing.cooldown_until:
                            attempt = existing.attempt + 1
                            del self._sessions[root]  # cooldown elapsed → allow one respawn
                        else:
                            return existing  # still cooling down — no per-request respawn
                    else:
                        return existing
            session = _LspSession(root, self._cmd, attempt=attempt)  # type: ignore[arg-type]
            self._sessions[root] = session
            return session

    # Serena serves the language servers named in the project's own config and nothing else. The
    # extensions that decide whether a repo NEEDS one, keyed by serena's language identifiers.
    _LANG_EXTS: ClassVar[dict[str, tuple[str, ...]]] = {
        "python": (".py", ".pyi"),
        "typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        "go": (".go",),
        "rust": (".rs",),
        "java": (".java",),
        "csharp": (".cs",),
        "ruby": (".rb",),
        "php": (".php",),
        "cpp": (".cpp", ".cc", ".hpp", ".hh", ".cxx"),
        "c": (".c", ".h"),
        "kotlin": (".kt", ".kts"),
        "swift": (".swift",),
    }
    _SKIP_DIRS: ClassVar[frozenset[str]] = frozenset({
        ".git", "node_modules", ".venv", "venv", "dist", "build", "coverage",
        "__pycache__", ".mypy_cache", ".pytest_cache", "vendor", "target", ".next",
    })
    _UNSERVED_FILE_FLOOR = 5      # below this, a stray file is not a language the repo is written in
    _TS_SOURCE_EXTS: ClassVar[tuple[str, ...]] = (".ts", ".tsx")
    _TS_PROJECT_FILES: ClassVar[tuple[str, ...]] = ("tsconfig.json", "jsconfig.json")

    def _language_coverage(self, project_root: str) -> tuple[list[str], dict[str, int]]:
        """Which languages serena is configured to serve here, and which the repo actually contains.

        A polyglot repository gets ONE serena config, and that config names a fixed list of language
        servers. On an evaluated monorepo it read `language_servers: [typescript]` while the tree
        held 69 Python files under `services/*/src`, so every Python `symbol` query returned an empty
        body — and the doctor reported the engine "ok / reached READY", which was true about the
        process and false about the answers. That is the worst shape a health check can take: green
        while the thing it certifies is silently serving nothing.

        Only the config is authoritative about what is served; the file census is a plain walk,
        bounded by the vendored-directory skip list, because an unserved language matters in
        proportion to how much of the repo is written in it."""
        configured: list[str] = []
        cfg = os.path.join(project_root, ".serena", "project.yml")
        try:
            with open(cfg, encoding="utf-8") as fh:
                in_block = False
                for line in fh:
                    stripped = line.strip()
                    if stripped.startswith("language_servers:"):
                        in_block = True
                        continue
                    if in_block:
                        if stripped.startswith("- "):
                            configured.append(stripped[2:].strip().strip('"\''))
                            continue
                        if stripped and not stripped.startswith("#"):
                            break
        except OSError:
            return [], {}
        if not configured:
            return [], {}
        ext_to_lang = {e: lang for lang, exts in self._LANG_EXTS.items() for e in exts}
        census: dict[str, int] = {}
        for _dirpath, dirnames, filenames in os.walk(project_root):
            dirnames[:] = [d for d in dirnames if d not in self._SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                lang = ext_to_lang.get(os.path.splitext(fn)[1].lower())
                if lang:
                    census[lang] = census.get(lang, 0) + 1
        return configured, census

    def _unserved_note(self, project_root: str) -> tuple[str, str] | None:
        """`(detail_suffix, remediation)` when the repo holds a language serena will not answer for."""
        try:
            configured, census = self._language_coverage(project_root)
        except Exception as exc:
            log_swallowed("LspProvider._unserved_note", exc)
            return None
        if not configured or not census:
            return None
        missing = sorted(
            ((lang, n) for lang, n in census.items()
             if lang not in configured and n >= self._UNSERVED_FILE_FLOOR),
            key=lambda t: -t[1],
        )
        if not missing:
            return None
        named = ", ".join(f"{lang} ({n} files)" for lang, n in missing[:4])
        return (
            f" — but .serena/project.yml serves only {', '.join(configured)}, so {named} "
            f"get NO answer from this engine (empty `symbol` results, not errors)",
            f"add the missing language(s) to `language_servers:` in "
            f"{os.path.join('.serena', 'project.yml')} and re-run, or use `--engine graph` for "
            f"{missing[0][0]} symbols",
        )

    def _ts_project_files(self, project_root: str) -> int | None:
        """``.ts``/``.tsx`` count when this tree has TypeScript that no project file covers.

        ``None`` means there is nothing to report — the config does not serve typescript, a project
        file exists somewhere, or there is too little TypeScript here to call it a TypeScript
        repository. One walk, shared by the two callers below so the health check and the query
        envelope cannot drift apart on what counts as "no project": #32 made each engine's
        remediation a single constant for exactly that reason, and a predicate is the same problem.
        """
        try:
            configured, _census = self._language_coverage(project_root)
            if "typescript" not in configured:
                return None                    # unserved entirely — `_unserved_note` owns that case
            ts_files = 0
            for _dirpath, dirnames, filenames in os.walk(project_root):
                dirnames[:] = [d for d in dirnames
                               if d not in self._SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    # Any project file anywhere is enough to stop guessing. A monorepo keeps them
                    # per-package, and claiming a repo has none because the root has none would be
                    # the same false-confidence move this check exists to catch, pointed the other
                    # way.
                    if fn in self._TS_PROJECT_FILES or (
                            fn.startswith("tsconfig.") and fn.endswith(".json")):
                        return None
                    if fn.endswith(self._TS_SOURCE_EXTS):
                        ts_files += 1
            return ts_files if ts_files >= self._UNSERVED_FILE_FLOOR else None
        except Exception as exc:
            log_swallowed("LspProvider._ts_project_files", exc)
            return None

    def _no_tsproject_note(self, project_root: str) -> tuple[str, str] | None:
        """`(detail_suffix, remediation)` when TypeScript is served but has no project to resolve in.

        `_unserved_note` catches the language serena was never told to serve. This is the other half
        of the same question — "will it answer for this repo's code?" — and it is much quieter. The
        language IS configured, serena boots, `symbol` returns the definition with its source, and
        every reference lookup comes back EMPTY, because `tsserver` with no `tsconfig.json` treats
        each file as its own inferred project and cannot see across files.

        Measured on `bench/fixtures/corpus_ts`, whose 20 files deliberately ship no tsconfig so the
        oracle's unresolvable-specifier guard has something to bite on: `forwardReleasedItem` is
        imported and called in four of them, the LSP reported 0 references, and `doctor --deep`
        reported `3 / 3 engines ready`. Copying the tree and adding a plain tsconfig turned the same
        query into 17 references — so the gap is the config, and the health check was green across
        it. `.ts`/`.tsx` only: a handful of loose `.js` files is not a TypeScript project, and
        flagging those would teach a reader to ignore the line.
        """
        ts_files = self._ts_project_files(project_root)
        if ts_files is None:
            return None
        return (
            f" — but no tsconfig.json covers the {ts_files} TypeScript files here, so the language "
            f"server resolves each one alone: `symbol` returns the definition and an EMPTY "
            f"reference list, which reads as 'nothing references this'",
            "add a tsconfig.json whose `include` covers the TypeScript sources (or point "
            "--project-root at the directory that already has one) and re-run; until then use "
            "`--engine graph` for callers, and do not read 0 references as 0 callers",
        )

    def _empty_references_unsound(self, project_root: str, rel_path: str) -> Missing | None:
        """Why an EMPTY reference list here carries no information, or ``None`` if it does.

        This is the query-path half of `_no_tsproject_note`, and the more important half: `doctor`
        is advisory and an agent need never run it, while this reaches whoever actually asked. An
        empty list from a language server with no project to resolve in is not "nothing references
        this" — it is "this backend was never in a position to tell you", which is what
        `outcome.py` now has a kind for.

        Scoped to the file the symbol was actually found in, NOT to the repository. A polyglot tree
        with loose TypeScript and no tsconfig must not cast doubt on a Python answer that was
        resolved perfectly well; the unsound emptiness belongs only to the language whose server
        could not resolve.
        """
        try:
            if not rel_path.lower().endswith(self._TS_SOURCE_EXTS):
                return None
            ts_files = self._ts_project_files(project_root)
            if ts_files is None:
                return None
        except Exception as exc:
            log_swallowed("LspProvider._empty_references_unsound", exc)
            return None
        return Missing(
            "unresolvable",
            f"the language server returned no references, but no tsconfig.json covers the "
            f"{ts_files} TypeScript files in this repository — with no project it resolves each "
            f"file alone and answers every cross-file lookup empty, so this is UNKNOWN rather than "
            f"none. Add a tsconfig.json covering the sources, or use `--engine graph` for callers",
        )

    def _a_served_source_file(self, project_root: str) -> str | None:
        """One repo-relative source file in a language this config actually serves, or ``None``.

        The deep answer check needs a question with a known-good subject, and a symbol NAME is the
        wrong kind of subject: picking one means guessing what this repository contains, and a
        wrong guess produces an empty answer that says nothing about the engine. A FILE the config
        serves is knowable from the tree, so an empty answer about it is a fact about the server.

        Prefers the most-populous served language, so the file is representative rather than the
        first one `os.walk` happens upon.
        """
        try:
            configured, census = self._language_coverage(project_root)
            served = [lang for lang in configured if census.get(lang)]
            if not served:
                return None
            best = max(served, key=lambda lang: census.get(lang, 0))
            wanted = self._LANG_EXTS.get(best, ())
            if not wanted:
                return None
            for dirpath, dirnames, filenames in os.walk(project_root):
                dirnames[:] = [d for d in dirnames
                               if d not in self._SKIP_DIRS and not d.startswith(".")]
                for fn in sorted(filenames):
                    if fn.endswith(wanted):
                        full = os.path.join(dirpath, fn)
                        return os.path.relpath(full, project_root)
            return None
        except Exception as exc:
            log_swallowed("LspProvider._a_served_source_file", exc)
            return None

    def _deep_answer(self, project_root: str, timeout_s: float) -> tuple[bool | None, str]:
        """Does the language server actually ANSWER about this repository's code?

        `READY` is a fact about the PROCESS, and this file already says so where the state is read.
        `_unserved_note` and `_no_tsproject_note` close two specific ways a READY server answers
        nothing — a language missing from the config, and TypeScript with no `tsconfig.json`. Both
        are checks on the CONFIGURATION, which is one inference away from the thing a reader wants:
        neither of them asks the server a question.

        So this asks one. `get_symbols_overview` on a served source file needs no symbol name, no
        index and no prior query, and its answer is content or it is not. An empty answer here
        means the server booted and cannot read this repository's code — which is exactly the state
        that reported `3 / 3 engines ready` over `bench/fixtures/corpus_ts` while every reference
        lookup came back empty.

        Returns ``(answered, detail)``; ``answered`` is ``None`` when the question could not be put.
        """
        rel = self._a_served_source_file(project_root)
        if rel is None:
            return None, "no file in a served language to verify against"
        try:
            session = self._get_or_create_session(project_root)
            out = self._call_tool(
                session, "get_symbols_overview", {"relative_path": rel}, timeout_s)
        except Exception as exc:
            log_swallowed("LspProvider._deep_answer", exc)
            return None, "the verification query raised"
        if isinstance(out, Missing):
            return None, f"the verification query did not complete ({out.kind})"
        text = self._extract_text(out.value)
        if not text or not str(text).strip():
            return False, f"it returned nothing for `{rel}`"
        parsed = self._loads(str(text))
        if isinstance(parsed, dict) and not any(parsed.values()):
            return False, f"it reported no symbols at all in `{rel}`"
        return True, f"and it answered a real query about `{rel}`"

    def probe(self, project_root: str, deep: bool = False, timeout_s: float = 20.0) -> dict:
        """Never-raise health check for the doctor. Shallow (default) is FREE — PATH presence
        plus any existing session's live state. Deep boots serena and polls until READY/FAILED,
        bounded by ``timeout_s`` (first boot pulls serena via uvx and is slow). ``repo_indexed``
        is always None: serena keeps no persistent index, it warms per-root on demand."""
        if not self.available:
            return {
                "installed": False, "runnable": False, "repo_indexed": None,
                "detail": _UNAVAILABLE_DETAIL,
                "remediation": _UNAVAILABLE_REMEDIATION,
            }
        cmd = self._cmd
        if not deep:
            existing = self._sessions.get(project_root)
            if existing is None:
                return {
                    "installed": True, "runnable": None, "repo_indexed": None,
                    "detail": f"serena via `{cmd}`; boot not verified (warms on 1st query; --deep to check now)",
                    "remediation": None,
                }
            with existing._lock:
                st = existing.state
            if st == _State.READY:
                unserved = (self._unserved_note(project_root)
                            or self._no_tsproject_note(project_root))
                return {"installed": True, "runnable": unserved is None, "repo_indexed": None,
                        "detail": "serena session is READY for this repo" + (
                            unserved[0] if unserved else ""),
                        "remediation": unserved[1] if unserved else None}
            if st == _State.FAILED:
                return {"installed": True, "runnable": False, "repo_indexed": None,
                        "detail": "serena session failed to boot for this repo",
                        "remediation": "re-run `codeintel doctor --deep` to see the boot error"}
            return {"installed": True, "runnable": None, "repo_indexed": None,
                    "detail": "serena session is warming for this repo", "remediation": None}

        # deep: boot (or reuse) a session and poll to a hard deadline — never hangs.
        access_failure = _project_access_failure(project_root)
        if access_failure is not None:
            detail, remediation = access_failure
            return {"installed": True, "runnable": False, "repo_indexed": None,
                    "detail": detail, "remediation": remediation}

        session = self._get_or_create_session(project_root)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with session._lock:
                st = session.state
            if st == _State.READY:
                # READY is a fact about the PROCESS. Whether it will answer for this repo's code is
                # a separate question, and the one the caller is actually asking.
                unserved = (self._unserved_note(project_root)
                            or self._no_tsproject_note(project_root))
                if unserved is not None:
                    return {"installed": True, "runnable": False, "repo_indexed": None,
                            "detail": f"serena booted via `{cmd}` and reached READY"
                                      + unserved[0],
                            "remediation": unserved[1]}
                # Both notes above read the CONFIGURATION. This asks the server.
                answered, why = self._deep_answer(project_root, timeout_s)
                return {"installed": True, "runnable": answered, "repo_indexed": None,
                        "detail": f"serena booted via `{cmd}` and reached READY " + why,
                        "remediation": None if answered else (
                            "the language server is running but returns nothing for this "
                            "repository's code — check `.serena/project.yml` names the right "
                            "language(s), and that the project builds")}
            if st == _State.FAILED:
                return {"installed": True, "runnable": False, "repo_indexed": None,
                        "detail": "serena failed to boot",
                        "remediation": f"check uvx + network: `uvx --from {_SERENA_GIT} "
                                       "serena start-mcp-server`"}
            time.sleep(0.5)
        return {"installed": True, "runnable": None, "repo_indexed": None,
                "detail": f"serena did not reach READY within {int(timeout_s)}s (still warming)",
                "remediation": "retry — first boot pulls serena via uvx and can be slow"}

    def _boot_failed_hint(self, session: _LspSession) -> str:
        """What a caller should do about a serena boot that did not finish.

        `boot-failed` carried no hint at all, so the FIRST query against a repo — the one an agent
        makes when it starts work — reported a broken engine while `uvx` was merely still
        resolving and downloading serena-agent from git. Run again a minute later, the same serena
        booted fine. A one-time install cost and a broken install are opposite conclusions (retry
        vs. go fix your machine) and they were reaching the caller as the same word.

        The distinction is exactly the two facts checked here: this is the first boot this process
        has attempted for the repo, and serena is being driven through `uvx` rather than an
        installed binary — the only combination in which "not finished yet" is a live reading.
        A respawn (`attempt > 1`) has a warm `uvx` cache, so it gets the diagnostic phrasing.
        """
        detail = (f"the serena session did not start ({session.boot_error})"
                  if session.boot_error else "the serena session did not start")
        if session.attempt <= 1 and self._cmd == "uvx":
            return (
                f"{detail}. This was the first boot attempt for this repo and serena is being run "
                f"through `uvx`, which resolves and downloads serena-agent from git on a cold "
                f"cache — that is a one-time cost which can outlast this call, NOT a broken "
                f"install. Retry the query in a minute, or run `codeintel setup --all <repo>` "
                f"once to warm it. If it still fails, `codeintel doctor --deep` boots serena and "
                f"reports why."
            )
        return (
            f"{detail}, and this is not a cold cache — `codeintel doctor --deep` boots serena and "
            f"reports why (set CODEINTEL_DEBUG=1 to pass serena's own stderr through)."
        )

    def build_result(
        self,
        op: Any,
        target: Any,
        files: Any,
        budget: Any,
        project_root: Any,
    ) -> Result:
        try:
            op_str = str(op or "")
            target_str = str(target or "")
            root_str = str(project_root or "")

            if not self.available:
                return safe_null_result(
                    op_str, target_str, engine="lsp", reason="engine-unavailable",
                    hint=_UNAVAILABLE_HINT,
                )

            # A protected macOS folder can still satisfy ``isdir`` while denying enumeration.
            # Detect that locally before spawning Serena: otherwise its early exit is wrapped by
            # the MCP transport as a generic boot failure and the first-query hint talks about a
            # cold uvx cache instead of the permission the user actually needs to grant.  Keep the
            # ``isdir`` guard so synthetic/nonexistent roots used by embedders retain the existing
            # never-raise backend behaviour.
            if os.path.isdir(root_str):
                access_failure = _project_access_failure(root_str)
                if access_failure is not None:
                    detail, remediation = access_failure
                    return safe_null_result(
                        op_str, target_str, engine="lsp", reason="source-unreadable",
                        hint=f"{detail}; {remediation}",
                    )

            try:
                budget_ms = int(budget) if budget else 0
            except Exception:
                budget_ms = 0
            timeout_s = (budget_ms / 1000) if budget_ms > 0 else _DEFAULT_TIMEOUT_S

            session = self._get_or_create_session(root_str)

            with session._lock:
                state = session.state

            if state == _State.WARMING:
                # Wait, briefly, rather than returning `warming` the instant a session is booting.
                #
                # Returning immediately made the LSP engine effectively unavailable on the FIRST
                # call of every session — which is exactly the call an agent makes when it starts
                # work on a repo. Worse, `callers`/`context` fold the LSP in as a CROSS-CHECK of
                # graph rows resolved by bare name: the `[?…]` badges say "unverified, ask the LSP",
                # and the engine that would verify them had always just declined. Dogfooding on
                # three repos hit that on every first query and never once saw the cross-check
                # arrive. `commands/query.py` already worked around it with a 45s retry loop, but
                # that loop lives in the CLI, so the MCP and HTTP transports — the ones an agent
                # actually calls — never got the benefit.
                #
                # Bounded well under the dispatch timeout this method already grants itself, so a
                # boot that is genuinely slow still degrades to the same safe null instead of
                # holding the call open. `_WARM_WAIT_S` is a ceiling, not a cost: the wait ends the
                # moment the session settles, and a session that is already READY never enters
                # this branch at all.
                state = session.wait_until_settled(min(_WARM_WAIT_S, timeout_s))
                if state == _State.WARMING:
                    # A bare `warming` is a dead end. It says the engine did not answer and
                    # nothing about whether asking again would help, how long that would take, or
                    # what can answer meanwhile — so a reader treats the engine as unusable and
                    # does not come back to it. One evaluation session did exactly that and never
                    # touched the LSP engine again after the first call.
                    #
                    # This is an inconsistency, not an oversight in the design: `_WARM_WAIT_S`'s
                    # own note above says degrading to `warming` is the right answer for a cold
                    # `uvx` "with `retry_after_s` in the envelope", and the `boot-failed` branch
                    # immediately below has carried `retry_after_s` since it was written. Only
                    # this branch — the one on the common path, hit on the first call of every
                    # session — was left bare.
                    #
                    # The number is the wait this call just spent rather than a guess at the
                    # remaining boot. A session still WARMING after `_WARM_WAIT_S` is resolving
                    # and downloading `serena-agent` on a cold `uvx`, which runs to tens of
                    # seconds, so another `_WARM_WAIT_S` is an honest floor on when it is worth
                    # re-asking — not a promise that it will be ready by then.
                    warming = safe_null_result(
                        op_str, target_str, engine="lsp", reason="warming",
                        hint=f"the language server is still booting for this repository — this "
                             f"is not a statement about your code. Ask again in "
                             f"~{int(_WARM_WAIT_S)}s; the first boot of a session is the slow "
                             f"one and later calls answer from the warm session. Meanwhile the "
                             f"graph engine can answer callers/callees/impact now "
                             f"(`engine=\"graph\"`).",
                    )
                    warming["retry_after_s"] = _WARM_WAIT_S
                    return warming

            if state == _State.FAILED:
                failed = safe_null_result(
                    op_str, target_str, engine="lsp", reason="boot-failed",
                    hint=self._boot_failed_hint(session),
                )
                if session.attempt <= 1 and self._cmd == "uvx":
                    failed["retry_after_s"] = 60
                return failed

            # READY
            # Cleared through a method rather than a direct assignment: `_dispatch` sets this as a
            # side effect, which a type checker cannot see, so an inline `= None` narrows the
            # attribute to None and makes the branch below look unreachable.
            self._clear_backend_error()
            result_text = self._dispatch(session, op_str, target_str, root_str, timeout_s)
            if result_text is None:
                # A backend failure is not an unsupported op. Reporting it as one sends the agent
                # looking for a different tool when the language server simply did not start —
                # the same misleading string the graph provider already had to stop emitting.
                missing = self._get_backend_missing()
                if self._last_backend_error or missing:
                    failed = safe_null_result(
                        op_str, target_str, engine="lsp",
                        reason=missing.kind if missing is not None else "backend-error",
                        hint=f"{missing.describe() if missing else self._last_backend_error} — "
                             f"run `codeintel doctor --deep` to boot-"
                             f"check serena; the full backend message is in the server log",
                    )
                    if missing is not None and missing.retry_after_s is not None:
                        failed["retry_after_s"] = missing.retry_after_s
                    return failed
                return safe_null_result(op_str, target_str, engine="lsp", reason="unsupported-op")

            envelope: Result = {
                "ok": True,
                "op": op_str,
                "target": target_str,
                "result": result_text,
                "engine": "lsp",
                "cached": False,
            }
            # A non-null result is no longer a promise that the answer is whole. When a named
            # section could not be retrieved, say so in machine-readable form as well as in the
            # body — an agent that only reads `result` still sees it, and one that reads the
            # envelope can branch on it.
            return attach_confidence(envelope, self._pending_gaps)
        except Exception as exc:
            log_swallowed("LspProvider.build_result", exc)
            return safe_null_result(op, target, engine="lsp", reason="error")

    def _dispatch(
        self,
        session: _LspSession,
        op: str,
        target: str,
        root: str,
        timeout_s: float,
    ) -> str | None:
        if op == "symbol" or op == "context":
            # `context` (fan-out op) → the LSP's richest single-symbol view: definition + refs.
            return self._op_symbol(session, target, root, timeout_s)
        if op == "overview":
            return self._op_overview(session, target, root, timeout_s)
        return None

    def _call_tool(
        self,
        session: _LspSession,
        tool: str,
        args: dict,
        timeout_s: float,
    ) -> Outcome[Any]:
        """Call a serena tool, returning why it failed rather than collapsing failure to None.

        The distinction is the whole point: a caller that receives `Missing` cannot accidentally
        render it as an empty answer, which is what happened when this returned `None` on timeout
        and the reference renderer read that as "no references exist".
        """
        try:
            mcp_session = session._mcp_session
            if mcp_session is None:
                return Missing("not-asked", "the language server session was not available")
            coro = mcp_session.call_tool(tool, args)
            future = asyncio.run_coroutine_threadsafe(coro, session._loop)
            return Ok(future.result(timeout=timeout_s))
        except FuturesTimeout:
            return Missing(
                "timeout",
                "the language server had not finished loading this workspace in time",
                retry_after_s=5.0,
            )
        except Exception as exc:
            log_swallowed(f"LspProvider._call_tool({tool})", exc)
            return Missing("backend-error", "the language server did not answer this call")

    def _extract_text(self, raw: Any) -> str | None:
        """The text payload of a tool result, or None — including when the result is an ERROR.

        An MCP `CallToolResult` carries `isError`, and this read straight past it: serena's failure
        text was harvested like any other content and handed back as the answer. What an agent then
        received for "where is this symbol defined?" was `ok: true`, no `reason`, and a body reading

            Error executing tool find_symbol: Exception: The language server manager is not
            initialized … do not attempt workarounds. Inform the user and wait for further
            instructions before you continue!

        followed by a dump of the LSP initialisation params. Three separate problems in one string:
        it is a failure presented as a result, it leaks internals, and — worst — it carries
        imperative instructions aimed at a language model into a field an agent reads as data. A
        backend's error path must never become a channel for telling the caller's agent what to do.
        """
        if raw is None:
            return None
        if isinstance(raw, str):
            return None if _looks_like_backend_error(raw) else raw
        if getattr(raw, "isError", False):
            self._last_backend_error = _summarize_backend_error(self._raw_text(raw))
            return None
        text = self._raw_text(raw)
        # `isError` is not always set by every server/version, so the text shape is a second gate.
        if text is not None and _looks_like_backend_error(text):
            self._last_backend_error = _summarize_backend_error(text)
            return None
        return text

    @staticmethod
    def _raw_text(raw: Any) -> str | None:
        """Concatenated text of an MCP result's content blocks, with no error interpretation."""
        try:
            parts = [item.text for item in raw.content if hasattr(item, "text")]
            return "\n".join(parts) if parts else None
        except Exception:
            return None

    @staticmethod
    def _loads(text: str | None) -> Any:
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _ref_line(content: Any) -> int | None:
        """Pull the referenced line number out of serena's `content_around_reference` blob,
        which marks the reference line with a leading `>` (e.g. `  >   7:from ...`).

        Returned as serena reports it — 0-based. Conversion to the 1-based number a human or an
        editor expects belongs to `loc()`, and to nothing else."""
        if not isinstance(content, str):
            return None
        m = re.search(r">\s*(\d+):", content)
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    def _format_matches(self, target: str, matches: list) -> tuple[str, dict | None]:
        parts = [f"## Symbol: {target}"]
        first: dict | None = None
        for m in matches:
            if not isinstance(m, dict):
                continue
            if first is None:
                first = m
            kind = m.get("kind") or "symbol"
            rel = m.get("relative_path") or "?"
            raw_loc = m.get("body_location")
            body_loc = raw_loc if isinstance(raw_loc, dict) else {}
            # serena's body_location is 0-based; `span()` owns the conversion.
            parts.append(f"**{kind}** — {span(rel, body_loc.get('start_line'), body_loc.get('end_line'))}")
            body = m.get("body")
            if body:
                parts.append(f"```\n{body}\n```")
        return "\n".join(parts), first

    def _format_refs(self, data: Any) -> list[str]:
        lines: list[str] = []
        if not isinstance(data, dict):
            return lines
        for file, kinds in data.items():
            if not isinstance(kinds, dict):
                continue
            for entries in kinds.values():
                if not isinstance(entries, list):
                    continue
                for ent in entries:
                    if not isinstance(ent, dict):
                        continue
                    np = str(ent.get("name_path") or "").strip()
                    line0 = self._ref_line(ent.get("content_around_reference"))
                    suffix = f"  ({np})" if np else ""
                    lines.append(f"- {loc(file, line0)}{suffix}")
                    if len(lines) >= 50:
                        return lines
        return lines

    def _op_symbol(
        self, session: _LspSession, target: str, root: str, timeout_s: float
    ) -> str | None:
        try:
            deadline = time.monotonic() + timeout_s

            def remaining() -> float:
                return max(0.001, deadline - time.monotonic())

            symbol_target, file_hint, qualified_hint = _split_target_file_hint(target)
            def_out = self._call_tool(
                session,
                "find_symbol",
                {
                    "name_path_pattern": symbol_target,
                    "include_body": not (file_hint or qualified_hint),
                    # A file hint exists specifically because the name is ambiguous. Retrieve
                    # enough definitions to find the requested file instead of silently selecting
                    # whichever five Serena happens to return first.
                    "max_matches": 500 if (file_hint or qualified_hint) else 5,
                },
                remaining(),
            )
            if isinstance(def_out, Missing):
                # The tool call itself failed or timed out. Rendering "(not found)" here — which is
                # what this did — states that the symbol does not exist, on no evidence whatsoever.
                # For an agent deciding whether to create something, "I could not ask" and "it is
                # not there" are opposite answers.
                self._set_backend_missing(def_out)
                self._last_backend_error = def_out.describe()
                return None
            def_raw = def_out.value
            def_text = self._extract_text(def_raw)
            matches = self._loads(def_text)
            candidate_page_capped = bool(
                isinstance(matches, list) and (file_hint or qualified_hint) and len(matches) >= 500
            )
            if candidate_page_capped:
                matches = []
                def_text = (
                    f"> The language server returned the maximum 500 definitions for `{target}`; "
                    "the exact definition cannot be proven from a truncated candidate page."
                )
            if isinstance(matches, list) and file_hint and not candidate_page_capped:
                matches = [
                    match for match in matches
                    if isinstance(match, dict)
                    and _file_hint_matches(match.get("relative_path"), file_hint)
                ]
                if not matches:
                    # Do not fall through and render the original JSON containing definitions
                    # from other files. More importantly, the reference lookup below must remain
                    # "not asked", rather than turning an unresolved exact definition into a
                    # confident zero-reference answer.
                    def_text = (
                        f"> The language server found no definition for `{symbol_target}` in "
                        f"`{file_hint}`."
                    )
            if isinstance(matches, list) and qualified_hint and not candidate_page_capped:
                scored = [
                    (match, _qualified_definition_score(match, qualified_hint))
                    for match in matches if isinstance(match, dict)
                ]
                best = max((score for _, score in scored), default=0)
                # `_qualified_definition_score` admits a leaf-only symbol match only when every
                # remaining qualifier segment was proven against the definition's relative path.
                matches = [match for match, score in scored if score == best and score > 0]
                if not matches:
                    def_text = (
                        f"> The language server found no definition matching the qualified symbol "
                        f"`{target}`."
                    )
            if isinstance(matches, list) and matches and (file_hint or qualified_hint):
                identities = {
                    (str(match.get("relative_path") or ""),
                     str(match.get("name_path") or ""),
                     json.dumps(match.get("body_location"), sort_keys=True))
                    for match in matches
                }
                if len(identities) != 1:
                    matches = []
                    def_text = (
                        f"> The language server found multiple definitions matching `{target}`; "
                        "the file hint is not specific enough to request exact references."
                    )
            if isinstance(matches, list) and matches and (file_hint or qualified_hint):
                selected = matches[0]
                body_out = self._call_tool(
                    session,
                    "find_symbol",
                    {
                        "name_path_pattern": selected.get("name_path") or symbol_target,
                        "relative_path": selected.get("relative_path"),
                        "include_body": True,
                        "max_matches": 500,
                    },
                    remaining(),
                )
                if isinstance(body_out, Missing):
                    self._set_backend_missing(body_out)
                    self._last_backend_error = body_out.describe()
                    return None
                body_text = self._extract_text(body_out.value)
                body_matches = self._loads(body_text)
                if isinstance(body_matches, list) and body_matches:
                    exact_body_matches = [
                        match for match in body_matches
                        if isinstance(match, dict)
                        and match.get("name_path") == selected.get("name_path")
                        and match.get("relative_path") == selected.get("relative_path")
                        and (
                            selected.get("body_location") is None
                            or match.get("body_location") == selected.get("body_location")
                        )
                    ]
                    if exact_body_matches:
                        matches = exact_body_matches
                    else:
                        self._last_backend_error = (
                            "the exact definition body lookup returned no matching definition"
                        )
                        return None
                else:
                    self._last_backend_error = (
                        "the exact definition body lookup returned no usable definition list"
                    )
                    return None
            first: dict | None = None
            if isinstance(matches, list) and matches:
                def_section, first = self._format_matches(target, matches)
            elif def_text is None:
                # `_extract_text` returns None for an error result, and there is nothing to render
                # from a failure. Returning None here routes to a safe-null carrying a real reason
                # rather than dressing the failure up as "## Symbol: x" with the error underneath.
                return None
            else:
                # Non-JSON but not an error — surface what serena returned, which is how a
                # degenerate-but-real response still reaches the caller.
                def_section = f"## Symbol: {target}\n{def_text}"

            # References require the located symbol's own path (two-step contract).
            #
            # There is no longer a default "(none)" string here, and that is the point. This
            # section is rendered from an Outcome, so "the reference lookup did not answer" and
            # "this symbol has no references" cannot produce the same bytes. The old default
            # survived a timed-out call and asserted, with no evidence and no `reason`, that
            # nothing referenced the symbol — the permissive answer to the one question an agent
            # asks before deleting code.
            if not (first and first.get("relative_path")):
                unasked = Missing(
                    "not-asked",
                    "the symbol's file path was not resolved, so references were never requested",
                )
                self._add_gap("references", unasked)
                ref_section = f"## References — not retrieved\n> {unasked.describe()}."
                return f"{def_section}\n\n{ref_section}"

            ref_out = self._call_tool(
                session,
                "find_referencing_symbols",
                {
                    "name_path": first.get("name_path") or target,
                    "relative_path": first.get("relative_path"),
                },
                remaining(),
            )
            miss: Missing | None = None
            parsed: object = None
            if isinstance(ref_out, Missing):
                miss = ref_out
            else:
                ref_text = self._extract_text(ref_out.value)
                if ref_text is None:
                    # `_extract_text` returns None for an error payload — and it has already
                    # recorded the backend's own message. That is a failure, not an empty answer.
                    miss = Missing("backend-error",
                                   self._last_backend_error
                                   or "the language server returned an error for this lookup")
                else:
                    parsed = self._loads(ref_text)
                    if parsed is None:
                        miss = Missing("unparsable",
                                       "the reference list could not be read from the backend's reply")

            if miss is not None:
                self._add_gap("references", miss)
                retry = " Re-ask in a few seconds." if miss.retry_after_s else ""
                ref_section = f"## References — not retrieved\n> {miss.describe()}.{retry}"
            else:
                ref_lines = self._format_refs(parsed)
                if ref_lines:
                    ref_section = f"## References ({len(ref_lines)})\n" + "\n".join(ref_lines)
                else:
                    # Asked, answered, nothing came back — which is a real answer only when the
                    # backend was in a position to know. Where it was not, the emptiness carries no
                    # information and must not be rendered as though it did; see `outcome.py`.
                    unsound = self._empty_references_unsound(
                        root, str(first.get("relative_path") or ""))
                    if unsound is not None:
                        self._add_gap("references", unsound)
                        ref_section = (f"## References — not retrieved\n> {unsound.describe()}.")
                    else:
                        # Distinct wording from the branch above so the two states are
                        # distinguishable in the body text as well as in `gaps`.
                        ref_section = ("## References (0)\n"
                                       "(the language server reports no references to this symbol)")

            return f"{def_section}\n\n{ref_section}"
        except Exception as exc:
            log_swallowed("LspProvider._op_symbol", exc)
            return None

    def _op_overview(
        self, session: _LspSession, target: str, root: str, timeout_s: float
    ) -> str | None:
        try:
            out = self._call_tool(
                session,
                "get_symbols_overview",
                {"relative_path": target or ""},
                timeout_s,
            )
            if isinstance(out, Missing):
                self._last_backend_error = out.describe()
                return None
            text = self._extract_text(out.value)
            if not text:
                return None
            parsed = self._loads(text)
            if isinstance(parsed, dict):
                parts = [f"## Overview: {target}"]
                for kind, names in parsed.items():
                    if isinstance(names, list):
                        parts.append(f"**{kind}**: " + ", ".join(str(n) for n in names))
                    else:
                        parts.append(f"**{kind}**: {names}")
                return "\n".join(parts)
            return text
        except Exception:
            return None
