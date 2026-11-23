"""Rendered names drop the backend's project id even when a directory in the path has a space.

The backend's project id is the flattened absolute path of the repo (`tmp-…-mixed`). A repo
directory such as `ünï cødé/日本/uni.py` gives qualified names with a space in a module segment,
and the old "a qualified name never contains a space" rule left the whole id in every row."""
from __future__ import annotations

from codeintel.engines.graph.render import _strip_project_prefix


def test_a_path_with_spaces_renders_without_the_project_id():
    qn = "tmp-claude-0-x-scratchpad-mixed.ünï cødé.日本.uni.unicaller"
    assert _strip_project_prefix(qn, may_be_filename=False) == "ünï cødé.日本.uni.unicaller"
    assert _strip_project_prefix(qn) == "ünï cødé.日本.uni.unicaller"


def test_a_heading_with_a_hyphenated_head_is_left_alone():
    heading = "EC-1.1: Empty workflow plan"
    assert _strip_project_prefix(heading) == heading
    assert _strip_project_prefix(heading, may_be_filename=False) == heading
