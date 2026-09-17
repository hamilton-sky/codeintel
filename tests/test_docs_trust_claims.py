"""`docs/trust.md` is the onboarding guide, and its claims must match the product.

It is the one doc written for someone who has read no others, which makes it the one whose drift
costs most: a reader who cannot check a claim against the source is the reader this file is for.
`test_docs_ci_claims.py` makes the same argument about the README's CI claims and gives the reason
drift is treated as a defect in BOTH directions — understating assurance is smaller than
overstating it, and is the same failure of a hand-written claim about a machine-readable fact.

So the facts are derived, never typed here:

* every safe-null `reason` the guide names must be one `provider.py` classifies;
* every `gaps` kind it names must be one some provider actually raises;
* every command it shows must parse against the real CLI — this is the strongest of the three,
  because a new user following the guide runs these literally;
* every degraded state must offer one concrete next action, which is the acceptance criterion the
  readiness doc set for this phase.
"""
from __future__ import annotations

import ast
import pathlib
import re
import shlex

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TRUST = ROOT / "docs" / "trust.md"
TEXT = TRUST.read_text(encoding="utf-8")


def _bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def _commands(text: str) -> list[str]:
    """Every `codeintel …` invocation the guide tells a reader to run.

    Comments are stripped and `&&` chains split, because the guide chains two commands in one line
    and a reader runs both.
    """
    out: list[str] = []
    for block in _bash_blocks(text):
        for raw in block.splitlines():
            line = raw.split("#", 1)[0].strip()
            for part in line.split("&&"):
                part = part.strip()
                if part.startswith("codeintel "):
                    out.append(part)
    return out


# --------------------------------------------------------------------------- the vocabularies

def _safe_null_reasons() -> set[str]:
    """Every `reason` string `safe_null_result` classifies, read out of its source."""
    import codeintel.provider as provider

    source = pathlib.Path(provider.__file__).read_text()
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "safe_null_result")
    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Set):
            found |= {e.value for e in node.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    assert len(found) > 20, f"the reason census looks broken, found {found}"
    return found


def _gap_kinds() -> set[str]:
    """Every gap kind some provider raises, plus the `Missing` kinds that become one."""
    import typing

    from codeintel.outcome import MissingKind

    kinds = set(typing.get_args(MissingKind))
    for path in sorted((ROOT / "src" / "codeintel").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_add_gap" and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)):
                kinds.add(str(node.args[1].value))
    assert len(kinds) > 10, f"the gap census looks broken, found {kinds}"
    return kinds


def test_every_reason_the_guide_names_is_one_the_product_emits():
    """A guide naming a `reason` the code cannot produce sends a reader looking for a state that
    does not exist — and they have no way to tell, which is the whole problem with this document."""
    real = _safe_null_reasons()
    named = set(re.findall(r'reason: "([a-z-]+)"', TEXT))
    assert named, "the guide names no reasons at all — has it stopped explaining safe-nulls?"
    assert named <= real, f"named in trust.md but not classified by provider.py: {named - real}"


def test_every_gap_kind_the_guide_names_is_one_some_provider_raises():
    kinds = _gap_kinds()
    named = set(re.findall(r"`(ancestor-scope|row-cap-reached|all-rows-name-resolved|"
                           r"low-confidence-edges|name-collisions-dropped|unresolvable|"
                           r"non-call-relationships|target-ambiguous)`", TEXT))
    assert named <= kinds, f"named in trust.md but raised nowhere: {named - kinds}"


# --------------------------------------------------------------------------- the commands

def test_every_command_the_guide_shows_actually_parses():
    """The strongest guard here: a new user runs these literally, and a flag that was renamed makes
    the first step of onboarding fail with a usage error."""
    from codeintel.__main__ import build_parser

    parser = build_parser()
    commands = _commands(TEXT)
    assert len(commands) >= 8, f"only found {commands} — the block extraction has broken"

    for command in commands:
        argv = shlex.split(command)[1:]            # drop the `codeintel` word itself
        try:
            parser.parse_args(argv)
        except SystemExit as exc:                  # argparse exits on a bad flag
            pytest.fail(f"`{command}` does not parse against the real CLI (exit {exc.code})")


def test_the_guide_does_not_invent_a_distribution_name():
    """`pip install <name>` is the first line a new user types, and the distribution is NOT called
    `codeintel` — that is the console script. Getting this wrong fails before anything else runs."""
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as fh:
        distribution = tomllib.load(fh)["project"]["name"]

    installs = re.findall(r"pip install ([A-Za-z0-9_.\-]+)", TEXT)
    assert installs, "the guide no longer tells a reader what to install"
    assert distribution in installs, (
        f"the guide installs {installs} but the distribution is `{distribution}`")


# --------------------------------------------------------------------------- the eight states

def _state_sections() -> dict[str, str]:
    """`{heading: body}` for each `### 3.N` state section."""
    parts = re.split(r"\n### (3\.\d[^\n]*)\n", TEXT)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_the_guide_covers_all_eight_repository_states():
    """The readiness doc names eight. Fewer means a reader meets a state the guide never described,
    which is exactly the position this phase exists to end."""
    sections = _state_sections()
    assert len(sections) == 8, f"expected 8 states, found {len(sections)}: {list(sections)}"


def test_every_degraded_state_names_one_concrete_next_action():
    """The readiness doc's acceptance criterion, verbatim: "Every common failure includes one
    concrete next action." Prose sympathy is not an action."""
    for heading, body in _state_sections().items():
        if "healthy" in heading.lower():
            continue                                # nothing to fix in the healthy state
        assert _bash_blocks(body), f"{heading} describes a degraded state and offers no command"


def test_the_guide_never_presents_heuristic_edges_as_authoritative():
    """The other acceptance criterion. Checked positively rather than by hunting for forbidden
    words: the guide must say, of the name-matched rows, that they need verifying."""
    row = next((ln for ln in TEXT.splitlines()
                if "name-matched" in ln and "|" in ln and "Graph" in ln), "")
    assert row, "the trust table no longer has a row for name-matched edges"
    assert "lead" in row.lower() and "verify" in row.lower(), row


def test_the_guide_states_the_rule_that_precedes_a_deletion():
    """The single most consequential thing a reader can get wrong, and the one every other doc in
    this tree traces its worst bug to: an empty answer and an unanswered lookup are not the same
    fact."""
    lowered = TEXT.lower()
    assert "confidence: complete" in lowered and "confidence: partial" in lowered, TRUST
    assert "unknown" in lowered and "never as none" in lowered, (
        "the partial-means-unknown rule is the one a reader must not miss")


# --------------------------------------------------------------------------- it has to be findable

def test_the_guide_is_listed_in_the_docs_index():
    """`docs/README.md` opens with "Anything not in this index does not exist". An onboarding guide
    nobody can find is the failure mode this phase is about, one level up."""
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "trust.md" in index, "docs/README.md does not list the onboarding guide"


def test_the_readme_points_a_new_user_at_it():
    """The README is where a stranger lands. It has to hand them this."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/trust.md" in readme, "README.md never mentions the onboarding guide"
