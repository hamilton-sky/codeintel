"""The git half of `code.query op=changed target=<ref>`: what the branch changed, from git alone.

`changed` used to ask the graph backend what the working tree had changed, and the backend answers
for UNCOMMITTED edits only — a branch with six commits and a clean tree reported "working tree
clean". This module asks git instead, and asks it a different question: **compare the merge-base of
`<ref>` and HEAD against the WORKING TREE** — committed and uncommitted work together, which is "what
would this branch change if I committed everything now". The merge-base rather than `<ref>` itself,
so work that landed on the base branch after this one forked is not reported as this branch's.

Everything here shells out to `git` and returns plain values. Nothing raises: a root that is not a
repository, a ref that does not exist, a history with no common ancestor and a git that is not
installed each come back as a `RangeRefusal` carrying a precise `reason` and a hint, because each is
a different fact with a different remedy and `None` would have read as "nothing changed".

Two env vars keep `git` a bystander. `GIT_OPTIONAL_LOCKS=0` stops `git diff` from refreshing the
index — a read-only question must not take `.git/index.lock` and fail the user's own commit running
beside it — and the repository-location variables are dropped so that the directory being asked
about is the only thing choosing the repository (a hook or a worktree-aware tool exports `GIT_DIR`,
and honouring it here would answer about a different tree than the one asked about).

A third thing keeps `git` from running the repository's own commands: every call passes
`-c core.fsmonitor=false`. `core.fsmonitor` is a repository-level setting that names a COMMAND git
runs to ask what changed, and a repository is input this tool reads, not code it trusts — a cloned
branch under review can carry `.git/config` of its own. The command-line `-c` outranks the
repository's setting, and the speed an fsmonitor buys is irrelevant to a one-shot read.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass

from codeintel.containment import contained_path, real_root
from codeintel.source_kind import is_code_path, is_prose

_GIT_TIMEOUT_S = 30
# A file bigger than this is not a hand-written definition table worth parsing for a review answer,
# and reading it would make one pathological file the cost of every `changed` call.
MAX_SOURCE_BYTES = 1_000_000


@dataclass(frozen=True)
class RangeRefusal:
    """Why there is no answer, in the vocabulary of `safe_null_result`'s `reason` and `hint`."""

    reason: str
    hint: str


@dataclass(frozen=True)
class RangeBase:
    ref: str                  # what the caller typed, normalised (`main...HEAD` -> `main`)
    base_sha: str             # merge-base(ref, HEAD) — the side everything is compared FROM


@dataclass(frozen=True)
class FileChange:
    status: str               # A | M | D | R | T | U
    path: str                 # the path on the side that exists (new, or old for a deletion)
    old_path: str | None = None


@dataclass(frozen=True)
class ChangedFiles:
    files: list[FileChange]
    # Non-empty when `git ls-files --others` failed. The tracked half of the list is still right,
    # but the untracked files are NEW work, and a list that is silently missing them is shorter than
    # the truth with nothing to say so. The caller turns this into a gap.
    untracked_error: str = ""


@dataclass(frozen=True)
class Mention:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class MentionSearch:
    hits: tuple[Mention, ...]
    truncated: bool = False
    error: str = ""           # non-empty when the search itself failed — never "no hits"
    # Files the name appears in that are not source this op reads (a manifest, a YAML or JSON config,
    # a doc). Not hits — they are not listed as callers — but a removed function still named in a
    # `pyproject.toml` entry point or a config fails at runtime all the same, so they are counted and
    # named. Configuration first, prose last, so a short list of names leads with what can break.
    outside: tuple[str, ...] = ()


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                        "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return env


# `_git` returns these instead of a process exit code when git could not be RUN at all. They are kept
# apart from git's own non-zero exits because those are routinely an ANSWER (1 is "no match", "no
# merge-base"), and apart from each other because each has a different remedy.
_NO_GIT = -1
_NO_DIR = -2
_COULD_NOT_RUN = -3


# Put before the subcommand on every invocation, here and not at each call site, so there is exactly
# one place a git call is assembled and none of them can forget it. See the module docstring.
_GIT = ("git", "-c", "core.fsmonitor=false")


def _unrunnable(exc: Exception, root: str, args: tuple[str, ...]) -> tuple[int, bytes, str]:
    """What a failure to LAUNCH git means, in the vocabulary of `_git`'s negative codes."""
    if isinstance(exc, FileNotFoundError):
        if not os.path.isdir(root):
            return _NO_DIR, b"", f"`{root}` is not an existing directory"
        return _NO_GIT, b"", "git is not installed or not on PATH"
    if isinstance(exc, NotADirectoryError):
        return _NO_DIR, b"", f"`{root}` is not a directory"
    if isinstance(exc, subprocess.TimeoutExpired):
        return _COULD_NOT_RUN, b"", f"git {args[0]} did not finish within {_GIT_TIMEOUT_S}s"
    return _COULD_NOT_RUN, b"", f"{type(exc).__name__}: {exc}"


def _git(root: str, *args: str) -> tuple[int, bytes, str]:
    """`(returncode, stdout, stderr)`; a negative code means git could not be run (see above)."""
    try:
        # Named, not resolved through `shutil.which`: dozens of tests replace that function
        # process-wide with a fake backend path, and a resolved path here would be theirs.
        done = subprocess.run(
            [*_GIT, *args],
            cwd=root, capture_output=True, timeout=_GIT_TIMEOUT_S, env=_env(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _unrunnable(exc, root, args)
    return done.returncode, done.stdout, done.stderr.decode("utf-8", "replace").strip()


def _git_capped(root: str, *args: str, cap_bytes: int) -> tuple[int, bytes, str, bool]:
    """`_git` for a command whose output has no natural bound: `(code, stdout, stderr, cut)`.

    `subprocess.run(capture_output=True)` buffers all of stdout before it returns, so a name that
    appears on a million lines of a vendored file is a million lines in memory. This reads at most
    `cap_bytes`, kills git when there is more, and says so in `cut` — the exit code of a process that
    was killed is meaningless, and the caller must treat `cut` as "a lower bound", not as failure.
    stderr goes to a file rather than a second pipe: nothing drains a pipe while stdout is being
    read, and a git that fills one blocks forever. The timeout is a timer that kills the process,
    because a blocking read has no timeout of its own."""
    try:
        with tempfile.TemporaryFile() as errfile:
            proc = subprocess.Popen(
                [*_GIT, *args], cwd=root, stdout=subprocess.PIPE, stderr=errfile, env=_env())
            expired = threading.Event()

            def _expire() -> None:
                expired.set()
                proc.kill()

            timer = threading.Timer(_GIT_TIMEOUT_S, _expire)
            timer.start()
            stream = proc.stdout
            try:
                if stream is None:             # `stdout=PIPE` was asked for; this is for the types
                    raise OSError("git was started without an output pipe")
                out = stream.read(cap_bytes + 1)
                cut = len(out) > cap_bytes
                if cut:
                    proc.kill()
                    out = out[:cap_bytes]
                proc.wait()
            finally:
                timer.cancel()
                if stream is not None:
                    stream.close()
            if expired.is_set():
                code, _, err = _unrunnable(subprocess.TimeoutExpired(args, _GIT_TIMEOUT_S), root, args)
                return code, b"", err, False
            errfile.seek(0)
            err = errfile.read(2000).decode("utf-8", "replace").strip()
            return proc.returncode, out, err, cut
    except OSError as exc:
        code, _, err = _unrunnable(exc, root, args)
        return code, b"", err, False


_BAD_REF = re.compile(r"[\x00-\x20\x7f]")


def resolve_base(root: str, target: str) -> RangeBase | RangeRefusal:
    """Validate `target` with git and find the merge-base it names against HEAD.

    Accepts a ref (`main`, a tag, a SHA, `HEAD~3`, `origin/main`) or `<ref>...HEAD`. Anything else
    in range syntax is refused rather than reinterpreted: `a..b` means a DIFFERENT diff in git
    (`a` itself against `b`, no merge-base), and `a...b` with `b` not HEAD is a comparison of two
    other trees. Quietly answering `main..HEAD` as if it were `main...HEAD` would be a made-up
    semantic."""
    ref = (target or "").strip()
    if "..." in ref:
        left, _, right = ref.partition("...")
        if right not in ("", "HEAD"):
            return RangeRefusal(
                "unsupported-range",
                f"`{ref}` compares two other revisions; this op compares a base against the "
                f"WORKING TREE, so the right-hand side can only be HEAD. Pass `{left}` or "
                f"`{left}...HEAD`.")
        ref = left
    elif ".." in ref:
        left, _, right = ref.partition("..")
        return RangeRefusal(
            "unsupported-range",
            f"`{ref}` is a two-dot range, which git reads as `{left}` itself against "
            f"`{right or 'HEAD'}` with no merge-base. This op always compares the merge-base "
            f"against the working tree — pass `{left}` or `{left}...HEAD`.")
    # Refused before git sees it: a leading `-` would be read as an OPTION, and whitespace or a
    # control character is never part of a ref anyone meant.
    if not ref or ref.startswith("-") or _BAD_REF.search(ref):
        return RangeRefusal(
            "unknown-ref",
            f"`{ref or target}` is not a ref git would accept (empty, a leading `-`, or "
            "whitespace), so it was not passed to git")

    code, out, err = _git(root, "rev-parse", "--is-inside-work-tree")
    if code == _NO_GIT:
        return RangeRefusal("git-unavailable", f"{err} — a base ref can only be resolved by git.")
    if code == _COULD_NOT_RUN:
        return RangeRefusal("timeout", f"{err}.")
    if code == _NO_DIR:
        return RangeRefusal(
            "not-a-git-repo", f"{err}, so there is no history to compare `{ref}` against.")
    if code != 0 or out.strip() != b"true":
        return RangeRefusal(
            "not-a-git-repo",
            f"`{root}` is not inside a git work tree, so there is no history to compare "
            f"`{ref}` against. Run it from a git checkout of this project (`git init` or `git "
            f"clone`), or ask `callers` about specific symbols instead.")

    code, out, _ = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    head = out.decode().strip()
    if code != 0 or not head:
        return RangeRefusal(
            "no-merge-base",
            "HEAD does not point at a commit (a repository with no commits yet), so there is no "
            f"merge-base with `{ref}` to compare from.")
    code, out, _ = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    ref_sha = out.decode().strip()
    if code != 0 or not ref_sha:
        return RangeRefusal(
            "unknown-ref",
            f"`{ref}` does not name a commit in this repository (a branch, tag, SHA or `HEAD~N`). "
            f"If it is a remote branch, name it as `origin/{ref}`; if it was never fetched, "
            f"`git fetch` first. `changed` used to ignore its target — leave it empty to see "
            f"uncommitted edits only.")
    code, out, _ = _git(root, "merge-base", ref_sha, head)
    base = out.decode().strip()
    if code != 0 or not base:
        # Asked only here, where the answer changes what the reader should do: a shallow clone has
        # a different remedy (fetch more) from unrelated histories (nothing to fetch).
        _, shallow_out, _ = _git(root, "rev-parse", "--is-shallow-repository")
        shallow = shallow_out.strip() == b"true"
        return RangeRefusal(
            "no-merge-base",
            f"`{ref}` and HEAD share no common ancestor"
            + (" in this SHALLOW clone — fetch more history (`git fetch --deepen=N` or "
               "`--unshallow`) and ask again" if shallow else
               " — unrelated histories, so there is no point to compare from")
            + ". This is not a statement that nothing changed.")
    return RangeBase(ref, base)


def changed_files(root: str, base_sha: str) -> ChangedFiles | RangeRefusal:
    """Every file that differs between `base_sha` and the working tree, renames paired.

    `git diff <commit>` is that comparison — the commit against the working tree, staged and
    unstaged together. It does not see an UNTRACKED file, which is new work all the same, so those
    are added from `ls-files --others`. `--relative` makes the paths relative to `root` (and drops
    anything outside it), which is what the graph's `file_path` is when the indexed root is a
    subdirectory of the repository."""
    code, out, err = _git(
        root, "diff", "--name-status", "-z", "-M", "--relative", "--no-ext-diff", "--no-color",
        base_sha, "--")
    if code != 0:
        return RangeRefusal("query-failed", f"`git diff` failed: {err or 'no output'}")
    parts = out.decode("utf-8", "replace").split("\0")
    changes: list[FileChange] = []
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        kind = status[0]
        if kind in "RC" and i + 2 < len(parts):
            old, new = parts[i + 1], parts[i + 2]
            changes.append(FileChange("R" if kind == "R" else "A", new, old if kind == "R" else None))
            i += 3
        elif i + 1 < len(parts):
            changes.append(FileChange(kind, parts[i + 1]))
            i += 2
        else:
            break
    code, out, err = _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--")
    if code != 0:
        return ChangedFiles(
            changes, untracked_error=err or f"`git ls-files --others` exited {code}")
    seen = {c.path for c in changes}
    changes.extend(
        FileChange("A", p) for p in out.decode("utf-8", "replace").split("\0") if p and p not in seen)
    return ChangedFiles(changes)


def read_old(root: str, base_sha: str, path: str) -> str | None:
    """A file as it was at the merge-base, or None when it cannot be read (or is too large)."""
    code, out, _ = _git(root, "show", f"{base_sha}:./{path}")
    if code != 0 or len(out) > MAX_SOURCE_BYTES:
        return None
    return out.decode("utf-8", "replace")


def read_new(root: str, path: str) -> str | None:
    """A file as it is in the working tree, or None. Goes through `contained_path` like every other
    reader of the tree: a symlink committed to point outside the repository must not have its
    target's bytes read into an answer."""
    safe = contained_path(real_root(root), os.path.join(root, path))
    if safe is None:
        return None
    try:
        if os.path.getsize(safe) > MAX_SOURCE_BYTES:
            return None
        with open(safe, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


# The most `git grep` output that is read. A hit line is a path, a line number and the line; the
# `cap` below bounds how many SOURCE hits are kept, and this bounds how much text is read to find them
# — a name that is also a word in a lockfile or a vendored bundle can match without limit.
_GREP_OUTPUT_CAP_BYTES = 2_000_000


def grep_mentions(root: str, name: str, *, cap: int = 200) -> MentionSearch:
    """Every source line in the working tree that mentions `name` as a whole word.

    This is what stands in for the graph on a REMOVED symbol, because the graph indexed at HEAD has
    no node for a function that is gone. It is TEXT: a mention is not a call, a same-named symbol
    elsewhere matches, and a name in a comment matches. The honest use of it is as a list of places
    to look, and callers label it that way.

    Tracked files are searched in their working-tree state (so an uncommitted fix counts), plus
    untracked files that are not ignored. Hits in non-source paths are not returned as hits — by the
    same `is_code_path` rule `changed` applies to the file list — but their FILES are, in `outside`:
    dropping them uncounted would let a function still named in an entry-point table or a config
    read as unreferenced. A failed search is reported in `error` and is never an empty list: `git
    grep` exits 1 for "no match", which is a real answer, and anything else is not. Output is read up
    to `_GREP_OUTPUT_CAP_BYTES` and no further; what that cut off makes the result `truncated`."""
    if not name or name.startswith("-"):
        return MentionSearch((), error=f"`{name}` is not a searchable identifier")
    code, out, err, cut = _git_capped(
        root, "grep", "-n", "-w", "-I", "-F", "-z", "--untracked", "--exclude-standard",
        "-e", name, cap_bytes=_GREP_OUTPUT_CAP_BYTES)
    if not cut:
        if code == 1:
            return MentionSearch(())
        if code != 0:
            return MentionSearch((), error=err or f"git grep exited {code}")
    records = out.decode("utf-8", "replace").split("\n")
    if cut:
        records.pop()                  # the last record was cut mid-line
    hits: list[Mention] = []
    outside: dict[str, None] = {}
    for record in records:
        pieces = record.split("\0", 2)
        if len(pieces) != 3 or not pieces[1].isdigit():
            continue
        if not is_code_path(pieces[0]):
            outside.setdefault(pieces[0])
            continue
        hits.append(Mention(pieces[0], int(pieces[1]), pieces[2].rstrip("\r")))
    return MentionSearch(
        tuple(hits[:cap]), truncated=cut or len(hits) > cap,
        outside=tuple(sorted(outside, key=lambda p: (is_prose(p), p))))


# ------------------------------------------------------------------------------------ index age

@dataclass(frozen=True)
class IndexAge:
    state: str                # "fresh" | "stale" | "unknown"
    detail: str


def graph_index_age(project: str, root: str, paths: list[str]) -> IndexAge:
    """Whether the graph index plausibly predates the tree the diff was taken from.

    The callers of a changed symbol come from the graph, and the graph reflects whenever it was last
    written. If a changed file — or HEAD itself — is newer than that, the caller list describes an
    older tree than the one the symbols were diffed in, and an agent reading it as current would
    mistake absence for innocence.

    What the tool already knows about index age is `SemanticDb.indexed_at`, which `codeintel status`
    prints. That is the SEMANTIC index's timestamp: written by the same `codeintel index` pass, but
    a different artefact, and a graph that was rebuilt on its own (a backend-side `detect_changes`
    reindex, the background reindexer's graph half) would make it lie in either direction. So this
    reads the graph's own file — one sqlite per project, named for the project id, in the directory
    `reset` already knows how to find — and takes its mtime (WAL included, since a write lands there
    first).

    "Inputs" are the newest of: the changed files present in the working tree, and the last time HEAD
    moved by something other than a plain commit (see `_last_tree_move`) — which is what catches a
    branch switch that left every file with an old mtime. Cannot-tell is `unknown`, never `fresh`:
    the one thing this must not do is imply freshness it did not measure."""
    try:
        from codeintel.reset import _graph_cache_dir
        base = os.path.join(_graph_cache_dir(), f"{project}.db")
        stamps = [os.stat(p).st_mtime for p in (base, base + "-wal") if os.path.exists(p)]
        if not stamps:
            return IndexAge(
                "unknown", f"the graph index file for `{project}` was not found under "
                           f"{_graph_cache_dir()}, so its age cannot be read")
        written = max(stamps)
        newest, newest_what = 0.0, ""
        for rel in paths:
            try:
                m = os.stat(os.path.join(root, rel)).st_mtime
            except OSError:
                continue
            if m > newest:
                newest, newest_what = m, rel
        code, out, _ = _git(root, "rev-parse", "--git-path", "logs/HEAD")
        if code == 0 and out.strip():
            reflog = out.decode().strip()
            moved = _last_tree_move(reflog if os.path.isabs(reflog) else os.path.join(root, reflog))
            if moved is not None and moved > newest:
                newest, newest_what = moved, "HEAD (a checkout, merge, reset or rebase)"
        if not newest:
            return IndexAge(
                "unknown", "neither the changed files nor the HEAD reflog could be dated, so "
                           "there is nothing to compare the index's age against")
        # A little slack: a file written during the same second as the index pass it triggered.
        if written + 2.0 < newest:
            return IndexAge("stale", f"the graph index was last written "
                                     f"{_ago(newest - written)} before {newest_what} last changed")
        return IndexAge("fresh", "")
    except Exception as exc:
        return IndexAge("unknown", f"the graph index's age could not be read ({type(exc).__name__})")


def _last_tree_move(reflog_path: str) -> float | None:
    """When HEAD last moved by something other than a plain commit, from its reflog.

    A commit records content that was already in the working tree, so it says nothing about whether
    the files the index saw are still the files on disk — treating it as "the tree moved" flagged
    every query after every commit as stale. A checkout, merge, reset, rebase or pull is the
    opposite: it rewrites files, including callers in files the diff never mentions, which is
    exactly the population a stale graph under-reports. Reflog lines are
    `old new Name <email> <epoch> <tz>` TAB `<message>`."""
    try:
        with open(reflog_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()[-200:]
    except OSError:
        return None
    for line in reversed(lines):
        head, _, message = line.partition("\t")
        if message.startswith("commit"):
            continue
        fields = head.rsplit(" ", 2)
        try:
            return float(fields[-2])
        except (IndexError, ValueError):
            continue
    return None


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"
