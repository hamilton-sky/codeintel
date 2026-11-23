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
