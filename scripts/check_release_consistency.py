#!/usr/bin/env python3
"""Release consistency — catch a version that was documented but never actually shipped.

Releases are cut by pushing a `vX.Y.Z` tag, which is the only thing that triggers the PyPI publish
workflow. Bumping `__version__` and writing the CHANGELOG entry are the steps a human does by hand;
pushing the tag is the step a human forgets. When that happens nothing goes red — CI is green, the
commit looks like a release, and the version simply never reaches users. The next bump then buries
it, so the miss is usually noticed only when someone asks why a fix they can read in the CHANGELOG
isn't in the package they installed.

That is not hypothetical: 0.13.2, 0.14.1 and 0.15.0 were each bumped, documented and committed
without ever being tagged, and the fixes in them sat unshipped behind a newer version number.

The rule enforced here: every version documented in CHANGELOG.md must have a matching `vX.Y.Z` tag,
with exactly two exemptions —

  * the version currently in `__version__`, which is legitimately untagged in the window between
    the release commit and the tag push, and
  * anything listed in KNOWN_UNRELEASED below, which records the versions already superseded before
    anyone noticed they were missing.

Run locally or in CI:

    python scripts/check_release_consistency.py

Exits 0 when consistent, 1 with an actionable report otherwise. Needs tags in the working copy —
in CI that means checking out with `fetch-depth: 0` and `fetch-tags: true`.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Versions documented in the CHANGELOG that were never tagged and never will be — each was
# superseded by a later release before the gap was found, so tagging them now would publish stale
# code. They are recorded rather than deleted from the CHANGELOG because the entries still document
# real changes that reached users inside a later version. Do not add to this list to silence the
# check: if a version has not shipped yet and should, tag it instead.
KNOWN_UNRELEASED = {
    "0.13.2",  # superseded by 0.14.0
    "0.14.1",  # superseded by 0.14.2
    "0.15.0",  # superseded by 0.15.1
    # 0.15.1 was still untagged when the production-readiness review found that its headline fix —
    # "answers could come from a different repository" — had only been applied to `doctor`, not to
    # the query path. Publishing it would have shipped a changelog entry claiming a fix the product
    # surface did not have, so the fix was completed and the result released as 0.15.2 instead.
    "0.15.1",
}


def current_version() -> str:
    """Read `__version__` without importing the package (which would need its dependencies)."""
    src = (ROOT / "src" / "codeintel" / "__init__.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise SystemExit("could not find __version__ in src/codeintel/__init__.py")


def changelog_versions() -> list[str]:
    """Every `## [X.Y.Z]` heading, newest first — the order they appear in the file."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.MULTILINE)


def tagged_versions() -> set[str]:
    """Versions with a `vX.Y.Z` tag in the working copy."""
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git not found on PATH — this check reads tags from the working copy")
    try:
        out = subprocess.run(
            [git, "tag", "--list", "v*"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"could not list git tags: {exc}") from exc
    return {line[1:] for line in out.split() if re.fullmatch(r"v\d+\.\d+\.\d+", line)}


def check_tag(tag: str) -> int:
    """Verify a tag about to be published matches what the tree actually ships.

    The reverse of the check below, and the case that bit for real: `v0.15.3` was pushed while
    `__version__` still read `0.15.2`. The build produced a wheel labelled 0.15.2, PyPI rejected it
    as a duplicate, and the publish failed several minutes in with "File already exists" — naming
    neither the tag nor the mismatch that caused it. Run as the FIRST step of the publish workflow
    so it costs seconds and says what to do.
    """
    wanted = tag[1:] if tag.startswith("v") else tag
    version = current_version()
    if wanted != version:
        # The remediation deliberately does NOT say `git tag -f && git push --force`, which is what
        # it used to say. The `release tags (v*)` ruleset now carries `deletion` and
        # `non_fast_forward` with an empty bypass list, so a pushed `v*` tag can be neither moved
        # nor deleted — by anyone, including the maintainer. Advising a force-push would send the
        # reader at the one operation the protection exists to refuse, and they would discover that
        # only after the push was rejected. A tag that is already published is spent: the way
        # forward is a new one.
        print(
            f"tag {tag} does not match __version__ ({version}).\n"
            f"`v*` tags are protected against deletion and force-push, so {tag} cannot be moved — "
            f"bump __version__ and the CHANGELOG, commit, then tag that commit afresh:\n"
            f"  git tag v<the-new-version> && git push origin v<the-new-version>"
        )
        return 1
    if version not in changelog_versions():
        print(f"CHANGELOG.md has no '## [{version}]' section — publishing a version with no entry "
              f"is how three earlier releases went out undocumented.")
        return 1
    print(f"tag {tag} matches __version__ ({version}), and the CHANGELOG documents it")
    return 0


def main() -> int:
    if "--tag" in sys.argv:
        idx = sys.argv.index("--tag")
        if idx + 1 >= len(sys.argv):
            print("--tag needs a value, e.g. --tag v1.2.3")
            return 1
        return check_tag(sys.argv[idx + 1])

    version = current_version()
    documented = changelog_versions()
    tags = tagged_versions()

    if not tags:
        # A tagless checkout cannot distinguish "never tagged" from "tags not fetched", and
        # reporting every release as missing would be worse than useless.
        print("no vX.Y.Z tags found — fetch tags before running this check (CI: fetch-tags: true)")
        return 1

    if not documented:
        print("no versioned entries found in CHANGELOG.md")
        return 1

    if documented[0] != version:
        print(
            f"version mismatch: __version__ is {version} but the newest CHANGELOG entry is "
            f"{documented[0]}.\nBump one to match the other — the top entry must describe the "
            f"version about to ship."
        )
        return 1

    missing = [v for v in documented if v not in tags and v != version and v not in KNOWN_UNRELEASED]
    if missing:
        print("documented but never released — these versions have a CHANGELOG entry and no tag:\n")
        for v in missing:
            print(f"  {v}    git tag v{v} <commit> && git push origin v{v}")
        print(
            "\nEach one is a set of changes users cannot install. Tag it to ship it, or add it to "
            "KNOWN_UNRELEASED in this script if it was superseded and should stay unpublished."
        )
        return 1

    unreleased = " (current, awaiting its tag)" if version not in tags else ""
    print(f"release consistency OK — {len(documented)} documented, {len(tags)} tagged")
    print(f"  current version: {version}{unreleased}")
    if version not in tags:
        print(f"  to ship it:      git tag v{version} && git push origin v{version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
