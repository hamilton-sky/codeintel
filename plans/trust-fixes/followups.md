# Follow-ups: trust-fixes

What the run left for later: minor findings of passing reviews, the findings a level 2 ticket passed with after 2 review rounds, and Done when checks this host could not pass. Each is a candidate for a cleanup ticket.

## 01-index-age-for-changed (left for later)

- 1. minor /home/user/codeintel/src/codeintel/engines/graph/changed_range.py: `uncommitted_paths` skips a rename's source entry only when the X column is R or C (`status[:1]`). A work-tree rename (` R`, from `git add -N` after a move) also puts the source path in the next -z entry. That entry is then parsed as a status line. I reproduced it in a scratch repo: `mv old_name.py new_name.py; git add -N new_name.py` gives `changed = [.../new_name.py, .../_name.py]`, which includes a file that does not exist. The "git lists N changed source file(s)" count is then one too high, and the bogus path can be named in a gap. Freshness is never under-reported. Fix: skip the next entry when either column is R or C (`b"R" in status or b"C" in status`), and add a test for ` R`. (pair review)
- 2. minor /home/user/codeintel/src/codeintel/engines/graph/changed_range.py: in `graph_index_age`, when `git log` cannot run at all (code < 0: timeout or git gone), the follow-up `rev-parse --verify HEAD` also fails. That reads as "no commit yet", so HEAD's time is skipped without any signal rather than answering `unknown`. This is unlikely, since `in_work_tree` and `status` succeeded just before, but it breaks the "cannot tell is unknown" rule. Fix: treat `code < 0` from either call as `unknown`. (pair review)
- 3. minor /home/user/codeintel/src/codeintel/engines/graph/ops.py: `in_work_tree` returns False when git is missing, cannot run or times out. Uncommitted `changed` then reports `not-a-git-repo` and suggests `git init`, while range mode tells these cases apart (`git-unavailable`, `timeout`). The ticket asked for this behaviour, so it may be deliberate. Fix: reuse range mode's mapping of `_git`'s negative codes. (pair review)

## 03-gateway-freshness (left for later)

- 1. minor src/codeintel/gateway/reindexer.py: a root that cannot be fingerprinted (not a git repo, over 50,000 listed files, git failing) still submits a pass every debounce window, and the gateway flags `reindexing` on an unchanged tree. Fix: document it in the README "Keeping answers fresh" section, or skip the flag when the submit came from the fallback path. (pair review)
- 2. minor src/codeintel/gateway/reindexer.py: in `_tree_fingerprint`, `os.stat` follows symlinks; a tracked symlink into a hung network mount can block the request thread. Fix: use `os.lstat`. (pair review)
- 3. minor src/codeintel/gateway/reindexer.py: a semantic pass that fails with a -1 return counts as completed and is not retried on an unchanged tree. Fix: say so in the `_do_reindex` docstring, which still reads "A persistent failure retries every debounce window". (pair review)

## 08-no-writes-through-symlinks (left for later)

- 1. minor src/codeintel/core/containment.py: `_why_not_a_dir` (and the Windows branch of `_write_walk`) reports `dangling` when a directory on the resolved path is a regular file (e.g. `AGENTS.md -> README.md/x`, ENOTDIR). Refusal correct, reason wrong. Fix: return `not-a-file` when the component exists and is not a directory. (pair review)
- 2. minor src/codeintel/core/containment.py: in `_write_walk`, if `os.fdopen(fd, ...)` raises, the descriptor from `os.open(tmp, ...)` is never closed. Fix: close `fd` in the except path when `fdopen` was not reached. (pair review)

## 09-transport-hardening (left for later)

- 1. minor src/codeintel/core/redact.py: the leading guard `(?<![\w.~/-])` also lets the home path through where it is a real path: with HOME=/home/alice, `file:///home/alice/x`, `-I/home/alice/inc`, `//home/alice/x` and `/var/home/alice/x` come back unchanged and `contains_home_path` agrees. `redact_text` also runs on backend stderr. Not a live leak today. Consider adding `realpath(home)` as a needle like `/private{home}`, and record the `-I`/`file://` gap next to the stated trade. (pair review)
- 2. minor src/codeintel/transports/mcp.py: the `root-not-found` hint repeats the caller's `project_root` unescaped and unbounded (only to a caller whose role allows that root). Consider truncating the echoed root. (pair review)

## 10-semantic-failure-handling (left for later)

- 1. minor src/codeintel/engines/semantic/provider.py: `_is_model_load_failure` matches the text "could not load embedding model " that `EmbeddingModelUnavailable` builds in src/codeintel/engines/semantic/db.py:102; rewording that message silently stops the record and no test would catch it. Fix: keep the exception type on the indexer, or define the prefix once in db.py and import it. (pair review)

## 11-agent-facing-hints (left for later)

- 1. minor src/codeintel/engines/graph/backend.py: `_SEARCH_ADVICE` is quadratic on repeated unclosed `search_graph(name_pattern="` text and `_agent_facing` runs on the whole stderr before the 400-char cap (1.2s at 40 KB, 28.6s at 200 KB, ~594s at 1 MB). No caller path found to trigger it on 0.10.8. Fix: cut the text to a bounded length (e.g. last 4x`_STDERR_CAP`) before the rewrites, or bound the lazy group (`[^"]{0,200}?`). (pair review)
- 2. minor src/codeintel/engines/graph/backend.py: an unquoted id in a list with no `project` keyword before it (`available: Users-alice-repo, home-bob-x`) and two-part ids (`"home-bob"`) pass through. Not a live leak on 0.10.8. Fix: when `is_project_not_found` matches, drop everything after the error phrase. (pair review)
- 3. minor src/codeintel/engines/graph/backend.py: the `_TOOL_NAMES` rewrite also hits the backend's deprecation warning, producing `cli code.query op=pattern --help`, which does not exist. Fix: drop `warning: passing raw JSON ...` lines with `_LOG_NOISE`, or leave `cli <tool>` forms alone. (pair review)
- 4. minor src/codeintel/engines/graph/provider.py: did-you-mean lists symbol names to any role that may run `callers`, even without `pattern` (own project only). Fix: skip suggestions when the role lacks `pattern`, or document it. (pair review)

## 12-small-cli-fixes (left for later)

- 1. minor src/codeintel/engines/graph/render.py: the new last-segment rule protects fewer document headings than the old "any space means leave it" rule. `_strip_project_prefix("Pre-1.0 notes for 2.x", may_be_filename=False)` returns `'0 notes for 2.x'`. Display-only regression. Fix: also refuse to strip when any segment after the head starts or ends with a space, or `rest` starts with a digit then a space; pin it in tests/test_render_names.py. (pair review)
- 2. minor src/codeintel/engines/semantic/db.py:484: the model-switch fix still says `codeintel reset --all`, which also wipes every graph index. Fix: `codeintel reset --all --semantic-only` (follow-up; not in the ticket's list). (pair review)
- 3. minor src/codeintel/setup/agent_prompt.py: `model_failed` also fires on `reason == "index-failed"`, so the model-download text would show for any index failure. Doctor rows don't produce `index-failed` today. Fix: key on `model-load-failed` only, or check `last_error`. (pair review)

## feature review (left for later)

- 1. minor src/codeintel/engines/graph/backend.py (ticket 11): `_SEARCH_ADVICE` is quadratic and `_agent_facing` runs on the whole stderr before `_STDERR_CAP` (108 KB takes 8.1 s). Fix: bound the text before `_agent_facing`, or use `[^"\\]*` instead of `.*?`. (feature review)
- 2. minor src/codeintel/engines/graph/backend.py (ticket 11): ids still pass in `available projects: home-alice-work, home-bob-repo-x` (only the first withheld) and `candidates [home-alice-work-repo, srv-bob-x-y]`; `cli search_graph is deprecated` becomes a nonexistent `cli code.query op=pattern`. Fix: withhold every slug-shaped id in comma lists after `projects?` and inside `[...]`; leave tool names alone in deprecation lines or drop them. (feature review)
- 3. minor src/codeintel/engines/graph/provider.py `_did_you_mean` (ticket 11): lists close names to a role that may not run `pattern`. Fix: offer only when the role may run `pattern`, or record it as accepted. (feature review)
- 4. minor src/codeintel/core/redact.py (ticket 09): `file:///home/x/a`, `-I/home/x/inc`, `//home/x/a`, `/var/home/x/a` pass unredacted. Fix: allow `file://`, doubled `/`, `-X` flag prefixes in `_SLASHED_HEAD`; also redact `realpath(home)`. (feature review)
- 5. minor src/codeintel/transports/mcp.py:200 (ticket 09): `root-not-found` echoes project_root unbounded and says "does not exist" for a regular file. Fix: cut to ~200 chars and say "is not a directory". (feature review)
- 6. minor src/codeintel/core/containment.py (ticket 08): `_why_not_a_dir` returns dangling on ENOTDIR; `fd` leaks if `os.fdopen` raises in `_write_walk`. Fix both. (feature review)
- 7. minor src/codeintel/engines/semantic/provider.py:151 (ticket 10): `_is_model_load_failure` matches text built in semantic/db.py:102. Fix: export the prefix from db.py or record the failure type. (feature review)
- 8. minor src/codeintel/engines/graph/render.py `_strip_project_prefix` (ticket 12): `Pre-1.0 notes for 2.x` renders as `0 notes for 2.x` (regression). Fix and add the test. (feature review)
- 9. minor src/codeintel/engines/semantic/db.py:484: dimension-mismatch warning says `codeintel reset --all` (wipes graph). Fix: `--all --semantic-only`. (feature review)
- 10. minor src/codeintel/setup/agent_prompt.py:59 (ticket 12): `model_failed` also matches `index-failed`. Fix: match only `model-load-failed`. (feature review)
- 11. minor src/codeintel/engines/graph/provider.py `_stamp_staleness` (ticket 02): the verdict re-reads the db mtime after dispatch, so a reindex finishing during dispatch marks old rows fresh. Fix: compare the pre-dispatch `written` with the post-dispatch mtime inside `_stamp_staleness` and treat a change as stale. (feature review)
- 12. minor src/codeintel/gateway/reindexer.py (ticket 03): unfingerprintable roots submit a pass every window and the gateway flags `reindexing` on an unchanged tree. Decide: document it in the spec, or skip the flag for debounce-only passes. Also `_tree_fingerprint` uses `os.stat`; use `os.lstat`. (feature review)
- 13. minor src/codeintel/gateway/gateway.py `_index_moved` (ticket 15): every edge-op cache hit runs `graph_index_age` (git calls). Fix: note the cost in a comment, or cache HEAD's commit time per generation. (feature review)

## 17-review-minors (left for later)

- 1. minor src/codeintel/engines/graph/backend.py: ids withheld only when the projects list is comma-separated; `and`, `;`, newline, backticks, or `projects (a-b-c, d-e-f)` leak every id after the first. Real backend prints a JSON array (covered), so defensive only. Fix: withhold every slug-shaped id to the end of the clause after `project(s)`. (pair review)
- 2. minor src/codeintel/engines/graph/backend.py: `_BRACKETED` rewrites every hyphenated token inside any `[...]`, e.g. `error in [src/my-file.py] at [2024-01-01]` -> `error in [src/<project>] at [<project>]`, damaging backend messages. Fix: only rewrite brackets whose whole content is a list of bare or quoted ids. (pair review)
- 3. minor src/codeintel/core/redact.py: `FILE:///home/alice/x` (case-sensitive scheme), `file://localhost/home/alice/x` and `///home/alice/x` are not redacted and contains_home_path misses them. Fix: case-insensitive scheme, optional localhost. (pair review)
