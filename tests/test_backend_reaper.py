"""The session reaper (conftest.py) deletes backend projects. Its one non-negotiable property is
that it deletes ONLY ephemeral pytest registrations and never a real project — a bug there would
delete a user's index, the corpus, or codeintel's own. So the selection is guarded here directly."""
from __future__ import annotations

from tests._backend_reaper import leaked_project_names

# A realistic `list_projects` payload: the real registrations that must survive, beside the ephemeral
# pytest ones the reaper exists to remove. The real roots are the actual shapes seen in the wild — a
# user checkout, the pinned corpus, codeintel itself — none of which contains the pytest marker.
_REAL = [
    {"name": "codeintel", "root_path": "/Users/dev/Documents/project/codeintel"},
    {"name": "corpus-click", "root_path": "/private/tmp/codeintel-corpus/click"},
    {"name": "user-repo", "root_path": "/Users/dev/Documents/project/pathly-adapters"},
    # a scratch project NOT under a pytest dir must also survive — only pytest tmp is in scope
    {"name": "scratch", "root_path": "/private/tmp/claude-501/session/scratchpad/prov"},
]
_JUNK = [
    {"name": "j1", "root_path": "/private/var/folders/n7/T/pytest-of-dev/pytest-271/test_code_query0/repo"},
    {"name": "j2", "root_path": "/tmp/pytest-of-alice/pytest-1/test_a_role_may_target0/allowed"},
]


def test_reaper_selects_every_pytest_registration():
    selected = set(leaked_project_names(_REAL + _JUNK))
    assert selected == {"j1", "j2"}


def test_reaper_never_selects_a_real_project():
    """The safety property, stated as its own assertion so it fails loudly if the marker ever widens.
    (Mutating `_PYTEST_TMP_MARKER` to '/' in _backend_reaper.py makes this fail — every real project
    is then selected — which is how we know the check can catch the dangerous direction.)"""
    selected = set(leaked_project_names(_REAL + _JUNK))
    for real in _REAL:
        assert real["name"] not in selected, f"the reaper would delete real project {real['name']!r}"


def test_reaper_skips_nameless_and_malformed_entries():
    """`delete_project` needs a name, and the backend payload is untrusted."""
    entries = [
        {"root_path": "/tmp/pytest-of-dev/pytest-9/test_x0"},   # no name — cannot be deleted
        {"name": "", "root_path": "/tmp/pytest-of-dev/pytest-9/test_y0"},
        "not-a-dict",
        None,
        {"name": "j3", "root_path": "/tmp/pytest-of-dev/pytest-9/test_z0"},
    ]
    assert leaked_project_names(entries) == ["j3"]
    assert leaked_project_names("not-a-list") == []


def test_reaper_dedupes_names():
    """The backend can hold more than one registration for a root; a name is deleted once."""
    dup = [
        {"name": "j", "root_path": "/tmp/pytest-of-dev/pytest-1/test_a0"},
        {"name": "j", "root_path": "/tmp/pytest-of-dev/pytest-1/test_a0"},
    ]
    assert leaked_project_names(dup) == ["j"]
