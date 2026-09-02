"""Repair `.serena/project.yml`'s `language_servers:` list from what the repository actually contains.

`LspProvider._language_coverage` already censuses the tree by extension and compares it against the
configured list, and `_unserved_note` already tells the caller *"add the missing language(s) to
`language_servers:` and re-run"*. This module is the part that stops advising and does it.

**Why it matters, measured.** serena gets ONE config per project naming a fixed list of language
servers, and its own init writes a single language. On `pathly-adapters` that list read `[python]`
while the tree held **771 TypeScript files** against 418 Python — so every TypeScript `symbol` query
returned an empty body, and `doctor` reported the engine healthy, which was true about the process and
false about every answer it would give. serena supports several language servers in parallel
(*"When using multiple language servers, the first language server that supports a given file will be
used"* — from the config's own comments), so the whole defect is one missing line of YAML.

FOUR THINGS THIS DELIBERATELY DOES NOT DO:

1. **It does not run at query time.** Editing a user's config as a side effect of asking a question is
   wrong, and a language server that needs an extra install would then fail a query that used to work.
   It is a `codeintel setup --languages` step, where `run_setup`'s own rule applies: each flag IS
   consent.
2. **It does not create the file.** No `.serena/project.yml` means serena was never initialised here;
   writing a bare one would skip the scaffolding and the explanatory comments serena's own init
   produces. That is reported, not repaired.
3. **It does not reorder what is already there.** The config's comments state that the FIRST entry is
   the default and the fallback, so re-sorting by file count could silently change which server
   answers for an ambiguous file. Existing entries keep their order and their meaning; missing ones
   are appended, most-populous first.
4. **It does not propose a language serena cannot serve.** This is not hypothetical — `_LANG_EXTS`
   carries a `c` key and serena's accepted ids contain no bare `c` (only `cpp` / `cpp_ccls`), so a
   naive writer would emit `- c` and break serena's startup for a repo that had been working. Hence
   `_SERENA_ID` below: an explicit map, and anything unmapped is skipped and reported rather than
   guessed at.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

from codeintel.provider import log_swallowed

# codeintel's census language -> the id serena's `language_servers:` accepts.
#
# Explicit rather than derived, because the two vocabularies are NOT the same and the mismatch is
# silent: serena's list has `cpp` and `cpp_ccls` but no bare `c`, so mapping `c` onto itself would
# write a value serena rejects. `c` maps to `cpp` because the servers behind it (clangd) serve C and
# C++ from one process.
#
# Anything absent from this map is skipped, which is the safe direction: the cost is a language we do
# not offer to configure, not a config we corrupt. serena's own comment says its id list "may be
# outdated", so drift is expected and must fail closed.
_SERENA_ID: dict[str, str] = {
    "python": "python",
    "typescript": "typescript",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "csharp": "csharp",
    "ruby": "ruby",
    "php": "php",
    "cpp": "cpp",
    "c": "cpp",
    "kotlin": "kotlin",
    "swift": "swift",
}

_CONFIG_REL = os.path.join(".serena", "project.yml")
_KEY = "language_servers:"


def _census(project_root: str) -> tuple[list[str], dict[str, int]]:
    """Reuse the provider's own census so the plan and the warning can never disagree."""
    from codeintel.providers.lsp import LspProvider

    return LspProvider()._language_coverage(project_root)


def plan(project_root: str, *, floor: int | None = None) -> dict[str, Any]:
    """What `language_servers:` should say, without touching anything. Never raises.

    Returns ``{path, exists, configured, census, additions, proposed, unmappable, below_floor,
    problem}``. `additions` is empty when nothing is missing, which is what makes applying idempotent.

    `floor` is the same threshold `_unserved_note` uses to decide a language is worth mentioning
    (`LspProvider._UNSERVED_FILE_FLOOR`, 5), and it is applied here for a stronger reason than tidy
    reporting: every entry in this list is a language server serena will BOOT. One stray `.ts` file in
    a Python repo should not cost a whole TypeScript server's startup and memory on every session. A
    language below the floor is recorded in `below_floor` rather than dropped silently, so the
    information is still available to anyone who wants to add it by hand.
    """
    out: dict[str, Any] = {
        "path": "", "exists": False, "configured": [], "census": {},
        "additions": [], "proposed": [], "unmappable": [], "below_floor": [], "problem": "",
    }
    try:
        root = os.path.abspath(str(project_root or "") or os.getcwd())
    except Exception:
        root = str(project_root or "")
    path = os.path.join(root, _CONFIG_REL)
    out["path"] = path
    out["exists"] = os.path.isfile(path)
    if not out["exists"]:
        # See rule 2 in the module docstring: report, do not scaffold.
        out["problem"] = "no-serena-config"
        return out

    try:
        configured, census = _census(root)
    except Exception as exc:
        log_swallowed("lang_config.plan.census", exc)
        out["problem"] = "census-failed"
        return out
    out["configured"] = list(configured)
    out["census"] = dict(sorted(census.items(), key=lambda kv: (-kv[1], kv[0])))
    if not configured:
        # `_language_coverage` returns empty when it cannot find the key at all — a shape it does not
        # recognise. Editing a file we could not parse is how a config gets corrupted.
        out["problem"] = "no-language-servers-key"
        return out

    if floor is None:
        from codeintel.providers.lsp import LspProvider

        floor = int(LspProvider._UNSERVED_FILE_FLOOR)

    already = set(configured)
    additions: list[str] = []
    unmappable: list[str] = []
    below_floor: list[str] = []
    # Most-populous first, so the most consequential missing language is added nearest the front.
    for lang, count in out["census"].items():
        if lang in already:
            continue
        if count < floor:
            below_floor.append(lang)
            continue
        serena_id = _SERENA_ID.get(lang)
        if serena_id is None:
            unmappable.append(lang)
            continue
        if serena_id in already or serena_id in additions:
            # `c` and `cpp` both land on `cpp`; adding it twice would be a broken config.
            continue
        additions.append(serena_id)

    out["additions"] = additions
    out["unmappable"] = unmappable
    out["below_floor"] = below_floor
    out["proposed"] = list(configured) + additions
    return out


def _rewrite(text: str, proposed: list[str]) -> str | None:
    """Replace the `language_servers:` item lines, leaving every other byte alone.

    A surgical text edit rather than a YAML round trip, for two reasons. There is no YAML dependency
    in this package and adding one to write four words would be absurd — `_language_coverage` parses
    the same block by hand for the same reason. And a round trip would discard the ~35 lines of
    comments serena's init writes above this key, including the list of accepted ids and the note
    that some servers need extra setup, which is the most useful documentation the file has.

    Returns ``None`` when the key is not found, so the caller can refuse rather than append blindly.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if line.strip() == _KEY or line.strip().startswith(_KEY):
            start = index
            break
    if start is None:
        return None

    # The block is the run of `- item` lines directly after the key. It stops at the first line that
    # is neither an item nor blank-inside-the-run, which is how the trailing comments and the next key
    # survive untouched.
    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped.startswith("- "):
            end += 1
            continue
        break

    newline = "\n"
    if lines[start].endswith("\r\n"):
        newline = "\r\n"
    rendered = [f"- {name}{newline}" for name in proposed]
    return "".join(lines[:start + 1] + rendered + lines[end:])


def apply_plan(project_root: str, *, apply: bool = False) -> dict[str, Any]:
    """Write the proposed `language_servers:` list. Dry-run unless `apply`. Never raises.

    Follows `reset.py`'s convention: the default is to say what would change and change nothing.

    The write is atomic (temp file in the same directory, then `os.replace`) and **verified by
    re-reading through the provider's own parser**. A half-written config is worse than the defect
    being fixed: serena would refuse to start, and the LSP engine would go from answering one language
    to answering none.
    """
    result = plan(project_root)
    result["applied"] = False
    result["verified"] = None
    if result["problem"] or not result["additions"]:
        return result
    if not apply:
        return result

    path = result["path"]
    try:
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
    except OSError as exc:
        log_swallowed("lang_config.apply_plan.read", exc)
        result["problem"] = "config-unreadable"
        return result

    updated = _rewrite(original, result["proposed"])
    if updated is None:
        result["problem"] = "language-servers-key-not-found"
        return result
    if updated == original:
        return result

    # Same directory as the target, so `os.replace` is a rename within one filesystem and therefore
    # atomic: a reader either sees the old config or the new one, never a partial write.
    directory = os.path.dirname(path) or "."
    tmp_path = ""
    try:
        handle_fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".project.yml.", suffix=".tmp")
        with os.fdopen(handle_fd, "w", encoding="utf-8") as fh:
            fh.write(updated)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""
    except Exception as exc:
        log_swallowed("lang_config.apply_plan.write", exc)
        result["problem"] = "config-unwritable"
        if tmp_path:
            # Leaving a `.project.yml.*.tmp` behind in the user's `.serena/` would be litter that
            # looks like a broken config.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return result

    result["applied"] = True
    # Read it back through the same parser the provider uses. If that disagrees with what was written,
    # say so rather than reporting success — this is the one operation here that can leave a repo worse
    # than it started.
    try:
        configured_after, _ = _census(os.path.dirname(os.path.dirname(path)))
        result["verified"] = configured_after == result["proposed"]
        result["configured_after"] = configured_after
    except Exception as exc:
        log_swallowed("lang_config.apply_plan.verify", exc)
        result["verified"] = None
    return result


def describe(result: dict[str, Any]) -> str:
    """One line for the setup report."""
    problems = {
        "no-serena-config": "no .serena/project.yml — run serena once, or `codeintel setup --warm`",
        "no-language-servers-key": "`language_servers:` not found in .serena/project.yml",
        "census-failed": "could not census the repository's languages",
        "config-unreadable": "could not read .serena/project.yml",
        "config-unwritable": "could not write .serena/project.yml",
        "language-servers-key-not-found": "`language_servers:` vanished between plan and write",
    }
    problem = str(result.get("problem") or "")
    if problem:
        return problems.get(problem, problem)

    additions = list(result.get("additions") or [])
    census = result.get("census") or {}
    if not additions:
        return (f"all {len(census)} detected language(s) already served "
                f"({', '.join(result.get('configured') or [])})")

    counts = ", ".join(f"{name} ({census.get(name) or census.get('c', 0)} files)"
                       if name in census else name
                       for name in additions)
    if result.get("applied"):
        verified = result.get("verified")
        mark = "verified" if verified else ("NOT verified" if verified is False else "unverified")
        return f"added {counts} to language_servers ({mark})"
    return f"would add {counts} to language_servers — re-run with --languages to apply"
