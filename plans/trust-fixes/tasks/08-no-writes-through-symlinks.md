# Never write CODE_INTEL.md through a symlink, and refuse AGENTS.md, CLAUDE.md or USING_CODEINTEL.md links that leave the work tree or enter .git

Type: task
Status: resolved
Blocked by: —
Test first: yes
Level: 3
Touches: src/codeintel/core/containment.py, src/codeintel/mapping/mapper.py, src/codeintel/cli/index.py, src/codeintel/cli/map.py, src/codeintel/setup/injector.py, src/codeintel/transports/mcp.py, docs/deploy.md, tests/test_hardening.py, tests/test_mapper.py, tests/test_cli_commands.py

This ticket fixes T6, the security finding `map-symlink-write-through`. It also fixes two small findings in the same files: `install-inject-advice-noop` and `deploy-doc-stale-symlink-hole`.

The threat: a cloned, untrusted repo ships `CODE_INTEL.md -> ~/.bashrc`, and a routine `codeintel map`, `codeintel index` or MCP `code.map` overwrites the target. The injector's `_write_using_codeintel_md` has the same hole through `USING_CODEINTEL.md`.

The rule is the user's decision (brief, `Decided:`; spec.md Decisions, "Symlinked context files (T6)"):

- `CODE_INTEL.md` is codeintel's own generated file and is never written through a symlink at all, wherever the link points.
- `AGENTS.md`, `CLAUDE.md` and `USING_CODEINTEL.md` are refused when the target, or any parent of it, resolves outside the repo root, or resolves inside the repo's `.git` directory. A link that resolves to a regular file elsewhere inside the work tree is still followed (shared rules files; pinned by `tests/test_mapper.py::test_inject_writes_through_a_symlinked_rules_file`).

1. **`src/codeintel/core/containment.py`: add `write_contained(root, name, content, *, follow_links: bool) -> str | None`.** It is the one write path for files codeintel writes into a repo. It returns `None` on success, or a refusal reason.
   - `follow_links=False` (used for `CODE_INTEL.md`): if `os.path.islink(join(root, name))`, refuse, whatever the link points at, including a dangling link or one inside the repo.
   - `follow_links=True` (the injector's files): resolve with `contained_path(real_root(root), join(root, name))`. Refuse when the realpath leaves the root (a symlink out, a parent directory that is a symlink out, or a dangling link). Also refuse when the realpath is `realpath(join(root, ".git"))` or under it, when that is a directory. (When `.git` is a file, a worktree or submodule, its git dir lives outside the root and the first rule already refuses it.)
   - Every refusal logs a WARNING naming the path and the reason (`symlink`, `outside-repo`, `inside-git-dir`).
   - A symlink that resolves to a regular file elsewhere inside the work tree is written through to its target, and the link is kept.
   - Write atomically:
     - Create a sibling temp file in the resolved file's directory with `os.open(tmp, O_WRONLY|O_CREAT|O_EXCL|getattr(os, "O_NOFOLLOW", 0), mode)`.
     - Carry over the existing file's mode, as `injector._write_file` does today.
     - Call `os.replace(tmp, resolved)`.
   - Creating a file that does not exist yet goes through the same `O_EXCL|O_NOFOLLOW` open, so a dangling link is never followed.
2. **Route the writes through `write_contained`.**
   - `MapGenerator.write`: return `(path, False)` on refusal.
   - `injector._write_file` and `_write_using_codeintel_md`.
   - `MapGenerator.write` and the refresh gate in `src/codeintel/cli/index.py` use `follow_links=False`; the injector uses `follow_links=True`.
   - The refresh gate in `src/codeintel/cli/index.py`: `os.path.isfile` follows links, so check `os.path.islink` first. A refusal skips the refresh silently, as other refresh failures do.
3. **Report the refusal.** `MapGenerator.write` records why it did not write (for example a `last_refusal` attribute, or a third return state the callers read).
   - `src/codeintel/cli/map.py` prints "Refused to write CODE_INTEL.md: it is a symlink" (naming the path) and exits 1.
   - The `code.map` handler in `src/codeintel/transports/mcp.py` returns that reason instead of the "new map was a stub" note.
   - `tests/test_cli_commands.py` replaces `MapGenerator` with a small `_Gen` fake that has no refusal attribute (`test_map_keeps_the_existing_file_when_the_new_map_is_a_stub` expects exit 0 and the "new map was a stub" line). `cli/map.py` reads the refusal with `getattr(gen, "last_refusal", None)` (or the equivalent), so a stub keep and a symlink refusal stay distinct and that test passes unchanged.
4. **`map --inject` creates AGENTS.md.** In `src/codeintel/cli/map.py`, pass `create=True` to `Injector().inject`, since the user asked by name. That makes `offer_injection`'s printed advice true.
5. **`docs/deploy.md`.** The "standing hole" section says the snippet read path is closed via `core.containment.open_contained`. It names the read paths not audited (the `context` op), and adds that repo-file writes go through `write_contained`.

## Not in this ticket

- `~/.claude.json` written by `src/codeintel/setup/installer.py` follows symlinks on purpose (dotfile managers, pinned by `tests/test_installer.py::test_install_writes_through_a_symlinked_config`). That file is the user's own, not the repo's, and stays as it is.
- The MCP `code.map` `inject` keeps `create=False`: creating a file needs explicit consent.

## Done when

- `tests/test_hardening.py` gains these tests. In each, the victim file is byte-identical afterwards and the outcome reports the refusal. They fail on main and pass here:
  - `test_map_refuses_symlinked_code_intel_md` (the repro from the findings), and the same for `index`'s refresh
  - `CODE_INTEL.md` linked to a file inside the repo (a regular file elsewhere in the tmp repo), and a dangling `CODE_INTEL.md` link: both refused, nothing created
  - `Injector().inject` with `AGENTS.md`, `CLAUDE.md` and `USING_CODEINTEL.md` each linked outside the root, and through a parent directory symlinked outside the root
  - `Injector().inject` with `AGENTS.md` linked to `.git/config` and to `.git/hooks/pre-commit`: refused, both files unchanged
- `tests/test_cli_commands.py`:
  - `codeintel map --inject` in a repo with neither context file creates AGENTS.md
  - `map` on a symlink exits 1 with the refusal line
  - the existing fake-injector test accepts the `create` keyword
- `tests/test_mapper.py::test_inject_writes_through_a_symlinked_rules_file` still passes unchanged. `.venv/bin/pytest tests/test_hardening.py tests/test_mapper.py tests/test_cli_commands.py tests/test_cli_install_offer_injection.py tests/test_installer.py tests/test_mcp_server.py tests/test_summary_integrity.py -q -o addopts=` passes, and all existing tests still pass. `tests/test_cli_install_offer_injection.py`, `tests/test_installer.py` (the `~/.claude.json` symlink test), `tests/test_mcp_server.py` and `tests/test_summary_integrity.py` (`mapper._stamp_line` and `index.run` are registered tallies) are not on Touches and pass unchanged.

```check
$ .venv/bin/pytest tests/test_hardening.py tests/test_mapper.py tests/test_cli_commands.py tests/test_cli_install_offer_injection.py tests/test_installer.py tests/test_mcp_server.py tests/test_summary_integrity.py -q -o addopts=
prints passed
```

## Reference

- spec.md, the story "A user cloning an untrusted repo", and Decisions ("Symlinked context files (T6)")
- redblue-fixplan.md § T6 and § T10 (`install-inject-advice-noop`, `deploy-doc-stale-symlink-hole`)
- open(2): https://manpages.debian.org/trixie/manpages-dev/open.2.en.html

## Answer

Work started at d05a5f956ecb1a07ceb56df39b1b2a18891fa84a. Test-only commit: af9d1f5 (`test(trust-fixes): 08 failing test`).

**Built**
- `src/codeintel/core/containment.py`: `write_contained(root, name, content, *, follow_links) -> str | None` and `REFUSAL_TEXT` (`symlink`, `outside-repo`, `inside-git-dir` mapped to the words a user reads). Refusals log WARNING `refusing to write <path>: <text> (<reason>)`. Write: sibling temp `<resolved>.<pid>.<rand>.codeintel.tmp` opened `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`, existing mode carried over with `os.chmod`, then `os.replace(tmp, resolved)`; the temp is removed if anything fails. I/O errors raise (callers already catch).
- `src/codeintel/mapping/mapper.py`: `MapGenerator.last_refusal` (reset on each `write`); `write` routes through `write_contained(..., follow_links=False)` and returns `(path, False)` with `last_refusal = "Refused to write CODE_INTEL.md: it is a symlink (<path>)"`. The stub-keep check no longer reads through a link (`not islink and isfile`), so a link is always reported as a refusal, never as a kept stub.
- `src/codeintel/cli/map.py`: prints `getattr(gen, "last_refusal", None)` and exits 1; `--inject` passes `create=True`, prints any `Injector.refusals` lines, and exits 1 when one exists.
- `src/codeintel/cli/index.py`: refresh gate checks `os.path.islink` before `isfile`; a link skips the refresh (no stdout) and logs the same WARNING.
- `src/codeintel/setup/injector.py`: `Injector.refusals` (one line per refused file, naming the path); `_write_file(project_root, name, content)` and `_write_using_codeintel_md` go through `write_contained(..., follow_links=True)`; `_sync_claude_import` returns its refusal. A refused main target returns `(None, "refused")` and writes nothing else; a refused CLAUDE.md sync or USING_CODEINTEL.md is recorded while the safe file is still written.
- `src/codeintel/transports/mcp.py`: `code.map` adds `note: <last_refusal>` and `inject.refused: [...]`; inject keeps `create=False`.
- `docs/deploy.md`: the RBAC warning now says the snippet read path is closed via `core.containment.open_contained` (and `_cleanup_deleted` via `contained_path`), names the unaudited read path (the `context` op's graph/LSP halves), and says repo-file writes go through `write_contained`.
- Tests: `tests/test_hardening.py` (16 new cases), `tests/test_cli_commands.py` (2 new tests, fake-injector test now takes `create` and asserts it is True).

**Proof**
- Hardening tests: `.venv/bin/pytest tests/test_hardening.py tests/test_cli_commands.py -q -o addopts=` with `src/` stashed back to d05a5f9: 16 failed (all new ones: map, index refresh, inside/dangling links, 3 names x {link, parent-dir}, `.git/config`, `.git/hooks/pre-commit`, MCP code.map, and the 3 cli tests), 94 passed. With the change: all pass. Each hardening test asserts the victim bytes unchanged and the refusal reported (cli line / `last_refusal` / `Injector.refusals` / WARNING record for `index` / MCP `note`).
- cli tests: `test_map_inject_creates_agents_md_when_the_repo_has_no_context_file`, `test_map_on_a_symlinked_code_intel_md_exits_1_naming_the_path`, `test_map_inject_reports_what_it_did_to_which_file` (accepts `create`, asserts True) pass; `test_map_keeps_the_existing_file_when_the_new_map_is_a_stub` passes unchanged.
- `.venv/bin/pytest tests/test_mapper.py::test_inject_writes_through_a_symlinked_rules_file -q -o addopts=`: 1 passed; `git diff d05a5f9 -- tests/test_mapper.py` is empty.
- Check block `.venv/bin/pytest tests/test_hardening.py tests/test_mapper.py tests/test_cli_commands.py tests/test_cli_install_offer_injection.py tests/test_installer.py tests/test_mcp_server.py tests/test_summary_integrity.py -q -o addopts=`: `278 passed, 1 xfailed` (the xfail is the pre-existing strict c4-coverage FINDING in test_summary_integrity), exit 0. The four off-Touches files are unchanged.
- Other tests touching these modules (`test_null_reason_and_ripple, test_qualifier_scan, test_rbac, test_changed_range, test_corpus, test_architecture, test_c4, test_c4_model, test_doctor` minus the known root-only deselect): 336 passed, 26 skipped. "All existing tests still pass" for the whole suite: left to the conductor's gate.
- `ruff check src tests bench`: all checks passed. `mypy`: no issues in 88 files. `codeintel c4 . --check --out /tmp/codeintel-c4-check`: 0 gating findings, exit 0. compileall: exit 0.
- Smoke in a scratch repo: `codeintel map .` with `CODE_INTEL.md -> ../victim` printed the refusal, exit 1, victim unchanged; `codeintel map --inject .` in an empty repo created AGENTS.md and USING_CODEINTEL.md.

**Decisions**
- Refusal reporting for the injector is an attribute (`Injector.refusals`) rather than a new action string, so `(path, action)` keeps its meaning and the fakes in test_cli_commands need no change beyond `create`. Callers read it with `getattr`.
- `follow_links=True` on a missing file checks `realpath(path)` (resolves a linked parent) against the root instead of `contained_path`, because `contained_path` refuses a path that does not exist yet; an existing or dangling entry goes through `contained_path` (dangling therefore refused as `outside-repo`). A hard-linked AGENTS.md is refused too (contained_path's nlink rule).
- `cli map` exits 1 on any refusal, including an inject refusal: the user asked for a write that did not happen.
- `index`'s refused refresh logs a WARNING (stderr via logging) but prints nothing and keeps exit 0, matching "skips silently, as other refresh failures do" while still satisfying "every refusal logs a WARNING".
- Temp names carry pid + random suffix so a stale temp from a crash cannot wedge `O_EXCL` forever.

**Shortcuts taken**
- `os.chmod(tmp, mode)` by path rather than `fchmod` (Python 3.11 on Windows lacks `os.fchmod`). The temp was just created by us with `O_EXCL|O_NOFOLLOW`; a racing swap of the temp name inside the repo directory is the only gap, and an attacker with that access already owns the repo. Upgrade to `fchmod` when 3.11 is dropped.
- A chmod failure now raises (logged as an inject/map failure) instead of being silently skipped as before.

**For later tickets**
- Ticket 13 (CHANGELOG): T6 `map-symlink-write-through`, `install-inject-advice-noop` (`map --inject` now creates AGENTS.md) and `deploy-doc-stale-symlink-hole` are fixed here. New user-visible: `map` exits 1 on a refused write; MCP `code.map` gains `note` and `inject.refused`.

**Round 2 (review fixes).** Work started at 0e1ea07. Test-only commit: 04dae8e (`test(trust-fixes): 08 failing test`, 8 new cases failing for the right reason: config and pre-commit "appended" through a second spelling of `.git`, the swapped parent written through, and the in-tree refusals all reported as `outside-repo`).

**Built (round 2)**
- `src/codeintel/core/containment.py`: `write_contained` now resolves once (`realpath`, `outside-repo` if it leaves the root) and hands the path components to `_write_walk`, which opens the root and then each directory on the resolved path with `O_DIRECTORY|O_NOFOLLOW` relative to the previous descriptor, `fstat`s each and refuses `inside-git-dir` when one `samestat`-matches `os.stat(root/.git)` (only when that is a directory). The leaf is `lstat`ed through the last descriptor (`inside-git-dir`, `symlink`, `not-a-file`, `hard-link`, `dangling`), the temp is created with `dir_fd`, `fchmod`ed and `os.replace`d with `src_dir_fd/dst_dir_fd`. New `REFUSAL_TEXT` keys `dangling`, `not-a-file`, `hard-link`. `_refuse_write` became public `log_write_refusal(path, reason)`. Without `dir_fd` support (Windows) the same walk runs on paths with `lstat`.
- `src/codeintel/cli/index.py`: the refresh gate calls `log_write_refusal(map_path, "symlink")`.
- `docs/deploy.md`: says `.git` is matched by file identity, the write walks without following links, and names the race that remains on Windows.
- `tests/test_hardening.py`: `test_inject_refuses_the_git_dir_reached_by_another_spelling` (x3), `test_write_contained_refuses_a_parent_swapped_for_a_link_after_the_check`, `test_write_contained_names_why_an_in_tree_target_is_refused` (x4).

**Proof (round 2)**
- Check block `.venv/bin/pytest tests/test_hardening.py tests/test_mapper.py tests/test_cli_commands.py tests/test_cli_install_offer_injection.py tests/test_installer.py tests/test_mcp_server.py tests/test_summary_integrity.py -q -o addopts=`: `286 passed, 1 xfailed`, exit 0 (278 before + 8 new).
- `tests/test_mapper.py::test_inject_writes_through_a_symlinked_rules_file`: 1 passed; `git diff d05a5f9 --stat` on test_mapper, test_cli_install_offer_injection, test_installer, test_mcp_server, test_summary_integrity: empty.
- `tests/test_rbac.py tests/test_doctor.py tests/test_architecture.py` (root-only deselect): 103 passed.
- Acceptance `test_tf_symlinks.py` + `test_tf_small_fixes.py::test_deploy_doc_says_the_snippet_symlink_hole_is_closed_via_open_contained`: 14 passed. (The other 7 failures in test_tf_small_fixes.py are reset/mcp-size/ctrl-c/rows stories of other tickets, untouched here.)
- `ruff check src tests bench`: all checks passed. `mypy`: no issues in 88 files. compileall exit 0. `codeintel c4 . --check --out /tmp/codeintel-c4-check`: 0 gating findings, exit 0. Whole Test command: left to the conductor's gate.

**Review fixes**
1. Case-insensitive `.git`: fixed by identity, as suggested. Every directory the write passes through and the leaf are `samestat`-compared with the git dir; for a new file the walk stops at its parent. Test fakes the case-insensitive spelling by making `realpath` spell paths under `.git` as `<repo>/./.git/...` (a different string for the same directory, which is what macOS returns for `.GIT/...`); it fails on the round-1 code (`appended` into `.git/config`) and passes now.
2. Parent-swap race: closed with `dir_fd` on POSIX (each component opened `O_NOFOLLOW|O_DIRECTORY`, create/chmod/replace through the last descriptor); a component that turned into a link is refused as `symlink`. Test fakes the swap by having `realpath` report `sub/AGENTS.md` while `sub` is a link out; refused, the outside file and directory untouched. Windows (no `dir_fd`) keeps the race, documented in `docs/deploy.md`.
3. Refusal reasons: dangling in-tree link -> `dangling`, a link to `.git` itself -> `inside-git-dir`, a link to an in-tree directory -> `not-a-file`, a hard link -> `hard-link`; `outside-repo` only when the realpath leaves the root. Pinned by the 4-case test.
4. `cli/index.py` reuses `log_write_refusal` instead of rebuilding the text.

**Decisions (round 2)**
- `.git` itself, not only things under it, is matched (the walk compares every component and the leaf), so `USING_CODEINTEL.md -> .git` reports `inside-git-dir`.
- The git check runs before the dangling check, so a dangling link into `.git/hooks/` is reported as `inside-git-dir`, the more useful reason.
- `hard-link` is still refused only for the followed files (round-1 behaviour via `contained_path`); `CODE_INTEL.md` is replaced by rename, which never writes through another name of the inode.

**Shortcuts taken (round 2)**: the round-1 `os.chmod` shortcut is gone on POSIX (`fchmod`); `os.chmod` by path remains only on the no-`dir_fd` fallback. The root directory itself is opened following links (it is the user's chosen root, already `realpath`ed).

## Review findings (round 1, pair review)

QUALITY
1. major src/codeintel/core/containment.py: `write_contained` decides "inside .git" by comparing path strings, `_inside(git_dir, resolved)`. On a case-insensitive filesystem (macOS APFS default), a cloned repo can ship `AGENTS.md -> .GIT/config` or `USING_CODEINTEL.md -> .GIT/hooks/pre-commit`; `os.path.realpath` keeps the `.GIT` spelling, so the prefix check fails and the write lands in the real `.git` (breaking `.git/config`, or appending to an executable hook with mode kept, or creating a hook). Fix: check by file identity: `os.stat` the git dir once, walk `resolved` and its parents up to the root, refuse if any `os.path.samestat`-matches the git dir (for a new file, start from its parent). Add a test faking case-insensitivity or using a second path to `.git` (e.g. a symlinked parent).
2. minor src/codeintel/core/containment.py: check and write are separate; `O_NOFOLLOW` guards only the last component. A caller who can write inside the root can swap a parent directory for a symlink between the check and `os.replace`. Fix: open the resolved parent with `O_DIRECTORY|O_NOFOLLOW` and use `dir_fd=` for `os.open(tmp)` and `os.replace`, or document the accepted race in `docs/deploy.md`.
3. minor src/codeintel/core/containment.py: with `follow_links=True` and an existing path, every `contained_path` failure is reported as `outside-repo`, including a dangling in-tree link, a link to an in-tree directory (`USING_CODEINTEL.md -> .git`) and a hard-linked file (nlink>1). Fix: tell these apart, or word it "does not resolve to a single regular file inside the repo".
4. minor src/codeintel/cli/index.py: the refresh-gate WARNING rebuilds the refusal text by hand instead of `REFUSAL_TEXT["symlink"]` / `_refuse_write`. Fix: reuse the containment helper.

Range d05a5f9..0e9c6ba (HEAD). Done when re-run: 278 passed, 1 xfailed. Probes: dangling in-tree link refused; `AGENTS.md -> .git/config` refused; `USING_CODEINTEL.md -> .git` refused; in-tree `CLAUDE.md -> docs/rules.md` written through. Only way past found is finding 1.

codeintel: changed d05a5f956ecb1a07ceb56df39b1b2a18891fa84a: helped

REVIEW: FAIL

SPEC
- tests/test_hardening.py gains the refusal tests (map and index refresh on a symlinked CODE_INTEL.md; in-repo and dangling links refused; AGENTS.md, CLAUDE.md, USING_CODEINTEL.md linked outside directly and through a parent; AGENTS.md linked to .git/config and .git/hooks/pre-commit), failing on main and passing here: MET. 34 passed at HEAD; all 12 refusal cases failed against the base source (af9d1f5 src).
- tests/test_cli_commands.py (`map --inject` creates AGENTS.md; `map` on a symlink exits 1 with the refusal line; fake injector accepts `create`): MET. Pass at HEAD, fail on base.
- tests/test_mapper.py::test_inject_writes_through_a_symlinked_rules_file passes unchanged; check block passes; existing tests pass; the four files not on Touches unchanged: MET.
- Acceptance: test_tf_symlinks.py plus test_tf_small_fixes.py::test_deploy_doc_says_the_snippet_symlink_hole_is_closed_via_open_contained: 14 passed.
- scope: OK. MCP `code.map` inject still uses `create=False`; installer.py untouched.
- test first: OK (af9d1f5).

END 0e9c6ba = HEAD. Answer not seen. No codeintel queries.

REVIEW: PASS
