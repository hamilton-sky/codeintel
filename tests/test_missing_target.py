"""A question that was never asked must not be answered as though it were.

An external evaluator ran a dependency analysis against this tool and never got one usable
`code.query` result in the whole session. Every call came back `outcome: "not_found"`,
`reason: "not-in-graph"`, with a hint reading:

    `` is not in the graph index for this project — if you just added or renamed it,
    refresh with: codeintel index ~/Documents/aws-resource-tools

Empty backticks, about a symbol the graph backend could find on request, and a remediation they
then ran twice for nothing. The cause was one argument: they passed the symbol as `q`, this tool's
parameter is `target`, and MCP builds its argument model from the tool signature with pydantic's
default `extra="ignore"` — so the unknown key was dropped in silence and `target` took its empty
default.

What that cost is the reason these tests exist. Believing the index broken, they completed the task
by going around `code.query` to the raw graph backend, which has none of the cross-language
collision filtering `graph_answer._drop_edge_collisions` applies here — and it duly reported `.tsx`
files "calling" a Python class's methods. They wrote that up as this project's most damaging defect.
It is a defect of the backend that this wrapper already fixes, and they never saw the fix, because
a missing argument was reported as a fact about their code.

So: a blank target is its own reason, classified `unavailable` rather than `not_found`, and the
hint names the parameter.
"""
from __future__ import annotations

import pytest

from codeintel.gateway import Gateway
from codeintel.policy import TieringPolicy
from codeintel.provider import Result, safe_null_result
from codeintel.providers.graph import GraphProvider
from codeintel.query_ops import OPS_REQUIRING_A_TARGET, QUERY_OPS, TARGETLESS_OPS

ROOT = "/Users/x/Documents/project/codeintel"
LIST_PROJECTS = {"projects": [{"name": "codeintel", "root_path": ROOT}]}


class _CountingProvider:
    """Answers anything, and records that it was asked. The point of most of these tests is that
    it is NOT asked: a malformed call must cost no dispatch, no backend round-trip and no index."""

    available = True

    def __init__(self, engine_name: str = "graph") -> None:
        self._engine_name = engine_name
        self.call_count = 0

    def build_result(self, op, target, files, budget, project_root) -> Result:
        self.call_count += 1
        return {
            "ok": True, "op": str(op or ""), "target": str(target or ""),
            "result": "an answer", "engine": self._engine_name, "cached": False,
        }


def _gateway() -> tuple[Gateway, _CountingProvider]:
    graph = _CountingProvider("graph")
    return Gateway(graph=graph, lsp=_CountingProvider("lsp")), graph


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(OPS_REQUIRING_A_TARGET))
def test_every_op_that_needs_a_target_says_so_when_it_does_not_get_one(op):
    """Not just `callers`. The evaluator hit this on `callers` and on `symbol`, and wrote the
    second one up as a separate defect of the LSP engine — it was this same one argument."""
    gw, graph = _gateway()
    env = gw.query(op=op, target="", project_root=ROOT)

    assert env["result"] is None
    assert env["reason"] == "no-target", env
    assert graph.call_count == 0, "a malformed call must not reach an engine"


@pytest.mark.parametrize("op", sorted(TARGETLESS_OPS))
def test_the_ops_that_answer_for_the_whole_repo_are_untouched(op):
    """The negative control, and the one way this guard could do real damage: `overview`,
    `changed` and `hotspots` are DOCUMENTED as ignoring `target` (`_TARGET_FIELD_DESCRIPTION` in
    server.py), so rejecting them for not carrying one would break the documented call."""
    gw, graph = _gateway()
    env = gw.query(op=op, target="", project_root=ROOT)

    assert env.get("reason") != "no-target", env
    assert env["result"] == "an answer"
    assert graph.call_count == 1


def test_a_whitespace_target_is_a_missing_one():
    """`target=" "` is the same mistake wearing a space, and it reaches the graph lookup as a
    search for nothing exactly like the empty string does."""
    gw, graph = _gateway()
    env = gw.query(op="callers", target="   ", project_root=ROOT)

    assert env["reason"] == "no-target", env
    assert graph.call_count == 0


def test_a_real_target_still_gets_a_real_answer():
    """The other negative control: the guard must be invisible to every well-formed call."""
    gw, graph = _gateway()
    env = gw.query(op="callers", target="validate_contract", project_root=ROOT)

    assert env["result"] == "an answer"
    assert env.get("reason") is None
    assert graph.call_count == 1


# ---------------------------------------------------------------------------
# What the envelope says, which is the whole point
# ---------------------------------------------------------------------------

def test_a_missing_argument_is_unavailable_and_never_not_found():
    """The classification is the fix. `not_found` is what an agent is instructed to read as
    "nothing found / not indexed yet", and this project's central claim is that it never confuses
    that with "could not ask" — the difference decides whether deleting a symbol is safe. A
    missing argument is the second, and it sits beside `no-project-root`, the same miss on the
    other argument."""
    assert safe_null_result("callers", "", reason="no-target")["outcome"] == "unavailable"
    assert safe_null_result("callers", "", reason="no-project-root")["outcome"] == "unavailable"

    gw, _ = _gateway()
    assert gw.query(op="callers", target="", project_root=ROOT)["outcome"] == "unavailable"


def test_the_hint_names_the_parameter_and_refuses_the_reindex_advice():
    """The regression guard on the wording, because the wording is what failed.

    Three things it must do, each of which the old answer got wrong: name `target`, so a caller
    who spelled it `q` is told what to spell instead; say that re-indexing will not help, because
    they re-indexed twice; and not print empty backticks around a symbol that was never named."""
    gw, _ = _gateway()
    hint = gw.query(op="callers", target="", project_root=ROOT)["hint"]

    assert "`target`" in hint, hint
    assert "`q`" in hint, "the argument name that was actually passed must be named"
    assert "re-indexing will not change it" in hint, hint
    assert "codeintel index" not in hint, "the remediation that cost the evaluator two runs"
    assert "``" not in hint, "the empty backticks that hid the cause"


def test_the_hint_lists_the_ops_that_need_no_target():
    gw, _ = _gateway()
    hint = gw.query(op="callers", target="", project_root=ROOT)["hint"]
    for op in TARGETLESS_OPS:
        assert op in hint, hint


# ---------------------------------------------------------------------------
# Ordering against the other guards on this path
# ---------------------------------------------------------------------------

def test_rbac_is_still_checked_before_the_arguments():
    """A denied role must not learn which arguments would have been well-formed, and must not be
    told a different story about the same call depending on how it was spelled. The policy check
    keeps its place at the top of `_query`."""
    policy = TieringPolicy(enabled=True, rules={"reader": ["overview"]}, roots={"reader": ["*"]})
    gw = Gateway(graph=_CountingProvider("graph"), policy=policy)

    env = gw.query(op="callers", target="", role="reader", project_root=ROOT)
    assert env["reason"] == "op-not-allowed-for-role", env


def test_the_guard_runs_before_any_indexing_work():
    """`maybe_reindex` walks the repository. A call that cannot be answered must not trigger it —
    the evaluator's malformed calls each kicked off work on a 2,000-file monorepo."""
    class _LoudReindexer:
        def __init__(self) -> None:
            self.calls = 0

        def maybe_reindex(self, root: str) -> None:
            self.calls += 1

    reindexer = _LoudReindexer()
    gw = Gateway(graph=_CountingProvider("graph"), reindexer=reindexer)
    gw.query(op="callers", target="", project_root=ROOT)

    assert reindexer.calls == 0


# ---------------------------------------------------------------------------
# The graph provider's own hint, reachable without the gateway
# ---------------------------------------------------------------------------

def _graph_provider(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects" else None))
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: [])
    return p


def test_the_graph_hint_never_prints_empty_backticks(monkeypatch):
    """The gateway now rejects a blank target before dispatch, so this line is unreachable from
    the query path — but a caller holding the provider directly still reaches it, and the sentence
    it produced is the one that started the whole misdiagnosis."""
    env = _graph_provider(monkeypatch).build_result("callers", "", [], 30000, ROOT)

    assert env["reason"] == "not-in-graph"
    assert "``" not in env["hint"], env["hint"]
    assert "that symbol" in env["hint"], env["hint"]


def test_the_reindex_hint_names_the_mcp_tool_as_well_as_the_cli(monkeypatch):
    """The reader is usually an agent with no shell. `codeintel index` is a CLI; an agent connected
    over MCP has `index_repository` in its own toolset and one call does the same job. Which of the
    two is available is not something this process can see, so it prints both."""
    env = _graph_provider(monkeypatch).build_result("callers", "nope", [], 30000, ROOT)

    assert "codeintel index" in env["hint"], env["hint"]
    assert "index_repository" in env["hint"], env["hint"]
    assert "incremental=true" in env["hint"], env["hint"]


def test_the_mcp_remediation_does_not_hand_back_a_path_no_shell_will_expand(monkeypatch):
    """The trap this hint walked into on the first attempt, caught by running it.

    `redact` rewrites this process's home directory to `~` in every field that reaches a caller,
    and its docstring gives the reason: the hints carry runnable commands and a shell expands `~`
    back. A JSON tool argument has no shell. So spelling the MCP form as
    `index_repository(repo_path="~/Documents/project/app")` ships a remediation that cannot run —
    strictly worse than the CLI-only hint it was added to improve. The MCP form points at the root
    the caller already passed instead of printing a redacted copy of it."""
    from codeintel.gateway import Gateway

    gw = Gateway(graph=_graph_provider(monkeypatch))
    hint = gw.query(op="callers", target="nope", project_root=ROOT)["hint"]

    mcp_form = hint.split("index_repository", 1)[1]
    assert "~" not in mcp_form, (
        "the MCP remediation must not carry a `~` path — nothing expands it on that transport: "
        + mcp_form
    )


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

def test_the_targetless_op_set_matches_the_graph_providers_own():
    """`query_ops.TARGETLESS_OPS` drives the gateway's argument check;
    `providers/graph._ROOT_SCOPED_OPS` drives the renderer. They are two copies of one fact,
    kept apart because `query_ops` is deliberately import-free (`codeintel --help` must not pay
    for the graph provider), so nothing but this test keeps them honest.

    Compared over the intersection with `QUERY_OPS`: `_ROOT_SCOPED_OPS` also carries `changes`
    (an alias) and `deadcode` (withdrawn), neither of which is a `code.query` op, and neither of
    which this set should grow."""
    from codeintel.providers.graph import _ROOT_SCOPED_OPS

    theirs = _ROOT_SCOPED_OPS & frozenset(QUERY_OPS)
    ours = frozenset(TARGETLESS_OPS)
    assert ours == theirs, (
        "codeintel.query_ops.TARGETLESS_OPS and providers/graph._ROOT_SCOPED_OPS have drifted — "
        f"only in TARGETLESS_OPS: {sorted(ours - theirs)}; "
        f"only in _ROOT_SCOPED_OPS: {sorted(theirs - ours)}."
    )


def test_the_two_op_sets_partition_the_vocabulary():
    """Every op is in exactly one of them. A new op added to `QUERY_OPS` and nowhere else lands in
    `OPS_REQUIRING_A_TARGET` by construction, which is the safe default — it gets an argument check
    it may not need, rather than silently skipping one it does."""
    assert OPS_REQUIRING_A_TARGET | frozenset(TARGETLESS_OPS) == frozenset(QUERY_OPS)
    assert not (OPS_REQUIRING_A_TARGET & frozenset(TARGETLESS_OPS))
