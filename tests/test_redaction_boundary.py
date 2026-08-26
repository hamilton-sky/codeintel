"""Redaction must remove the home directory and touch nothing else.

Two distinct corruptions, both from matching the home path as a bare substring:

1. Any path that merely STARTED with the home string was mangled into one that does not exist —
   ``/rootfs/etc/config.py`` -> ``~fs/etc/config.py`` under ``HOME=/root``. Nothing about ``/root``
   is special; any home that is a string prefix of a real sibling does it
   (``HOME=/home/sh`` turned ``/home/shammai/app.py`` into ``~ammai/app.py``). The damaged path
   lands in the field the agent reads as the answer, with nothing marking it as damaged.

2. The flattened project-id form of a single-segment home is a bare English word, and replacing it
   rewrote ordinary prose: under ``HOME=/root``, ``"root cause analysis"`` became
   ``"<home> cause analysis"`` — in comments, docstrings and every snippet printed.

``HOME=/root`` is the routine container case, which is where this was reported from.

The generative tests at the bottom are the point: this is a class of bug, not one case, and the
invariant ("under the home directory is redacted, everything else is untouched") is checkable over
many shapes at once.
"""
from __future__ import annotations

import pytest

from codeintel.redact import (
    contains_home_path,
    looks_like_any_home_path,
    redact,
    redact_text,
)

# Homes chosen to be adversarial in different ways: single-segment (flattens to a common word),
# short (a prefix of many real paths), deep, and one with a hyphen already in it.
HOMES = ["/root", "/home/sh", "/Users/alice", "/home/deploy/app", "/home/my-user"]

# Appended DIRECTLY to the home string (no separator) — every one of these is a DIFFERENT path or
# a different word, and none may be altered.
SIBLING_SUFFIXES = ["fs/etc/config.py", "_cause.md", "kit", "-backup/x.py", "2/old.py", ".bak"]


@pytest.fixture
def home(monkeypatch):
    def _set(value: str) -> str:
        monkeypatch.setenv("HOME", value)
        return value
    return _set


# ------------------------------------------------------------------ the reported corruptions

@pytest.mark.parametrize("h", HOMES)
@pytest.mark.parametrize("suffix", SIBLING_SUFFIXES)
def test_a_path_that_merely_shares_a_prefix_is_left_alone(home, h, suffix):
    home(h)
    text = f"{h}{suffix}"
    assert redact_text(text) == text, "a different path must survive redaction unchanged"


def test_the_reported_case(home):
    home("/root")
    assert redact_text("/rootfs/etc/config.py:3 | x=1") == "/rootfs/etc/config.py:3 | x=1"
    assert redact_text("/root_cause.md") == "/root_cause.md"


def test_the_general_case_is_not_specific_to_root(home):
    """Any home that is a string prefix of a real sibling path, not just `/root`."""
    home("/home/sh")
    assert redact_text("/home/shammaihamilton/project/app.py") \
        == "/home/shammaihamilton/project/app.py"


def test_a_single_segment_home_does_not_rewrite_ordinary_prose(home):
    """`/root` flattens to `root`; replacing that word corrupts comments and docstrings."""
    home("/root")
    for text in ("root cause analysis", "the root of the call tree", "rootCause()"):
        assert redact_text(text) == text


# ------------------------------------------------------------------ redaction still redacts

@pytest.mark.parametrize("h", HOMES)
def test_real_home_paths_are_still_redacted(home, h):
    home(h)
    assert redact_text(f"{h}/project/app.py:12 | def main()") == "~/project/app.py:12 | def main()"
    assert redact_text(h) == "~"
    assert redact_text(f"/private{h}/a.py") == "~/a.py"


def test_the_flattened_project_id_form_is_still_redacted(home):
    """The leak this form exists for: a backend project id IS the flattened absolute path."""
    home("/Users/alice")
    assert redact_text("Users-alice-Documents-cobra.Execute") == "<home>-Documents-cobra.Execute"


def test_redaction_still_covers_a_whole_envelope(home):
    home("/Users/alice")
    env = {"ok": True, "result": "/Users/alice/p/a.py:1 | x", "gaps": [{"detail": "/Users/alice/p"}]}
    out = redact(env)
    assert out["result"] == "~/p/a.py:1 | x"
    assert out["gaps"][0]["detail"] == "~/p"


# ------------------------------------------------------------------ the invariants, generatively

@pytest.mark.parametrize("h", HOMES)
def test_invariant_everything_under_the_home_is_redacted(home, h):
    home(h)
    for tail in ("a.py", "deep/nested/mod.py", "x", "with space/f.py", "dash-dir/f.py"):
        for text in (f"{h}/{tail}", f"/private{h}/{tail}"):
            out = redact_text(text)
            assert out.startswith("~"), f"{text!r} -> {out!r} still exposes the home directory"
            assert h not in out


@pytest.mark.parametrize("h", HOMES)
def test_invariant_nothing_outside_the_home_is_touched(home, h):
    home(h)
    unrelated = [
        "/etc/passwd", "/var/log/app.log", "/usr/local/bin/tool", "no path here at all",
        "def resolve_root(path):",
        f"{h.rstrip('/')}x/y.py",          # one character longer -> a different directory
        f"{h.rsplit('/', 1)[0]}/other/z.py",  # a sibling under the same parent
    ]
    for text in unrelated:
        assert redact_text(text) == text, f"{text!r} was altered but is not under {h}"


def test_the_boundary_is_deliberately_asymmetric(home):
    """Guarded on the TAIL, not the head — and that asymmetry is the safe direction.

    A tail guard cannot cause a miss: if the home string is followed by more name characters, the
    text names a *different* directory and never the home, so declining to match is always right.
    A head guard could: the home path legitimately follows all sorts of characters, and every
    character we refuse to match after is a way for a real leak to survive.

    In a module whose entire purpose is preventing disclosure, a missed redaction is the worse
    failure, so the home embedded inside a longer token is still redacted. The visible cost is a
    URL that happens to contain the home path getting rewritten — and note that for a normal home
    that URL contained the account name, so redacting it was right anyway. It only looks wrong for
    a short home like `/root`, where nothing was disclosed to begin with.
    """
    home("/root")
    assert redact_text("https://example.com/root/api") == "https://example.com~/api"


@pytest.mark.parametrize("h", HOMES)
def test_invariant_the_detector_and_the_redactor_agree(home, h):
    """If they disagree, the detector reports a leak the redactor cannot remove — and the only
    ways out are to corrupt a path that was never a leak, or to teach the tests to ignore one."""
    home(h)
    samples = [
        f"{h}/p/a.py", f"/private{h}/p/a.py", h,
        h.strip("/").replace("/", "-") + "-Documents-project.Execute",
        "/etc/passwd", "root cause analysis", f"{h}fs/x.py", f"{h}-backup/x.py",
    ]
    for text in samples:
        cleaned = redact_text(text)
        assert not contains_home_path(cleaned), \
            f"{text!r} -> {cleaned!r} still trips the leak detector"


def test_a_home_that_is_the_filesystem_root_disables_redaction(home):
    """`HOME=/` would otherwise make every absolute path in every answer collapse to `~`."""
    home("/")
    assert redact_text("/etc/passwd:1 | root:x:0:0") == "/etc/passwd:1 | root:x:0:0"


# ------------------------------------------------- the second defence, which had no callers at all

def test_no_home_shaped_path_survives_redacting_our_own_answers(home):
    """`looks_like_any_home_path` was defined and never called — the module claims two independent
    defences and had one. It is the broad check: `contains_home_path` only knows THIS process's
    home, so anything redaction cannot recognise as its own is invisible to it and would ship."""
    h = home("/Users/alice")
    envelope = {
        "ok": True,
        "op": "search",
        "result": f"{h}/Documents/proj/app.py:12 | def main()\n{h}/p/b.py:3 | x = 1",
        "hint": f"run: codeintel index {h}/Documents/proj",
        "gaps": [{"section": "corpus", "detail": f"no code matched under {h}/Documents/proj"}],
    }
    out = redact(envelope)
    blob = repr(out)
    assert not looks_like_any_home_path(blob), f"a home-shaped path survived redaction: {blob}"
    assert not contains_home_path(blob)


def test_another_accounts_home_is_detected_even_though_it_cannot_be_redacted(home):
    """The case this detector was written for: an HTTP caller asking about a repo the server does
    not own. `redact_text` can only rewrite the home it knows, so `/Users/bob/...` passes straight
    through — and mapping it to `~` would be wrong anyway, since `~` claims it as the reader's."""
    home("/Users/alice")
    foreign = "/Users/bob/shared/lib.py:2 | def helper()"
    assert redact_text(foreign) == foreign, "not this process's home; nothing to rewrite to"
    assert not contains_home_path(foreign), "the narrow detector cannot see it — that is the gap"
    assert looks_like_any_home_path(foreign), "the broad detector must see it"


def test_a_sibling_sharing_the_username_is_out_of_scope_but_visible(home):
    """Pinned deliberately. `/Users/alice-backup` is a DIFFERENT directory, so redaction leaves it
    alone — rewriting it is exactly what the substring bug did, and it produced paths that do not
    exist. It does still carry the account name, so the broad detector flags it: out of scope for
    automatic rewriting, not out of sight."""
    home("/Users/alice")
    sibling = "/Users/alice-backup/p/a.py:1 | x"
    assert redact_text(sibling) == sibling
    assert not contains_home_path(sibling)
    assert looks_like_any_home_path(sibling)


def test_a_ci_path_is_home_shaped_but_must_not_be_rewritten(home):
    """Why the broad detector is an assertion helper and NOT wired into `redact_text`: blanket
    rewriting of home-shaped paths would mangle `/home/runner/work/...`, which carries nothing
    sensitive and whose actionability is the thing this module goes out of its way to preserve."""
    home("/Users/alice")
    ci = "/home/runner/work/repo/src/a.py:1 | x"
    assert redact_text(ci) == ci
