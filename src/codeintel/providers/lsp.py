from __future__ import annotations

import asyncio
import enum
import json
import re
import shutil
import threading
import time
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from codeintel.provider import Result, safe_null_result

_COOLDOWN_SECONDS = 60
_DEFAULT_TIMEOUT_S = 5.0

# Serena ships as the `serena-agent` package (executable name `serena`); `uvx serena` does NOT
# work ("Package `serena` does not provide any executables"). The working invocation — verified
# against the installed serena and the machine's own serena MCP config — pulls it straight from
# the upstream repo and starts the stdio MCP server, binding the project via `--project`.
_SERENA_GIT = "git+https://github.com/oraios/serena"


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
        self._mcp_session: Optional[ClientSession] = None
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
        async with stdio_client(
            StdioServerParameters(command=launch_args[0], args=launch_args[1:])
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                with self._lock:
                    self._mcp_session = session
                    self.state = _State.READY
                # Keep the loop (and the subprocess/session it owns) alive so _call_tool can
                # schedule coroutines onto it. Resolves only when the thread/loop is torn down.
                await asyncio.get_running_loop().create_future()


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

    def __init__(self) -> None:
        self._sessions: dict[str, _LspSession] = {}
        self._sessions_lock = threading.Lock()
        self._detect_backend()

    def _detect_backend(self) -> None:
        # Prefer a directly-installed serena; otherwise drive it through uvx.
        if shutil.which("serena"):
            self.available = True
            self._cmd: Optional[str] = "serena"
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
            result_text = self._dispatch(session, op_str, target_str, root_str, timeout_s)
            if result_text is None:
                return safe_null_result(op_str, target_str, engine="lsp", reason="unsupported-op")

            return {
                "ok": True,
                "op": op_str,
                "target": target_str,
                "result": result_text,
                "engine": "lsp",
                "cached": False,
            }
        except Exception:
            return safe_null_result(op, target, engine="lsp", reason="error")

    def _dispatch(
        self,
        session: _LspSession,
        op: str,
        target: str,
        root: str,
        timeout_s: float,
    ) -> Optional[str]:
        if op == "symbol":
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
    ) -> Optional[Any]:
        try:
            mcp_session = session._mcp_session
            if mcp_session is None:
                return None
            coro = mcp_session.call_tool(tool, args)
            future = asyncio.run_coroutine_threadsafe(coro, session._loop)
            return future.result(timeout=timeout_s)
        except Exception:
            return None

    def _extract_text(self, raw: Any) -> Optional[str]:
        if raw is None:
            return None
        if isinstance(raw, str):
            return raw
        # mcp CallToolResult has a .content list of TextContent
        try:
            parts = []
            for item in raw.content:
                if hasattr(item, "text"):
                    parts.append(item.text)
            return "\n".join(parts) if parts else None
        except Exception:
            return None

    @staticmethod
    def _loads(text: Optional[str]) -> Any:
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _ref_line(content: Any) -> Optional[str]:
        """Pull the referenced line number out of serena's `content_around_reference` blob,
        which marks the reference line with a leading `>` (e.g. `  >   7:from ...`)."""
        if not isinstance(content, str):
            return None
        m = re.search(r">\s*(\d+):", content)
        return m.group(1) if m else None

    def _format_matches(self, target: str, matches: list) -> tuple[str, Optional[dict]]:
        parts = [f"## Symbol: {target}"]
        first: Optional[dict] = None
        for m in matches:
            if not isinstance(m, dict):
                continue
            if first is None:
                first = m
            kind = m.get("kind") or "symbol"
            rel = m.get("relative_path") or "?"
            loc = m.get("body_location") if isinstance(m.get("body_location"), dict) else {}
            s, e = loc.get("start_line"), loc.get("end_line")
            span = f":{s}-{e}" if s is not None else ""
            parts.append(f"**{kind}** — {rel}{span}")
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
            for _kind, entries in kinds.items():
                if not isinstance(entries, list):
                    continue
                for ent in entries:
                    if not isinstance(ent, dict):
                        continue
                    np = str(ent.get("name_path") or "").strip()
                    line = self._ref_line(ent.get("content_around_reference"))
                    loc = f"{file}:{line}" if line else str(file)
                    suffix = f"  ({np})" if np else ""
                    lines.append(f"- {loc}{suffix}")
                    if len(lines) >= 50:
                        return lines
        return lines

    def _op_symbol(
        self, session: _LspSession, target: str, root: str, timeout_s: float
    ) -> Optional[str]:
        try:
            def_raw = self._call_tool(
                session,
                "find_symbol",
                {"name_path_pattern": target, "include_body": True, "max_matches": 5},
                timeout_s,
            )
            def_text = self._extract_text(def_raw)
            matches = self._loads(def_text)

            first: Optional[dict] = None
            if isinstance(matches, list) and matches:
                def_section, first = self._format_matches(target, matches)
            else:
                # Non-JSON / degenerate response — surface whatever serena returned.
                def_section = f"## Symbol: {target}\n{def_text or '(not found)'}"

            # References require the located symbol's own path (two-step contract).
            ref_section = "## References\n(none)"
            if first and first.get("relative_path"):
                ref_raw = self._call_tool(
                    session,
                    "find_referencing_symbols",
                    {
                        "name_path": first.get("name_path") or target,
                        "relative_path": first.get("relative_path"),
                    },
                    timeout_s,
                )
                ref_lines = self._format_refs(self._loads(self._extract_text(ref_raw)))
                if ref_lines:
                    ref_section = f"## References ({len(ref_lines)})\n" + "\n".join(ref_lines)

            return f"{def_section}\n\n{ref_section}"
        except Exception:
            return None

    def _op_overview(
        self, session: _LspSession, target: str, root: str, timeout_s: float
    ) -> Optional[str]:
        try:
            raw = self._call_tool(
                session,
                "get_symbols_overview",
                {"relative_path": target or ""},
                timeout_s,
            )
            text = self._extract_text(raw)
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
