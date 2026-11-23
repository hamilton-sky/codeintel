"""Caller counts that say what they count: imports, shadowing names, and how many symbols share a name.

Three findings, one story (an agent reading caller counts):

* `callers-collapse-to-module-scope`. A row whose caller is a file's module scope is the backend saying
  "this file references the symbol at module level" — usually the import line. The function inside that
  file that calls it is not resolved. It was folded into the heading's direct count, so `callers
  safe_null_result` read "14 direct" over four real calls. It is now counted as a file that imports the
  symbol, and the answer carries an `import-only-callers` gap.
* `usage-rows-from-shadowing-names`. Under a narrowed target (`Gateway.query`, `impl.process`), a USAGE
  row from a function whose only `query` is its own parameter is not a reference to the target. Its
  file neither writes the qualifier nor imports the target's module, so the row is dropped; a row whose
  file cannot be read is kept, but as name-matched.
* `ambiguous-count-wrong`. The banner over several same-named symbols claimed they were every symbol of
  that name in the index. It counts only the ones with edges, and says how many more exist. An
  unqualified target never gets a "Settle it" qualifier the caller did not write.
"""
from __future__ import annotations

import re

from tests.test_graph_failure_population import _gp
from tests.test_summary_integrity import _headline_disagreements, _sections

_TARGET_FILE = "pkg/core.py"


def _module_row(file: str, target: str = "safe_null_result", kind: str = "USAGE") -> dict:
    """A caller row whose caller is the backend's whole-file node for `file`'s module scope."""
    stem = file[:-3].replace("/", ".")
    return {
        "a.name": file.rsplit("/", 1)[-1], "a.qualified_name": f"proj.{stem}.__file__",
        "a.file_path": file, "labels(a)": '["File"]', "type(c)": kind,
        "b.name": target, "b.qualified_name": f"proj.pkg.core.{target}", "b.file_path": _TARGET_FILE,
    }


def _call_row(caller: str, file: str, target: str = "safe_null_result", *,
              target_qn: str = "proj.pkg.core.safe_null_result", target_file: str = _TARGET_FILE,
              kind: str = "CALLS", strategy: str | None = "import_map",
              conf: str | None = "0.95") -> dict:
    row = {
        "a.name": caller.rsplit(".", 1)[-1], "a.qualified_name": caller,
        "a.file_path": file, "labels(a)": '["Function"]', "type(c)": kind,
        "b.name": target, "b.qualified_name": target_qn, "b.file_path": target_file,
    }
    if strategy is not None:
        row["strategy"] = strategy
    if conf is not None:
        row["c.confidence"] = conf
    return row


def _ask(op: str, target: str, rows: list[dict], root: str = "/tmp/x", *, count=None,
         queries: list[str] | None = None) -> dict:
    """The envelope for `op target` over `rows`. `count` answers the symbol-count query: a callable
    given the cypher, or None to answer it with the edge rows like every other query."""
    def answer(cypher: str, project: str, timeout_ms: int) -> list[dict]:
        if queries is not None:
            queries.append(cypher)
        if count is not None and cypher.startswith("MATCH (n) ") and "count(" in cypher:
            return count(cypher)
        return [dict(r) for r in rows]

    return _gp(query_rows=answer).build_result(op, target, [], 30000, root)


def _gap_kinds(env: dict) -> list[str]:
    return [str(g["kind"]) for g in env.get("gaps") or []]


def _row_lines(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln.startswith("- ")]


# ============================================================== module scope is an import, not a call

def test_a_caller_only_at_module_scope_is_an_import_and_not_a_direct_call():
    """The `safe_null_result` shape: every production caller is a module-scope row. None of them is a
    direct call, the heading says how many files import it, and the answer says the call sites inside
    those files were not resolved."""
    env = _ask("callers", "safe_null_result",
               [_module_row("pkg/gateway.py"), _module_row("pkg/server.py")])
    body = str(env["result"])

    assert ("## Callers of safe_null_result (0 direct calls, 2 files import it — call sites inside "
            "not resolved)") in body, body
    assert "import-only-callers" in _gap_kinds(env), env["gaps"]
    assert env["confidence"] == "partial", env
    assert env["evidence"]["safe_for_destructive"] is False, env["evidence"]
    # The rows are what they always were: one location per importing file.
    assert _row_lines(body) == ["- module scope of pkg/gateway.py", "- module scope of pkg/server.py"]
    # And the heading's numbers still add up to the rows beneath it.
    assert _sections(body), body
    assert not _headline_disagreements(body), body


def test_direct_calls_and_importing_files_are_counted_apart():
    """Two function callers and three importing files: `2 direct calls, 3 files import it`, five rows."""
    rows = ([_call_row("pkg.t1.test_one", "pkg/t1.py"), _call_row("pkg.t2.test_two", "pkg/t2.py")]
            + [_module_row(f"pkg/{n}.py") for n in ("gateway", "server", "provider")])
    env = _ask("callers", "safe_null_result", rows)
    body = str(env["result"])

    assert re.search(r"\(2 direct calls, 3 files import it — call sites inside not resolved\)", body), body
    assert "import-only-callers" in _gap_kinds(env), env["gaps"]
    assert len(_row_lines(body)) == 5, body
    assert not _headline_disagreements(body), body
    gap = next(g for g in env["gaps"] if g["kind"] == "import-only-callers")
    assert "3" in gap["detail"] and gap["section"] == "callers", gap


def test_other_references_keep_their_own_count_beside_the_imports():
    """A CALL_REFERENCE row is still an "other reference", and one importing file is singular."""
    rows = [_call_row("pkg.t1.test_one", "pkg/t1.py"),
            _call_row("pkg.reg.register", "pkg/reg.py", kind="CALL_REFERENCE"),
            _module_row("pkg/gateway.py")]
    body = str(_ask("callers", "safe_null_result", rows)["result"])

    assert ("(1 direct call, 1 other reference(s), 1 file imports it — call sites inside not "
            "resolved)") in body, body
    assert not _headline_disagreements(body), body


def test_an_answer_with_no_module_scope_row_keeps_its_heading_and_has_no_import_gap():
    """CONTROL: no import row, no new wording and no new gap."""
    env = _ask("callers", "safe_null_result",
               [_call_row("pkg.t1.test_one", "pkg/t1.py"), _call_row("pkg.t2.test_two", "pkg/t2.py")])
    body = str(env["result"])

    assert "## Callers of safe_null_result (2)" in body, body
    assert "import-only-callers" not in _gap_kinds(env), env["gaps"]
    assert "import it" not in body, body


# ====================================================== USAGE rows under a narrowed target

def _shadow_tree(tmp_path) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/sub/__init__.py": "",
        "pkg/sub/impl.py": "def process(x):\n    return x\n",
        "main.py": "from pkg.sub.impl import process\n\n\ndef run():\n    return process(1)\n",
        "hier.py": "def param_shadow(process):\n    return process + 1\n",
        "uses.py": "from pkg.sub import impl\n\n\ndef via_module():\n    return impl.process\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _process_row(caller: str, file: str, **kw) -> dict:
    return _call_row(caller, file, "process", target_qn="proj.pkg.sub.impl.process",
                     target_file="pkg/sub/impl.py", **kw)


def _shadow_rows() -> list[dict]:
    return [
        _process_row("main.run", "main.py"),
        _process_row("hier.param_shadow", "hier.py", kind="USAGE", strategy=None, conf=None),
        _process_row("uses.via_module", "uses.py", kind="USAGE", strategy=None, conf=None),
    ]


def test_a_parameter_that_shadows_the_name_is_not_a_reference_under_a_narrowed_target(tmp_path):
    """`def param_shadow(process)` mentions only its own parameter. `hier.py` neither writes `impl`
    nor imports the module that defines `pkg.sub.impl.process`, so its USAGE row is dropped. CONTROL:
    `uses.py` imports `pkg.sub.impl` and keeps its USAGE row; the real call stays."""
    _shadow_tree(tmp_path)
    env = _ask("callers", "impl.process", _shadow_rows(), str(tmp_path))
    body = str(env["result"])
    names = [r["qualified_name"] for r in env["rows"]]

    assert "hier.param_shadow" not in names, names
    assert not any("param_shadow" in ln for ln in _row_lines(body)), body
    assert names == ["main.run", "uses.via_module"], names
    assert "(1 direct, 1 other reference(s))" in body, body
    assert not _headline_disagreements(body), body
    # The drop is counted in the body, and the dropped row is named nowhere in it.
    assert "1 `USAGE` row(s) dropped" in body, body
    assert "param_shadow" not in body, body
    # The "Narrowed by" sentence is now true of every row listed.
    assert "Narrowed by the qualified name `impl.process`" in body, body


def test_the_same_row_under_a_bare_target_is_left_alone(tmp_path):
    """CONTROL: without a qualifier there is nothing to narrow by, so nothing is dropped."""
    _shadow_tree(tmp_path)
    env = _ask("callers", "process", _shadow_rows(), str(tmp_path))
    names = [r["qualified_name"] for r in env["rows"]]

    assert "hier.param_shadow" in names, names


def test_a_usage_row_whose_file_cannot_be_read_is_name_matched_and_not_a_direct_call(tmp_path):
    """No file to read means no verdict: the row stays, badged name-matched, out of the direct count."""
    _shadow_tree(tmp_path)
    rows = [*_shadow_rows(),
            _process_row("gone.missing", "gone.py", kind="USAGE", strategy=None, conf=None)]
    env = _ask("callers", "impl.process", rows, str(tmp_path))
    body = str(env["result"])
    by_name = {r["qualified_name"]: r for r in env["rows"]}

    assert "gone.missing" in by_name, by_name
    assert by_name["gone.missing"]["evidence"] == "name-matched", by_name["gone.missing"]
    assert by_name["gone.missing"]["verified"] is False
    assert "(1 direct, 2 other reference(s))" in body, body
    assert "1 `USAGE` row(s) could not be checked" in body, body
    assert env["evidence"]["safe_for_destructive"] is False


def test_the_usage_note_counts_the_rows_it_dropped_and_the_rows_it_could_not_judge(tmp_path):
    """The verifier for the narrowing note's tallies: N dropped is exactly the USAGE rows that left
    `rows[]`, and N unchecked is exactly the USAGE rows still there and now name-matched."""
    _shadow_tree(tmp_path)
    (tmp_path / "hier2.py").write_text("def other(process):\n    return process\n")
    rows = [
        *_shadow_rows(),
        _process_row("hier2.other", "hier2.py", kind="USAGE", strategy=None, conf=None),
        _process_row("gone.a", "gone_a.py", kind="USAGE", strategy=None, conf=None),
        _process_row("gone.b", "gone_b.py", kind="USAGE", strategy=None, conf=None),
        _process_row("gone.c", "gone_c.py", kind="USAGE", strategy=None, conf=None),
    ]
    env = _ask("callers", "impl.process", rows, str(tmp_path))
    body = str(env["result"])
    listed = {r["qualified_name"] for r in env["rows"]}
    asked = {r["a.qualified_name"] for r in rows}
    dropped = asked - listed
    unjudged = [r for r in env["rows"]
                if r["edge"] == "USAGE" and r["evidence"] == "name-matched"]

    assert dropped == {"hier.param_shadow", "hier2.other"}, dropped
    assert f"{len(dropped)} `USAGE` row(s) dropped" in body, body
    assert f"{len(unjudged)} `USAGE` row(s) could not be checked" in body, body
    assert len(unjudged) == 3


# ============================================================================ the ambiguity banner

def _runs(n: int, *, strategy: str = "import_map", conf: str = "0.95", per: int = 1) -> list[dict]:
    return [_call_row(f"main{i}.caller{j}", f"main{i}.py", "run",
                      target_qn=f"proj.mod{i}.run", target_file=f"mod{i}.py",
                      strategy=strategy, conf=conf)
            for i in range(n) for j in range(per)]


def test_the_banner_counts_the_symbols_with_edges_and_the_ones_without():
    """Five `run` symbols have callers; the index holds eighteen symbols named `run`."""
    queries: list[str] = []
    env = _ask("callers", "run", _runs(5), count=lambda cypher: [{"symbols": "18"}],
               queries=queries)
    body = str(env["result"])

    assert "5 symbols named `run` have caller edges; 13 more named `run` exist with none" in body, body
    assert "distinct symbols in this index" not in body, body
    counted = [q for q in queries if q.startswith("MATCH (n) ") and "count(" in q]
    assert len(counted) == 1, queries
    assert 'n.name="run"' in counted[0], counted


def test_the_banner_quotes_the_name_it_counts():
    """The name goes into the query through `_cypher_literal`, as every other query here does."""
    queries: list[str] = []
    rows = [dict(r, **{"b.name": 'r"un'}) for r in _runs(2)]
    _ask("callers", 'r"un', rows, count=lambda cypher: [{"symbols": "2"}], queries=queries)
    counted = [q for q in queries if q.startswith("MATCH (n) ") and "count(" in q]

    assert counted and 'n.name="r\\"un"' in counted[0], counted


def test_a_count_that_fails_leaves_the_number_out_and_says_others_may_exist():
    """No number is better than a wrong one: the banner says others may exist."""
    for reply in ([], [{"symbols": "many"}], [{"symbols": "3"}, {"symbols": "4"}]):
        env = _ask("callers", "run", _runs(3), count=lambda cypher, reply=reply: reply)
        body = str(env["result"])

        assert "3 symbols named `run` have caller edges; others may exist" in body, body
        assert "more named `run` exist" not in body, body
        # An optional count that failed is not a failure of the answer.
        assert "Incomplete:" not in body, body


def test_the_ambiguity_banner_counts_the_symbols_it_lists_and_the_rest_from_the_index():
    """The verifier for the banner's tally: N is the number of per-symbol sections printed, and N + M
    is exactly what the count query answered."""
    env = _ask("callers", "run", _runs(4), count=lambda cypher: [{"symbols": "11"}])
    body = str(env["result"])
    sections = [ln for ln in body.splitlines() if ln.startswith("### ")]
    m = re.search(r"(\d+) symbols named `run` have caller edges; (\d+) more named `run` exist", body)

    assert m, body
    assert int(m.group(1)) == len(sections) == 4, body
    assert int(m.group(1)) + int(m.group(2)) == 11, body


def test_an_unqualified_target_never_prints_a_settle_it_qualifier():
    """`callers run` named no qualifier. Taking one from the first group's backend name
    (`mod0`) and calling it "the name it is qualified by" invents a fact about the question."""
    env = _ask("callers", "run", _runs(2, strategy="unique_name", conf="0.55", per=3),
               count=lambda cypher: [{"symbols": "2"}])
    body = str(env["result"])

    assert "name-matched" in str(env["rows"]), env["rows"]
    assert "Settle it" not in body, body
    assert "is the name it is qualified by" not in body, body
    assert "is the module it is defined in" not in body, body


def test_the_headline_check_reads_the_import_shape_and_can_fail_on_it():
    """The aggregate verifier parses the new heading. NEGATIVE CONTROL: a heading whose numbers do not
    add up to the rows beneath it is reported."""
    good = ("## Callers of x (1 direct call, 2 files import it — call sites inside not resolved)\n"
            "- pkg.a (a.py)\n- module scope of b.py\n- module scope of c.py")
    bad = ("## Callers of x (1 direct call, 3 files import it — call sites inside not resolved)\n"
           "- pkg.a (a.py)\n- module scope of b.py")
    assert [claimed for _h, _l, claimed, _r in _sections(good)] == [3]
    assert not _headline_disagreements(good)
    assert _headline_disagreements(bad) == ["'Callers of x' claims 4, 2 row(s) beneath it"]
