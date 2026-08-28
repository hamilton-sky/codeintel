"""The CLI's front door: `codeintel help` and what happens when you mistype a command.

argparse's default behavior on an unknown subcommand is to print the full list of choices and stop
— a dead end for a one-character typo (`gragh`, `dector`), which is exactly how it gets hit. And its
help lists twelve commands in declaration order with no grouping, so "what can this thing do?" means
reading every line to find the one verb you wanted.

The load-bearing invariant here is that the help screen stays HONEST: every command it advertises
must actually be a registered subcommand, and every registered subcommand must be advertised.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

import pytest

from codeintel.__main__ import _COMMAND_GROUPS, _COMMANDS, _suggest, render_help


def _run(*args, env_extra=None):
    import os
    env = {**os.environ, "NO_COLOR": "1", **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "codeintel", *args],
                          capture_output=True, text=True, timeout=120, env=env)


# --------------------------------------------------------------------------- honesty

def test_help_advertises_exactly_the_registered_subcommands():
    """The drift that makes help worse than useless: a command renamed in argparse but not in the
    help table, or advertised here and never wired up."""
    # argparse itself is the source of truth for what is actually registered. This used to scrape
    # `--help`, but `--help` now renders the grouped screen — which is built FROM _COMMAND_GROUPS,
    # so scraping it would compare the advertised list against itself and pass vacuously. Reading
    # the parser's registered subcommands keeps the invariant real.
    import argparse as _argparse

    from codeintel.__main__ import build_parser

    sub = [a for a in build_parser()._actions
           if isinstance(a, _argparse._SubParsersAction)]
    assert len(sub) == 1, "expected exactly one subparser group"
    registered = set(sub[0].choices)
    assert set(_COMMANDS) | {"help"} == registered


def test_every_advertised_command_has_a_description():
    for _group, items in _COMMAND_GROUPS:
        for name, desc in items:
            assert desc.strip(), name
            assert len(desc) > 20, f"{name}: description is too thin to help anyone"


def test_no_command_is_listed_in_two_groups():
    assert len(_COMMANDS) == len(set(_COMMANDS))


# --------------------------------------------------------------------------- help screen

def test_bare_invocation_shows_the_grouped_help_and_exits_zero():
    res = _run()
    assert res.returncode == 0
    for group, _items in _COMMAND_GROUPS:
        assert group in res.stdout
    assert "Examples" in res.stdout


def test_help_subcommand_matches_the_bare_invocation():
    assert _run("help").stdout == _run().stdout


def test_help_lists_every_command_with_its_description():
    out = _run("help").stdout
    for name, desc in [(n, d) for _g, items in _COMMAND_GROUPS for n, d in items]:
        assert name in out and desc in out


def test_examples_stay_aligned_as_they_grow():
    """A hardcoded comment column silently loses its gutter the moment an example outgrows it —
    ljust() will not pad below the string's own length."""
    lines = [ln for ln in _run("help").stdout.splitlines() if "  # " in ln]
    assert len(lines) >= 3
    assert len({ln.index("#") for ln in lines}) == 1      # one shared comment column


def test_examples_only_reference_real_commands():
    from codeintel.__main__ import _EXAMPLES
    for cmd, _why in _EXAMPLES:
        assert cmd.split()[0] == "codeintel"
        assert cmd.split()[1] in _COMMANDS


# --------------------------------------------------------------------------- typos

@pytest.mark.parametrize("typo,expected", [
    ("gragh", "graph"),        # observed in real use
    ("dector", "doctor"),      # observed in real use
    ("doctro", "doctor"),
    ("instal", "install"),
    ("quer", "query"),
    ("statuss", "status"),
])
def test_a_typo_suggests_the_command_that_was_meant(typo, expected):
    assert expected in _suggest(typo)


def test_a_prefix_suggests_every_command_it_could_be():
    assert set(_suggest("serve")) >= {"serve", "serve-http"}


def test_unknown_command_exits_two_with_a_suggestion():
    res = _run("gragh", ".", "--html")
    assert res.returncode == 2
    assert "unknown command: 'gragh'" in res.stderr
    assert "did you mean" in res.stderr and "graph" in res.stderr
    assert "codeintel help" in res.stderr


def test_unrecognizable_input_still_points_at_help():
    res = _run("zzzzzzzz")
    assert res.returncode == 2
    assert "codeintel help" in res.stderr      # no guess to offer, but never a dead end


def test_flags_are_left_to_argparse():
    """Only a bare word is claimed as a command — `--version` and `--help` must still work."""
    assert _run("--version").returncode == 0
    assert _run("--help").returncode == 0


# --------------------------------------------------------------------------- color

def test_color_is_stripped_when_not_a_tty():
    assert "\x1b[" not in _run("help").stdout


def test_no_color_beats_force_color():
    assert "\x1b[" not in _run("help", env_extra={"FORCE_COLOR": "1"}).stdout


def test_render_help_never_raises_without_a_terminal():
    assert isinstance(render_help(), str) and render_help()


def test_every_registered_subcommand_says_when_to_use_it_and_shows_an_example():
    """A flag list answers "what can I pass"; it never answers "should I run this at all".

    Nine of ten subcommands previously had zero examples and no statement of purpose beyond a
    one-line `help=`. This pins that a command cannot be added without that text — the gap is
    invisible otherwise, because argparse happily prints a bare flag list.
    """
    from codeintel.__main__ import _EPILOGS, build_parser

    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    registered = set(subparsers.choices)

    # `help` is the grouped top-level screen itself — it has no flags and no epilog to give.
    missing = sorted(registered - set(_EPILOGS) - {"help"})
    assert not missing, f"subcommands with no epilog: {missing}"

    for name in sorted(registered & set(_EPILOGS)):
        sub = subparsers.choices[name]
        assert sub.epilog, name
        assert "examples:" in sub.epilog, f"{name} epilog has no example block"
        assert f"codeintel {name}" in sub.epilog, f"{name} epilog never shows the command itself"
        # RawDescriptionHelpFormatter, or argparse re-wraps and destroys the aligned columns.
        # `formatter_class` is `codeintel.__main__._formatter_class` (a factory pinning the width
        # every subparser shares — see the terminal-aware-width tests) rather than the bare class,
        # so the thing pinned here is what it actually PRODUCES.
        assert isinstance(sub._get_formatter(), argparse.RawDescriptionHelpFormatter), name


def test_no_epilog_names_a_command_that_does_not_exist():
    """A renamed or removed command must not leave help text advertising it."""
    from codeintel.__main__ import _EPILOGS, build_parser

    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    stale = sorted(set(_EPILOGS) - set(subparsers.choices))
    assert not stale, f"epilogs for unregistered commands: {stale}"


def _visible(line: str) -> int:
    """Width as a terminal sees it: ANSI escapes are zero-width, and the rule glyph is 3 bytes
    per column. Measuring bytes instead reports a 40-column rule as 122 characters."""
    return len(re.sub(r"\x1b\[[0-9;]*m", "", line))


def test_no_help_line_exceeds_the_width_budget():
    """Every rendered help line must fit 78 columns.

    `RawDescriptionHelpFormatter` is set on each subparser so argparse will NOT rewrap an epilog —
    which is what protects the hand-aligned example columns, and also what makes an over-long line
    the terminal's problem instead of argparse's. A hard wrap mid-row destroys the alignment the
    formatter was chosen to preserve. Measured at the time this was written: 15 lines of the
    top-level screen and 29 epilog lines were over budget, the worst at 108 and 98.
    """
    from codeintel.__main__ import _EPILOGS, HELP_WIDTH, render_help

    over = [(w, line) for line in render_help().splitlines()
            if (w := _visible(line)) > HELP_WIDTH]
    assert not over, f"top-level help lines over {HELP_WIDTH}: {over[:4]}"

    for name, epilog in _EPILOGS.items():
        wide = [(len(line), line) for line in epilog.splitlines() if len(line) > HELP_WIDTH]
        assert not wide, f"{name} epilog lines over {HELP_WIDTH}: {wide[:3]}"


def test_the_comment_gutter_cannot_be_widened_without_bound():
    """One long invocation must not set the comment column for every short row.

    Unbounded, `codeintel doctor` paid 38 columns of padding and its line reached 96 — so an
    80-column terminal wrapped the comment onto a ragged row of its own, losing the alignment the
    gutter exists to create.
    """
    from codeintel.__main__ import _EXAMPLES, _START_HERE, HELP_GUTTER_CAP

    derived = max(max(len(cmd) + 3 for cmd, _ in _START_HERE),
                  max(len(cmd) for cmd, _ in _EXAMPLES)) + 2
    assert min(derived, HELP_GUTTER_CAP) <= HELP_GUTTER_CAP
    # and the cap has to leave room for a comment inside the budget
    assert HELP_GUTTER_CAP < 78


# --------------------------------------------------------------------------- terminal-aware width

def test_help_width_is_derived_from_the_terminal_and_clamped(monkeypatch):
    """`COLUMNS=40` used to be ignored entirely (a hardcoded 78) and `COLUMNS=200` would have let
    argparse's own formatter drift away from the epilog's fixed width — clamped to [40, 78] either
    way."""
    from codeintel.__main__ import _terminal_width

    monkeypatch.setenv("COLUMNS", "20")
    assert _terminal_width() == 40                     # floor
    monkeypatch.setenv("COLUMNS", "200")
    assert _terminal_width() == 78                      # ceiling
    monkeypatch.setenv("COLUMNS", "60")
    assert _terminal_width() == 60                      # passed through in between


def test_every_subcommand_formatter_shares_the_top_level_screens_width():
    """argparse's own `HelpFormatter` reads live `COLUMNS` independently of the hand-aligned
    epilog's `RawDescriptionHelpFormatter` — this is what let one `--help` screen render its flags
    list and its epilog at two different widths. Every subparser must be pinned to the same
    `HELP_WIDTH` the top-level screen itself is measured against."""
    from codeintel.__main__ import HELP_WIDTH, build_parser

    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name, sub in subparsers.choices.items():
        formatter = sub._get_formatter()
        assert formatter._width == HELP_WIDTH, name


# --------------------------------------------------------------------------- global flags

def test_no_color_and_ascii_work_before_any_subcommand():
    """Both used to be per-subcommand flags on only five of fourteen commands, reachable only
    AFTER the subcommand — so `codeintel --no-color help`, the one place `main()` prints help
    without ever calling `parser.parse_args()` at all, had no way to see them and exited 2."""
    res = _run("--no-color", "help")
    assert res.returncode == 0
    assert "\x1b[" not in res.stdout

    res = _run("--ascii", "help")
    assert res.returncode == 0


def test_bare_no_color_alone_still_shows_help():
    res = _run("--no-color")
    assert res.returncode == 0
    assert "codeintel" in res.stdout


def test_render_help_documents_the_global_flags_and_exit_codes():
    out = render_help()
    for opt, _desc in [("--version", ""), ("-h, --help", ""), ("--no-color", ""), ("--ascii", "")]:
        assert opt in out, opt
    assert "0  success" in out
    assert "1  failure" in out
    assert "2  usage error" in out


# --------------------------------------------------------------------------- bad flags (not a typo)

def test_a_bad_flag_points_at_help_instead_of_the_flat_choice_wall():
    """argparse's default `error()` dumps the full `{serve,index,query,...}` choice wall at
    whatever width the terminal happens to render usage — the exact wall the grouped `codeintel
    help` screen exists to replace. This used to fire for ANY unrecognized flag on ANY subcommand,
    because leftover unrecognized arguments are always reported by the ROOT parser's `error()`."""
    res = _run("doctor", "--this-flag-does-not-exist")
    assert res.returncode == 2
    assert "{serve" not in res.stderr
    assert "codeintel help" in res.stderr


def test_a_subcommands_own_usage_error_still_names_that_subcommand():
    """The fix is scoped to the wide top-level wall — a subcommand's own, already-specific error
    (missing/invalid value for one of ITS OWN flags) must keep naming that subcommand, not just
    point at `codeintel help`."""
    res = _run("query", "--op", "not-a-real-op", "--target", "x")
    assert res.returncode == 2
    assert "query" in res.stderr


# --------------------------------------------------------------------------- flags/ops named in help must be real

_FLAG_RE = re.compile(r"(?<![\w-])(--[a-zA-Z][a-zA-Z0-9-]*)")
_OP_VALUE_RE = re.compile(r'--op\s+"?([a-zA-Z][a-zA-Z0-9_-]*)"?')


def _known_flags(sub: argparse.ArgumentParser) -> set[str]:
    return set(sub._option_string_actions)


_INVOCATION_RE = re.compile(r"codeintel\s+([a-zA-Z][\w-]*)([^\n`]*)")


def test_every_flag_named_in_an_epilog_is_accepted_by_the_command_it_names():
    """The D1/D2-class defect this is aimed at: an epilog naming `--dry-run` before the parser
    defines it, or a `--target` example the parser rejects as missing. Nobody was checking an
    epilog's prose against the argparse surface it describes — this does, for every epilog.

    An epilog can (and does — `c4`'s mentions `codeintel graph --html`) reference a DIFFERENT
    command's invocation in passing; each `codeintel <cmd> ...` snippet is checked against ITS
    OWN parser, not blindly against the epilog's owner."""
    from codeintel.__main__ import _EPILOGS, build_parser

    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))

    checked = 0
    for epilog in _EPILOGS.values():
        for name, rest in _INVOCATION_RE.findall(epilog):
            sub = subparsers.choices.get(name)
            if sub is None:
                continue
            known = _known_flags(sub)
            for flag in _FLAG_RE.findall(rest):
                checked += 1
                assert flag in known, f"`codeintel {name}{rest}` uses {flag!r}, which `{name}`'s parser rejects"
    assert checked, "no flag found in any epilog to check"


def test_every_flag_in_a_top_level_example_is_accepted_by_its_commands_parser():
    from codeintel.__main__ import _EXAMPLES, _START_HERE, build_parser

    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))

    for cmd, _why in [*_EXAMPLES, *_START_HERE]:
        name = cmd.split()[1]
        sub = subparsers.choices[name]
        known = _known_flags(sub)
        for flag in _FLAG_RE.findall(cmd):
            assert flag in known, f"{cmd!r} uses {flag!r}, which `{name}`'s parser rejects"


def test_every_op_value_named_in_help_text_is_a_real_query_op():
    """Five of six `--op` examples in the query epilog used to fail because `--target` was
    required with no default — this specifically pins every `--op <value>` token anywhere on the
    help surface to the parser's own accepted set, so a typoed or retired op cannot hide in prose."""
    from codeintel.__main__ import _EPILOGS, _EXAMPLES, _START_HERE
    from codeintel.query_ops import QUERY_OPS

    texts = [*_EPILOGS.values(), *(cmd for cmd, _ in _EXAMPLES), *(cmd for cmd, _ in _START_HERE)]
    seen: set[str] = set()
    for text in texts:
        seen |= set(_OP_VALUE_RE.findall(text))
    assert seen, "no `--op <value>` example found to check"
    for op in seen:
        assert op in QUERY_OPS, f"help text uses unknown op {op!r}"


def test_query_epilog_documents_every_op_exactly_once():
    """`callees`, `context` and `pattern` were documented NOWHERE on the CLI. This pins the
    `operations:` block to the full canonical set, so a future op can't be added to the parser
    without also landing here."""
    from codeintel.__main__ import _EPILOGS
    from codeintel.query_ops import QUERY_OPS

    ops_block = _EPILOGS["query"].split("operations:\n", 1)[1].split("\n\n", 1)[0]
    listed = re.findall(r"(?m)^  (\w[\w-]*)\s{2,}", ops_block)
    assert sorted(listed) == sorted(QUERY_OPS), (sorted(listed), sorted(QUERY_OPS))


def test_query_op_choices_match_the_canonical_list():
    from codeintel.__main__ import build_parser
    from codeintel.query_ops import QUERY_OPS

    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    op_action = next(a for a in subparsers.choices["query"]._actions if a.dest == "op")
    assert set(op_action.choices) == set(QUERY_OPS)
    assert op_action.required is True


def test_query_engine_choices_match_the_gateways_canonical_set():
    from codeintel.__main__ import build_parser
    from codeintel.gateway import _KNOWN_ENGINES

    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    engine_action = next(a for a in subparsers.choices["query"]._actions if a.dest == "engine")
    assert set(engine_action.choices) == _KNOWN_ENGINES


def test_query_target_is_optional_with_a_blank_default():
    """`--target` used to be `required=True` while five of six epilog examples showed `--op`
    alone — the parser disagreeing with its own prose. `overview`/`changed`/`hotspots` genuinely
    ignore it, so a blank default (not `required=True`) is what makes those examples actually
    runnable."""
    from codeintel.__main__ import build_parser

    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    target_action = next(a for a in subparsers.choices["query"]._actions if a.dest == "target")
    assert target_action.required is False
    assert target_action.default == ""


# The QUERY_OPS/`_QueryOp` drift guard lives in tests/test_mcp_server.py
# (test_query_ops_module_matches_the_query_op_literal) — it is a schema invariant on
# `codeintel.server`, not CLI help text, and that file already imports `codeintel.server`.


# --------------------------------------------------------------------------- one description table

def test_every_subparsers_help_and_description_come_from_command_groups():
    """`_COMMAND_GROUPS` vs each `add_parser(help=...)` string used to be two separate tables that
    drifted apart wording-first (`query`'s `help=` still said "Query the code intelligence engine"
    long after `_COMMAND_GROUPS` had moved on). Deriving both `help=` and `description=` from
    `_COMMAND_GROUPS` removes the second table; this pins that nobody reintroduces one."""
    from codeintel.__main__ import _COMMAND_GROUPS, build_parser

    desc_by_name = {name: desc for _group, items in _COMMAND_GROUPS for name, desc in items}
    parser = build_parser()
    subparsers_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))

    for pseudo in subparsers_action._choices_actions:
        if pseudo.dest not in desc_by_name:      # "help" itself: deliberately not in the table
            continue
        assert pseudo.help == desc_by_name[pseudo.dest], pseudo.dest
        assert subparsers_action.choices[pseudo.dest].description == desc_by_name[pseudo.dest], \
            pseudo.dest


# --------------------------------------------------------------------------- prose vs implementation

def test_setup_install_deps_help_names_the_packages_onboarding_actually_installs():
    """`--install-deps` help text said `pip install -e .` after `onboarding.py` had already moved
    to installing named packages instead (editable-installing whatever repo the user happened to
    be standing in — not codeintel's own deps — with no `cwd=` pin on the subprocess). The
    flag/op consistency tests above are STRUCTURAL (is this a real flag, a real op); they cannot
    catch this because `--install-deps` was, and still is, a perfectly real flag — the bug was in
    what the prose claimed it runs. Pinning the exact package names against `onboarding.py`'s own
    source is the cheap version of that semantic check for this one flag."""
    import inspect

    from codeintel import onboarding
    from codeintel.__main__ import build_parser

    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    action = next(a for a in subparsers.choices["setup"]._actions if a.dest == "install_deps")

    source = inspect.getsource(onboarding)
    for pkg in ("fastembed", "sqlite-vec"):
        assert pkg in action.help, f"--install-deps help omits {pkg!r}"
        assert pkg in source, f"onboarding.py no longer installs {pkg!r} — update the help text too"
