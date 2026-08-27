"""The CLI's front door: `codeintel help` and what happens when you mistype a command.

argparse's default behavior on an unknown subcommand is to print the full list of choices and stop
— a dead end for a one-character typo (`gragh`, `dector`), which is exactly how it gets hit. And its
help lists twelve commands in declaration order with no grouping, so "what can this thing do?" means
reading every line to find the one verb you wanted.

The load-bearing invariant here is that the help screen stays HONEST: every command it advertises
must actually be a registered subcommand, and every registered subcommand must be advertised.
"""
from __future__ import annotations

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
