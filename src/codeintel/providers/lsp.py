from __future__ import annotations

import asyncio
import enum
import shutil
import threading
import time
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from codeintel.provider import Result, safe_null_result

_COOLDOWN_SECONDS = 60
_DEFAULT_TIMEOUT_S = 5.0


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
            self._loop.close()

    async def _warmup(self, project_root: str, cmd: str) -> None:
        if cmd == "uvx":
            launch_args = ["uvx", "serena", "--project_root", project_root]
        else:
            launch_args = [cmd, "--project_root", project_root]

        async with stdio_client(
            __import__("mcp").StdioServerParameters(
                command=launch_args[0],
                args=launch_args[1:],
            )
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                with self._lock:
                    self._mcp_session = session
                    self.state = _State.READY
                # Keep the loop alive while the session is used externally.
                # The loop ends when the thread exits or an error occurs.
                await asyncio.get_event_loop().create_future()


class LspProvider:
    """Wraps the LSP-over-MCP bridge (serena). Never raises."""

    def __init__(self) -> None:
        self._sessions: dict[str, _LspSession] = {}
        self._sessions_lock = threading.Lock()
        self._detect_backend()

    def _detect_backend(self) -> None:
        if shutil.which("uvx"):
            self.available = True
            self._cmd: Optional[str] = "uvx"
        elif shutil.which("serena"):
            self.available = True
            self._cmd = "serena"
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
                            del self._sessions[root]
                        else:
                            return existing
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

    def _op_symbol(
        self, session: _LspSession, target: str, root: str, timeout_s: float
    ) -> Optional[str]:
        try:
            def_raw = self._call_tool(
                session,
                "find_symbol",
                {"name": target, "project_root": root},
                timeout_s,
            )
            ref_raw = self._call_tool(
                session,
                "find_referencing_symbols",
                {"name": target, "project_root": root},
                timeout_s,
            )
            def_text = self._extract_text(def_raw) or "(not found)"
            ref_text = self._extract_text(ref_raw) or "(none)"
            return f"## Symbol: {target}\n{def_text}\n\n## References\n{ref_text}"
        except Exception:
            return None

    def _op_overview(
        self, session: _LspSession, target: str, root: str, timeout_s: float
    ) -> Optional[str]:
        try:
            raw = self._call_tool(
                session,
                "get_symbols_overview",
                {"relative_path": target or "", "project_root": root},
                timeout_s,
            )
            return self._extract_text(raw)
        except Exception:
            return None
