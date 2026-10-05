"""The Graphify arm: off by default, sealed from any model, keyed exactly like every other arm.

Graphify is a second engine measured against the same oracle. What makes that comparison worth
anything is not the adapter's cleverness but three properties, each pinned here:

* the arm does not exist unless asked for, so the benchmark everyone else runs is unchanged;
* Graphify reads a copy and cannot reach a model — it would otherwise write into the repository it
  measures, and could send a private tree to an LLM backend it auto-detects;
* its answers are keyed by WHERE the call is, through the same enclosing map the other arms use, so
  a difference in the table is a difference between engines and not between two key-builders.

None of these tests need Graphify installed: the end-to-end ones drive a fake `graphify` script.
POSIX only, because the fake is a shell script.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

BENCH = pathlib.Path(__file__).resolve().parent.parent / "bench"
sys.path.insert(0, str(BENCH))

import graphify_arm
import score
from score import Answer, graphify_answers

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake graphify is a shell script")

CORPUS_TS = BENCH / "fixtures" / "corpus_ts"


class _Lang:
    """The one seam `graphify_answers` uses: the symbol enclosing a call-site line."""

    def __init__(self, at: dict[tuple[str, int], str]):
        self.at = at

    def enclosing(self, root, rel_file, line):
        return self.at.get((rel_file, line))


def _index(nodes, links):
    return graphify_arm.GraphifyIndex.from_graph({"nodes": nodes, "links": links})


def _node(nid, label, file, line=1):
    return {"id": nid, "label": label, "source_file": file, "source_location": f"L{line}"}


def _edge(src, dst, relation="calls", confidence="EXTRACTED", file="a.py", line=1):
    return {"source": src, "target": dst, "relation": relation, "confidence": confidence,
            "source_file": file, "source_location": f"L{line}"}


# --------------------------------------------------------------------------- off unless asked for

def test_the_arm_is_off_unless_its_executable_is_named(monkeypatch, tmp_path):
    monkeypatch.delenv(graphify_arm.ENV, raising=False)
    assert graphify_arm.executable() is None

    monkeypatch.setenv(graphify_arm.ENV, str(tmp_path / "no-such-graphify"))
    assert graphify_arm.executable() is None, "a path that is not there must not switch the arm on"

    exe = tmp_path / "graphify"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv(graphify_arm.ENV, str(exe))
    assert graphify_arm.executable() == str(exe)


# --------------------------------------------------------------------------- sealed from any model

def test_graphify_inherits_nothing_and_finds_no_model_backend_on_its_path(monkeypatch, tmp_path):
    """A `uv tool install` shim lives in `~/.local/bin`, next to `claude`. Putting the shim's own
    directory on PATH would hand Graphify a backend it auto-detects, so the REAL directory is used."""
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(key, "sk-must-not-leak")
    shims = tmp_path / "local-bin"
    real = tmp_path / "tools" / "graphifyy" / "bin"
    shims.mkdir()
    real.mkdir(parents=True)
    (real / "graphify").write_text("#!/bin/sh\n")
    (shims / "claude").write_text("#!/bin/sh\n")
    (shims / "graphify").symlink_to(real / "graphify")

    env = graphify_arm.sealed_env(str(shims / "graphify"), str(tmp_path / "home"))

    assert set(env) == {"HOME", "PATH", "LANG"}, env
    assert str(shims) not in env["PATH"].split(":"), "the shim directory holds a model backend"
    assert str(real) in env["PATH"].split(":")
    assert env["HOME"] == str(tmp_path / "home")


# --------------------------------------------------------------------------- keyed like every arm

def test_a_call_is_keyed_by_where_it_is_and_only_extracted_edges_reach_the_extracted_arm():
    gi = _index(
        [_node("t", "target()", "lib.py"), _node("a", "a()", "a.py"), _node("b", "b()", "b.py")],
        [_edge("a", "t", file="a.py", line=4), _edge("b", "t", confidence="INFERRED", file="b.py", line=9)])
    lang = _Lang({("a.py", 4): "a", ("b.py", 9): "Holder.b"})

    every, extracted = graphify_answers(gi, lang, "/root", "lib.py", "target")

    assert every.callers == {("a.py", "a"), ("b.py", "Holder.b")}
    assert extracted.callers == {("a.py", "a")}, "an INFERRED edge is not Graphify's 'certain' label"


def test_a_method_target_is_found_through_its_class_in_either_edge_direction():
    gi = _index(
        [_node("c", "Chain", "chain.ts"), _node("m", ".resolve()", "chain.ts"),
         _node("other", "Other", "other.ts"), _node("om", ".resolve()", "other.ts"),
         _node("x", "route()", "agent.ts")],
        [{"source": "c", "target": "m", "relation": "method"},
         {"source": "om", "target": "other", "relation": "method"},          # reversed on purpose
         _edge("x", "m", file="agent.ts", line=3), _edge("x", "om", file="agent.ts", line=7)])
    lang = _Lang({("agent.ts", 3): "route", ("agent.ts", 7): "route2"})

    every, _ = graphify_answers(gi, lang, "/root", "chain.ts", "Chain.resolve")
    other, _ = graphify_answers(gi, lang, "/root", "other.ts", "Other.resolve")

    assert every.callers == {("agent.ts", "route")}, "a same-named method of another class leaked in"
    assert other.callers == {("agent.ts", "route2")}


def test_a_symbol_graphify_has_no_node_for_is_unanswered_never_an_empty_answer():
    gi = _index([_node("t", "target()", "lib.py")], [])

    every, extracted = graphify_answers(gi, _Lang({}), "/root", "lib.py", "missing")

    assert every.unavailable and extracted.unavailable
    assert "no node" in (every.reason or "")
    present, _ = graphify_answers(gi, _Lang({}), "/root", "lib.py", "target")
    assert not present.unavailable and present.callers == set(), "a node with no edges IS an answer"


def test_a_reference_is_change_impact_not_a_call_and_an_import_is_neither():
    gi = _index(
        [_node("t", "target()", "lib.py"), _node("a", "a()", "a.py")],
        [_edge("a", "t", relation="references", file="a.py", line=2),
         _edge("a", "t", relation="imports_from", file="a.py", line=1)])
    lang = _Lang({("a.py", 2): "a", ("a.py", 1): "<module>"})

    every, _ = graphify_answers(gi, lang, "/root", "lib.py", "target")

    assert every.callers == set()
    assert every.others == {("a.py", "a")}
    assert every.everything == {("a.py", "a")}, "an import does not break when a body moves"


def test_a_function_passed_as_a_value_is_change_impact_not_a_call():
    """Graphify records a callback or a dispatch-table entry as `indirect_call`, and its own
    blast-radius walk counts it. Dropping the relation charged Graphify for impact it reported."""
    gi = _index(
        [_node("t", "target()", "lib.py"), _node("d", "dispatch()", "d.py")],
        [_edge("d", "t", relation="indirect_call", file="d.py", line=6)])

    every, _ = graphify_answers(gi, _Lang({("d.py", 6): "dispatch"}), "/root", "lib.py", "target")

    assert every.callers == set(), "a function handed over as a value is not a call of it"
    assert every.others == {("d.py", "dispatch")}


# --------------------------------------------------------------------------- end to end, fake engine

_FAKE = r"""#!/bin/sh
# A stand-in for `graphify`: `--version`, and `update <dir> --no-cluster`, which writes a graph that
# records what this process could see — so the test reads the sealing back instead of assuming it.
if [ "$1" = "--version" ]; then echo "graphify 0.0-fake"; exit 0; fi
dir="$2"
keys=$(env | grep -c '_API_KEY=')
mkdir -p "$dir/graphify-out"
cat > "$dir/graphify-out/graph.json" <<JSON
{"nodes": [
  {"id": "sq", "label": "settleQueue()", "source_file": "src/settle.ts", "source_location": "L1"},
  {"id": "da", "label": "drainA()", "source_file": "src/callerSettleA.ts", "source_location": "L3"},
  {"id": "db", "label": "drainB()", "source_file": "src/callerSettleB.ts", "source_location": "L3"},
  {"id": "seen", "label": "keys=$keys home=$HOME", "source_file": "-", "source_location": "L1"}],
 "links": [
  {"source": "da", "target": "sq", "relation": "calls", "confidence": "EXTRACTED",
   "source_file": "src/callerSettleA.ts", "source_location": "L4"},
  {"source": "db", "target": "sq", "relation": "calls", "confidence": "INFERRED",
   "source_file": "src/callerSettleB.ts", "source_location": "L4"}]}
JSON
echo "[graphify watch] Rebuilt (no clustering): 4 nodes, 2 edges"
"""


def _fake_graphify(tmp_path) -> pathlib.Path:
    exe = tmp_path / "fakebin" / "graphify"
    exe.parent.mkdir()
    exe.write_text(_FAKE)
    exe.chmod(0o755)
    return exe


def test_build_reads_a_copy_never_the_tree_and_sees_no_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    exe = _fake_graphify(tmp_path)

    gi = graphify_arm.build(str(CORPUS_TS), str(exe))

    assert not (CORPUS_TS / "graphify-out").exists(), "graphify wrote into the tree it measures"
    seen = gi.nodes["seen"]["label"]
    assert seen.startswith("keys=0 "), f"an API key reached graphify: {seen}"
    assert "codeintel-bench-graphify-" in seen, f"graphify ran with the real HOME: {seen}"
    assert gi.summary.startswith("[graphify watch] Rebuilt")


def _stub_engines(monkeypatch):
    monkeypatch.setattr(score, "graph_answer", lambda *a, **k: (Answer(), Answer(), Answer()))
    monkeypatch.setattr(score, "lsp_answers", lambda *a, **k: (Answer(), Answer()))
    monkeypatch.setattr(score, "_provenance", lambda *a, **k: None)


def test_without_the_variable_the_benchmark_prints_its_five_arms_and_no_graphify_row(
        monkeypatch, capsys):
    """The default table grew a fifth arm, `graph_qualified`, and the point of this test is what did
    NOT change: no Graphify row appears unless the variable names its executable, and the arm field
    stays 16 columns wide (`graph_qualified` is 15 characters, so the longest default name still
    fits the original width)."""
    monkeypatch.delenv(graphify_arm.ENV, raising=False)
    _stub_engines(monkeypatch)

    score.run(str(CORPUS_TS), [("src/settle.ts", "settleQueue")], language="typescript", gated=False)
    out = capsys.readouterr().out

    assert "graphify" not in out
    assert "\ngraph_verified  " in out, "the default table's 16-column arm field changed width"
    table = [ln.split()[0] for ln in out.splitlines()
             if ln.split() and not ln.startswith(" ") and ln.split()[0].startswith(("graph", "lsp"))
             and " / " in ln]
    assert table == ["graph", "graph_verified", "graph_qualified", "lsp_raw", "lsp_classified"], out


def test_with_the_variable_graphify_is_scored_against_the_same_oracle(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(graphify_arm.ENV, str(_fake_graphify(tmp_path)))
    _stub_engines(monkeypatch)

    score.run(str(CORPUS_TS), [("src/settle.ts", "settleQueue")], language="typescript", gated=False)
    out = capsys.readouterr().out

    rows = {ln.split()[0]: ln.split()[1:] for ln in out.splitlines()
            if ln.startswith(("graphify ", "graphify_extracted "))}
    # settleQueue's two true callers are drainA and drainB: the fake records both, one EXTRACTED.
    assert rows["graphify"][:2] == ["100%", "100%"], out
    assert rows["graphify_extracted"][:2] == ["100%", "50%"], out
    assert "graphify: graphify 0.0-fake" in out, "the run does not say which graphify it measured"


def test_a_graphify_that_fails_to_build_skips_its_arms_and_says_why(monkeypatch, tmp_path, capsys):
    broken = tmp_path / "graphify"
    broken.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo x; exit 0; fi\necho boom >&2\nexit 3\n")
    broken.chmod(0o755)
    monkeypatch.setenv(graphify_arm.ENV, str(broken))
    _stub_engines(monkeypatch)

    score.run(str(CORPUS_TS), [("src/settle.ts", "settleQueue")], language="typescript", gated=False)
    out = capsys.readouterr().out

    assert "graphify: arms skipped" in out and "boom" in out, out
    assert not any(ln.startswith("graphify ") for ln in out.splitlines()), (
        "a build that failed was scored as an engine that found nothing")
    assert os.path.isdir(CORPUS_TS) and not (CORPUS_TS / "graphify-out").exists()


def test_a_graph_left_behind_by_a_failed_graphify_is_not_scored(monkeypatch, tmp_path):
    """A process can write `graph.json` and then fail — a later pipeline step, a killed wrapper.
    That graph may be partial, and scoring it would charge the engine for a half-built index."""
    exe = tmp_path / "graphify"
    exe.write_text(_FAKE + "\necho 'clustering failed' >&2\nexit 2\n")
    exe.chmod(0o755)

    with pytest.raises(RuntimeError, match="exited 2"):
        graphify_arm.build(str(CORPUS_TS), str(exe))
