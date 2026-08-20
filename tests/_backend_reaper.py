"""Selection logic for the session-end backend reaper (see conftest.py).

Factored out of the fixture so its one safety-critical property — it must NEVER name a real project
for deletion — is unit-testable without touching the backend. Not a test module (the leading
underscore keeps pytest from collecting it); it is imported by both conftest.py and the test that
guards the property."""
from __future__ import annotations

from typing import Any

# The one marker that proves a registration is ephemeral: pytest roots every temporary directory it
# hands a test under `.../pytest-of-<user>/pytest-<n>/...`, and such a directory never outlives its
# session. A real repository's path — a user's checkout, the pinned corpus, codeintel itself — cannot
# contain this segment, so matching on it can only ever select junk.
_PYTEST_TMP_MARKER = "/pytest-of-"


def leaked_project_names(entries: Any) -> list[str]:
    """The names of backend projects that are ephemeral pytest tmp registrations, safe to delete.

    Order-preserving, de-duplicated. Skips entries with no name (``delete_project`` needs one) and
    anything that is not a mapping."""
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(entries, list):
        return out
    for e in entries:
        if not isinstance(e, dict):
            continue
        root = str(e.get("root_path") or "")
        name = str(e.get("name") or "")
        if name and name not in seen and _PYTEST_TMP_MARKER in root:
            seen.add(name)
            out.append(name)
    return out
