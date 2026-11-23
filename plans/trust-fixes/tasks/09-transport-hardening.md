# Harden the transports: /code/status 403, root-not-found, redact leading boundary, and the MCP wire-byte bound

Type: task
Status: open
Blocked by: 08
Test first: yes
Level: 3
Touches: src/codeintel/transports/http.py, src/codeintel/transports/mcp.py, src/codeintel/core/redact.py, src/codeintel/core/provider.py, tests/test_http_server.py, tests/test_http_auth.py, tests/test_rbac.py, tests/test_redaction_boundary.py, tests/test_mcp_server.py, tests/test_mcp_answer_size.py

This ticket fixes T7: `status-root-denied-200-fake-fallback`, `nonexistent-root-says-not-indexed` and `redact-mid-path`. It also fixes the T10 finding `mcp-budget-measured-on-wrong-bytes`. The fourth T7 finding, the backend-error leak, is fixed in the engine by the agent-facing hints ticket.

1. **`GET /code/status` for a root outside the role.**
   - `_code_status_handler_inner` returns `_STATUS_FALLBACK` plus `reason: "root-not-allowed-for-role"` when `gw.allows_root` denies.
   - `do_GET` in `src/codeintel/transports/http.py` sends 403 when the reason is in `_RBAC_DENIED_REASONS`, as `do_POST` does.
   - A bare `/code/status` with no `project_root` under a role with restricted roots is denied the same way.
   - An allowed root still gets the real readiness.
2. **Root-not-found.**
   - In `code_query_handler`, after the RBAC check and the cwd fallback: a non-empty `project_root` that is not a directory returns `safe_null_result(op, target, reason="root-not-found", hint="<root> does not exist; pass the absolute repo root, or omit it to use the server cwd")` before `gw.query`.
   - That also stops `search` from claiming "first-time indexing started".
   - Add `root-not-found` to the `unavailable_reasons` set in `core/provider.safe_null_result`.
3. **`src/codeintel/core/redact.py` leading boundary.** `_bounded` gets the negative lookbehind the fix plan names, `(?<![\w.~/-])`, before `re.escape(needle)` on the slashed needles. The needle then matches only where it starts a path token: at the start of the text, or after whitespace, a quote, a backtick, `(`, `=`, `:` or `,`.
   - `/srv/root/app`, `/proc/self/root/x` and `scratchpad/root/app` are left alone; `/root/x`, `"/root/x"` and `` `/root/x` `` still become `~/x`.
   - `/private{home}` stays its own needle, so `/private/root/x/y` is still redacted (`tests/test_incompleteness.py::test_redaction_survives_the_private_prefix`, unchanged).
   - `contains_home_path` uses the same `_bounded`, so the detector and the redactor keep agreeing (`test_invariant_the_detector_and_the_redactor_agree`, unchanged).
   - Keep the flattened form's boundary rules as they are.
   - One existing test pins the opposite rule on purpose and changes with it: `tests/test_redaction_boundary.py::test_the_boundary_is_deliberately_asymmetric` asserts `https://example.com/root/api` becomes `https://example.com~/api` under HOME=/root, and its docstring argues against a head guard. The finding `redact-mid-path` reverses that decision, so the test now asserts the URL is unchanged, and its docstring states the trade honestly: the home string in the middle of a longer path or URL is no longer redacted, in exchange for never corrupting a real path such as `/srv/root/app` in an actionable hint, which the finding judged the worse failure. Rewrite `src/codeintel/core/redact.py`'s "guarded on the tail" comments to match.
4. **MCP wire bytes.** `_size` in `src/codeintel/transports/mcp.py` measures the serialization FastMCP sends for a dict result: the `TextContent` text, which is pretty-printed. Compute it with the same call the installed `mcp` package makes, `pydantic_core.to_json(obj, fallback=str, indent=2)` (`.venv/lib/python3.13/site-packages/mcp/server/mcpserver/utilities/func_metadata.py`, `_convert_to_content`), and pin it in the test by running `_convert_to_content` itself on the compacted envelope.

## Not in this ticket

- Stripping project ids from backend-error hints, and mapping "project not found" to `project-not-indexed`, belongs to the agent-facing hints ticket (engine side, so every transport is covered).
- Replacing backend stderr with a fixed message and a request id on HTTP is dropped (see the plan reply): the engine-side strip removes the leak.

## Done when

- `tests/test_http_auth.py` or `tests/test_rbac.py`: GET `/code/status?project_root=<outside>` gives 403 with `root-not-allowed-for-role`. Fails on main.
- `tests/test_mcp_server.py`: `code_query_handler({"op": "callers", "target": "main", "project_root": "/nonexistent/dir"})` gives reason `root-not-found` and outcome `unavailable`, and `gw.query` is never called. Fails on main.
- `tests/test_redaction_boundary.py` gains the three mid-path cases under HOME=/root and keeps every other existing case; only `test_the_boundary_is_deliberately_asymmetric` changes, as step 3 says. Fails on main. `tests/test_incompleteness.py`, `tests/test_backend_failure_detail.py` and `tests/test_missing_target.py`, which also redact home paths (after a backtick, a space and inside hints), pass unchanged.
- `tests/test_mcp_answer_size.py`: for an envelope with many rows, the real FastMCP-serialized text is at most 40,000 bytes. Fails on main.
- `.venv/bin/pytest tests/test_http_server.py tests/test_http_auth.py tests/test_rbac.py tests/test_redaction_boundary.py tests/test_mcp_server.py tests/test_mcp_answer_size.py tests/test_incompleteness.py tests/test_backend_failure_detail.py tests/test_missing_target.py tests/test_doctor.py tests/test_summary_integrity.py tests/test_never_raise.py -q -o addopts= --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read` passes, and all existing tests still pass. `tests/test_doctor.py` (which reads `_STATUS_FALLBACK`), `tests/test_summary_integrity.py` (`mcp._code_status_handler_inner` and `mcp.compact_for_mcp` are registered) and `tests/test_never_raise.py` (`code_query_handler` on a real `tmp_path`) are not on Touches and pass unchanged.

```check
$ .venv/bin/pytest tests/test_http_server.py tests/test_http_auth.py tests/test_rbac.py tests/test_redaction_boundary.py tests/test_mcp_server.py tests/test_mcp_answer_size.py tests/test_incompleteness.py tests/test_backend_failure_detail.py tests/test_missing_target.py tests/test_doctor.py tests/test_summary_integrity.py tests/test_never_raise.py -q -o addopts= --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read
prints passed
```

## Reference

- spec.md, the story "An operator running the HTTP server with RBAC", and Decisions ("The MCP 40 KB bound")
- redblue-fixplan.md § T7, and § T10 first bullet

## Answer
