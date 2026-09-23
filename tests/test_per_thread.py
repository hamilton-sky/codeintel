"""Per-request state on shared providers must not leak between concurrent requests.

`serve-http` is a `ThreadingHTTPServer` over ONE `GraphProvider`. The provider accumulates an
answer's gaps, rows and backend failure as it renders, then summarises them into the envelope —
`safe_for_destructive` included. As plain instance attributes those were shared by every thread, so
two concurrent queries could hand each other their gaps or rows.

These tests interleave two threads deterministically with a barrier: each writes its own value,
both wait until both have written, then each reads. With shared attributes the second writer's
value is what both threads read. The control at the bottom shows the harness catches exactly that.
"""
from __future__ import annotations

import threading
from typing import Any

import pytest

from codeintel.graph_backend import BackendClient
from codeintel.per_thread import PerThread
from codeintel.providers.graph import GraphProvider

_GRAPH_FIELDS: dict[str, tuple[Any, Any]] = {
    "_pending_gaps": ((({"section": "callers", "kind": "a"}),), ({"section": "callers", "kind": "b"},)),
    "_pending_rows": (({"name": "row-a"},), ({"name": "row-b"},)),
    "_pending_row_cap": (True, False),
    "_pending_withheld": (3, 7),
    "_pending_nonrow_lines": (True, False),
    "_answered_root": ("/repo/a", "/repo/b"),
}


def _interleave(obj: object, field: str, values: tuple[Any, Any]) -> list[Any]:
    """Two threads write `values[i]`, rendezvous, then read back. Returns what each read."""
    barrier = threading.Barrier(2, timeout=5)
    seen: list[Any] = [None, None]

    def worker(i: int) -> None:
        setattr(obj, field, values[i])
        barrier.wait()          # both have written before either reads
        seen[i] = getattr(obj, field)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return seen


@pytest.mark.parametrize("field", sorted(_GRAPH_FIELDS))
def test_graph_request_state_is_private_to_each_thread(field):
    provider = GraphProvider.__new__(GraphProvider)     # as the test stubs build it: no __init__
    values = _GRAPH_FIELDS[field]

    assert _interleave(provider, field, values) == list(values)


def test_the_backend_failure_is_private_to_each_thread():
    """One request's timeout is not another's: `_last_failure` decides whether an op reports
    "the backend did not answer" or renders what it got."""
    from codeintel.outcome import Missing

    client = BackendClient.__new__(BackendClient)
    values = (Missing("timeout", "a"), None)

    assert _interleave(client, "_last_failure", values) == list(values)


def test_a_thread_that_never_wrote_sees_the_default_not_another_threads_value():
    provider = GraphProvider.__new__(GraphProvider)
    provider._pending_rows = ({"name": "written-on-main"},)
    seen: list[Any] = []

    t = threading.Thread(target=lambda: seen.append(provider._pending_rows))
    t.start()
    t.join(timeout=5)

    assert seen == [()]


def test_two_providers_on_one_thread_keep_separate_state():
    a, b = GraphProvider.__new__(GraphProvider), GraphProvider.__new__(GraphProvider)
    a._pending_withheld = 4

    assert b._pending_withheld == 0


def test_the_harness_detects_shared_state():
    """The control. Without it, a barrier that silently never interleaved would pass every test
    above. A plain attribute must fail the same check the real fields pass."""
    class _Shared:
        value: Any = None

    class _Private:
        value: PerThread[Any] = PerThread(None)

    shared = _interleave(_Shared(), "value", ("a", "b"))
    assert shared[0] == shared[1], shared   # both threads read the last writer's value

    assert _interleave(_Private(), "value", ("a", "b")) == ["a", "b"]
