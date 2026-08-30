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

# Serena ships as the `serena-agent` package (executable name `serena`); `uvx serena` does NOT
# work ("Package `serena` does not provide any executables"). The working invocation — verified
# against the installed serena and the machine's own serena MCP config — pulls it straight from
# the upstream repo and starts the stdio MCP server, binding the project via `--project`.
_SERENA_GIT = "git+https://github.com/oraios/serena"


# Prefixes serena uses when a tool call fails. Checked in addition to the MCP `isError` flag,
# which is not set by every server or version — and the cost of missing one is that a failure is
# served to an agent as an answer.
_BACKEND_ERROR_MARKERS = (
    "error executing tool",
    "exception:",
    "traceback (most recent call last)",
    "the language server manager is not initialized",
)


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


class _State(enum.Enum):
    WARMING = "WARMING"
    READY = "READY"
    FAILED = "FAILED"


class _LspSession:
    def __init__(self, project_root: str, cmd: str) -> None:
        self.state = _State.WARMING
        self.cooldown_until: float = 0.0
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._mcp_session: ClientSession | None = None
        self._thread = threading.Thread(
            target=self._run,
            args=(project_root, cmd),
            daemon=True,
        )
        self._thread.start()

    def _run(self, project_root: str, cmd: str) -> None:
        try:
            self._loop.run_until_complete(self._warmup(project_root, cmd))
        except Exception:
            with self._lock:
                self.state = _State.FAILED
                self.cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
        finally:
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

    # Class-level default so a provider built via `__new__` (the test stubs do this) still has it
    # rather than raising AttributeError inside the never-raise handler.
    _last_backend_error: str | None = None
    # Sections of the current answer that are known to be short of an answer. An op appends here
    # instead of quietly rendering an empty section, and `build_result` turns them into the
    # envelope's `gaps` / `confidence`. Class-level default for the same __new__ reason as above.
    _pending_gaps: tuple[dict[str, Any], ...] = ()

    def __init__(self) -> None:
        self._sessions: dict[str, _LspSession] = {}
        self._sessions_lock = threading.Lock()
        self._last_backend_error = None
        self._pending_gaps = ()
        self._detect_backend()

    def _clear_backend_error(self) -> None:
        self._last_backend_error = None
        self._pending_gaps = ()

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
            existing = self._sessions.get(root)
            if existing is not None:
                with existing._lock:
                    if existing.state == _State.FAILED:
                        if time.monotonic() > existing.cooldown_until:
                            del self._sessions[root]  # cooldown elapsed → allow one respawn
                        else:
                            return existing  # still cooling down — no per-request respawn
                    else:
                        return existing
            session = _LspSession(root, self._cmd)  # type: ignore[arg-type]
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

    def probe(self, project_root: str, deep: bool = False, timeout_s: float = 20.0) -> dict:
        """Never-raise health check for the doctor. Shallow (default) is FREE — PATH presence
        plus any existing session's live state. Deep boots serena and polls until READY/FAILED,
        bounded by ``timeout_s`` (first boot pulls serena via uvx and is slow). ``repo_indexed``
        is always None: serena keeps no persistent index, it warms per-root on demand."""
        if not self.available:
            return {
                "installed": False, "runnable": False, "repo_indexed": None,
                "detail": "neither `serena` nor `uvx` found on PATH",
                "remediation": "install uv (provides uvx): `codeintel setup --install-uv` "
                               "(or `brew install uv` / `pip install uv`) — serena is then "
                               "fetched on first use",
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
                unserved = self._unserved_note(project_root)
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
        session = self._get_or_create_session(project_root)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with session._lock:
                st = session.state
            if st == _State.READY:
                # READY is a fact about the PROCESS. Whether it will answer for this repo's code is
                # a separate question, and the one the caller is actually asking.
                unserved = self._unserved_note(project_root)
                return {"installed": True, "runnable": unserved is None, "repo_indexed": None,
                        "detail": f"serena booted via `{cmd}` and reached READY" + (
                            unserved[0] if unserved else ""),
                        "remediation": unserved[1] if unserved else None}
            if st == _State.FAILED:
                return {"installed": True, "runnable": False, "repo_indexed": None,
                        "detail": "serena failed to boot",
                        "remediation": "check uvx + network: `uvx --from "
                                       "git+https://github.com/oraios/serena serena start-mcp-server`"}
            time.sleep(0.5)
        return {"installed": True, "runnable": None, "repo_indexed": None,
                "detail": f"serena did not reach READY within {int(timeout_s)}s (still warming)",
                "remediation": "retry — first boot pulls serena via uvx and can be slow"}

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
                return safe_null_result(op_str, target_str, engine="lsp", reason="engine-unavailable")

            try:
                budget_ms = int(budget) if budget else 0
            except Exception:
                budget_ms = 0
            timeout_s = (budget_ms / 1000) if budget_ms > 0 else _DEFAULT_TIMEOUT_S

            session = self._get_or_create_session(root_str)

            with session._lock:
                state = session.state

            if state == _State.WARMING:
                return safe_null_result(op_str, target_str, engine="lsp", reason="warming")

            if state == _State.FAILED:
                return safe_null_result(op_str, target_str, engine="lsp", reason="boot-failed")

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
                if self._last_backend_error:
                    return safe_null_result(
                        op_str, target_str, engine="lsp", reason="backend-error",
                        hint=f"{self._last_backend_error} — run `codeintel doctor --deep` to boot-"
                             f"check serena; the full backend message is in the server log",
                    )
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
            def_out = self._call_tool(
                session,
                "find_symbol",
                {"name_path_pattern": target, "include_body": True, "max_matches": 5},
                timeout_s,
            )
            if isinstance(def_out, Missing):
                # The tool call itself failed or timed out. Rendering "(not found)" here — which is
                # what this did — states that the symbol does not exist, on no evidence whatsoever.
                # For an agent deciding whether to create something, "I could not ask" and "it is
                # not there" are opposite answers.
                self._last_backend_error = def_out.describe()
                return None
            def_raw = def_out.value
            def_text = self._extract_text(def_raw)
            matches = self._loads(def_text)

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
                timeout_s,
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
                    # Asked, answered, genuinely nothing. Distinct wording from the branch above
                    # so the two states are distinguishable in the body text as well as in `gaps`.
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
