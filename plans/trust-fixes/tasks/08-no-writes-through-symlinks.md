# Never write CODE_INTEL.md through a symlink, and refuse AGENTS.md, CLAUDE.md or USING_CODEINTEL.md links that leave the work tree or enter .git

Type: task
Status: open
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
