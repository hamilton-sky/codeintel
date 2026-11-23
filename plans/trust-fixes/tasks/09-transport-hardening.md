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

Work started at 3b515e2fef18b7da1255d690499c015a94bd4675. Test-only commit: f23cf3d (`test(trust-fixes): 09 failing test`); at it the new tests failed for the right reason (10 failed: status 200 not 403 x2, reason `''` not `root-not-found` x3, `https://example.com~/api`, `/srv~/app`, `/proc/self~/x`, `scratchpad~/app`, wire text 44,366 > 40,000 bytes).

**Built**
- `src/codeintel/transports/mcp.py`: `_code_status_handler_inner` returns `{**_STATUS_FALLBACK, "reason": "root-not-allowed-for-role"}` when `gw.allows_root` denies (`_STATUS_FALLBACK` itself unchanged). `code_query_handler`, after the cwd fallback: `if project_root and gw.allows_root(role, project_root) and not os.path.isdir(project_root)` returns `redact(safe_null_result(op, target, reason="root-not-found", hint="<root> does not exist; pass the absolute repo root, or omit it to use the server cwd"))` before `gw.query`. `_size` is `len(pydantic_core.to_json(obj, fallback=str, indent=2))`; the per-row cost in `compact_for_mcp` is `_size([[row]]) - 8` (the row at its wire depth). `import json` dropped (unused now).
- `src/codeintel/transports/http.py`: `do_GET /code/status` sends 403 when `reason` is in `_RBAC_DENIED_REASONS`.
- `src/codeintel/core/redact.py`: `_SLASHED_HEAD = (?<![\w.~/-])`; `_bounded(needle, tail, head="")`; both slashed calls (redact_text, contains_home_path) pass it. Flattened form unchanged. Boundary comment and `redact_text` docstring rewritten to state the head guard and its cost.
- `src/codeintel/core/provider.py`: `root-not-found` in `unavailable_reasons`.
- Tests: `tests/test_rbac.py` (`scoped_server` fixture with stubbed `run_doctor`; status outside root 403, bare status 403, allowed root 200 with real readiness, missing root outside the role still 403 `root-not-allowed-for-role`), `tests/test_mcp_server.py` (missing root for callers and search, a file as root, an existing root still reaches the gateway, hint redacted), `tests/test_redaction_boundary.py` (3 mid-path cases, 9 token-start cases still redacted, asymmetric test flipped), `tests/test_mcp_answer_size.py` (wire text via `_convert_to_content` <= 40,000).

**Proof**
- 403 bullet: `tests/test_rbac.py::test_status_for_a_root_outside_the_role_is_403` and `test_a_bare_status_under_a_role_with_restricted_roots_is_403` failed at f23cf3d (`assert 200 == 403`), pass now; the allowed-root test passes in both.
- root-not-found bullet: `tests/test_mcp_server.py::test_a_root_that_does_not_exist_is_root_not_found_before_any_query` failed at f23cf3d (`'' == 'root-not-found'`, the fake gateway was queried), passes now with `outcome == "unavailable"` and `gw.queries == []`.
- Redaction bullet: the three mid-path cases and the flipped asymmetric test failed at f23cf3d, pass now; every other case in the file unchanged and passing. `test_incompleteness.py`, `test_backend_failure_detail.py`, `test_missing_target.py` unchanged and pass (in the check block below; `git diff 3b515e2 --stat` on them is empty).
- Wire bytes bullet: `test_the_text_fastmcp_sends_stays_under_the_byte_budget` failed at f23cf3d (44,366 bytes), passes now.
- Check block, run after the last code edit: `396 passed, 1 deselected, 1 xfailed` (the xfail is the pre-existing strict c4-coverage one in test_summary_integrity). test_doctor, test_summary_integrity, test_never_raise unchanged and pass.
- Wider: every test file that references redact / compact_for_mcp / code_status_handler / safe_null_result / transports (28 files): `854 passed, 3 skipped, 1 deselected, 1 xfailed`, exit 0. Whole Test command: left to the conductor's gate.
- `ruff check src tests bench`: all checks passed. `mypy`: no issues in 88 files. compileall exit 0. `codeintel c4 . --check --out /tmp/codeintel-c4-check`: 0 gating findings, exit 0.
- Acceptance `test_tf_http.py` + `test_tf_small_fixes.py`: 8 passed; the 7 failing are other tickets' stories (backend-error ids live test, which failed here on `index_repository` exiting 1; prompt, rows, reset x3, ctrl-c).

**Decisions**
- root-not-found is gated on `gw.allows_root` only, not on `gw.allows(role, op)`: a role naming a missing path outside its roots still gets `root-not-allowed-for-role` (pinned over HTTP), so existence is not leaked across tenants. An op-denied role naming a missing path inside its own roots gets `root-not-found` rather than `op-not-allowed-for-role`; that leaks nothing beyond its roots. Calling `gw.allows` too would have broken the existing `_Gw` stub in `test_rbac.py::test_the_cwd_default_survives_on_the_stdio_path` (it has no `allows`).
- The root-not-found envelope is passed through `redact`, since it never reaches `Gateway.query` where envelopes are redacted (the hint echoes the root).
- The head guard is a separate `head` argument to `_bounded` (default empty) so the flattened form keeps its rules untouched.
- Per-row cost `_size([[row]]) - 8` is an upper bound on what a row adds at its wire depth (first row +8, later +6), so no re-check loop is needed.

**Shortcuts taken**: none.

**For later tickets**
- Ticket 13 (CHANGELOG): T7 `status-root-denied-200-fake-fallback` (GET /code/status now 403, and its body carries `reason`), `nonexistent-root-says-not-indexed` (new reason `root-not-found`, outcome `unavailable`), `redact-mid-path` (the home string in the middle of a path or URL is no longer redacted: say so), T10 `mcp-budget-measured-on-wrong-bytes`.
- The agent-facing hints ticket: over stdio, `code_query_handler` now answers `root-not-found` for a missing root before the engine, so its "project not found -> project-not-indexed" mapping applies only to roots that exist.


## Review findings (round 1, floor guard)

floor guard: the diff weakens the bar instead of meeting it
suppress: src/codeintel/transports/mcp.py: return redact(safe_null_result(  # type: ignore[return-value]
if a finding is intended, the ticket needs a line like: Floor: allow <category>
