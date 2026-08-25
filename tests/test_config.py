"""Config validation/clamping (0.3.0). A malformed .codeintel.toml must degrade to defaults,
never break a query that loads it."""
from __future__ import annotations

from codeintel.config import _DEFAULTS, _coerce, load_config


def test_defaults_present_for_unset_keys():
    c = _coerce({})
    for k, v in _DEFAULTS.items():
        assert c[k] == v


def test_cosine_floor_is_clamped_to_unit_interval():
    assert _coerce({"cosine_floor": 5.0})["cosine_floor"] == 1.0
    assert _coerce({"cosine_floor": -3})["cosine_floor"] == 0.0
    assert _coerce({"cosine_floor": 0.4})["cosine_floor"] == 0.4


def test_non_numeric_ints_fall_back_to_default():
    assert _coerce({"window": "abc"})["window"] == _DEFAULTS["window"]
    assert _coerce({"max_chunks": 0})["max_chunks"] == _DEFAULTS["max_chunks"]
    assert _coerce({"stride": -5})["stride"] == _DEFAULTS["stride"]


def test_unknown_enum_values_fall_back():
    assert _coerce({"backend": "quantum"})["backend"] == "auto"
    assert _coerce({"semantic": "maybe"})["semantic"] == "on"
    assert _coerce({"reindex": "sometimes"})["reindex"] == "on-demand"


def test_chunk_strategy_enum_validation():
    assert _coerce({})["chunk_strategy"] == "syntax"                          # default
    assert _coerce({"chunk_strategy": "lines"})["chunk_strategy"] == "lines"  # escape hatch kept
    assert _coerce({"chunk_strategy": "SYNTAX"})["chunk_strategy"] == "syntax"  # case-normalized
    assert _coerce({"chunk_strategy": "ast"})["chunk_strategy"] == "syntax"   # unknown → default


def test_rerank_config_validation():
    assert _coerce({})["rerank"] == "on"                                      # default
    assert _coerce({})["rerank_candidates"] == 60                            # default
    assert _coerce({"rerank": "off"})["rerank"] == "off"                     # escape hatch kept
    assert _coerce({"rerank": "ON"})["rerank"] == "on"                       # case-normalized
    assert _coerce({"rerank": "maybe"})["rerank"] == "on"                    # unknown → default
    assert _coerce({"rerank_candidates": 50})["rerank_candidates"] == 50     # positive kept
    assert _coerce({"rerank_candidates": 0})["rerank_candidates"] == 60      # non-positive → default
    assert _coerce({"rerank_candidates": "x"})["rerank_candidates"] == 60    # non-int → default


def test_valid_values_are_preserved():
    c = _coerce({"backend": "graph", "cosine_floor": 0.5, "window": 40, "reindex": "never"})
    assert c["backend"] == "graph"
    assert c["cosine_floor"] == 0.5
    assert c["window"] == 40
    assert c["reindex"] == "never"


def test_empty_model_falls_back_but_custom_model_kept():
    assert _coerce({"model": "   "})["model"] == _DEFAULTS["model"]
    assert _coerce({"model": "BAAI/bge-base-en-v1.5"})["model"] == "BAAI/bge-base-en-v1.5"


def test_extra_forward_compat_keys_preserved():
    assert _coerce({"future_option": 123})["future_option"] == 123


def test_infinity_and_nan_do_not_crash():
    # TOML 1.0 allows `inf`/`nan` literals; int(float('inf')) raises OverflowError (not
    # ValueError), which must be caught so a bad value degrades instead of crashing the CLI.
    assert _coerce({"max_chunks": float("inf")})["max_chunks"] == _DEFAULTS["max_chunks"]
    assert _coerce({"window": float("-inf")})["window"] == _DEFAULTS["window"]
    assert _coerce({"max_total_chunks": float("inf")})["max_total_chunks"] == _DEFAULTS["max_total_chunks"]
    assert _coerce({"cosine_floor": float("nan")})["cosine_floor"] == _DEFAULTS["cosine_floor"]
    assert _coerce({"cosine_floor": float("inf")})["cosine_floor"] == 1.0  # inf clamps to the ceiling


def test_load_config_reads_and_validates_project_toml(tmp_path):
    (tmp_path / ".codeintel.toml").write_text('cosine_floor = "high"\nbackend = "semantic"\n')
    cfg = load_config(str(tmp_path))
    assert cfg["cosine_floor"] == _DEFAULTS["cosine_floor"]  # bad value → default, no crash
    assert cfg["backend"] == "semantic"                      # good value → kept


# --------------------------------------------------------------- CODEINTEL_HOME as a real seam

def test_codeintel_home_redirects_the_global_config(tmp_path, monkeypatch):
    """`CODEINTEL_HOME` has to move the machine-wide config, not just the semantic cache.

    `load_config` read `pathlib.Path.home()` inline, so the override reached the cache and nothing
    else — a container told to keep its state somewhere writable still merged (or failed to find)
    a config from a home directory it did not have."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('backend = "semantic"\nwindow = 77\n')
    monkeypatch.setenv("CODEINTEL_HOME", str(home))

    cfg = load_config(str(tmp_path))
    assert cfg["backend"] == "semantic"
    assert cfg["window"] == 77


def test_project_config_still_overrides_the_redirected_global(tmp_path, monkeypatch):
    """Precedence is defaults < global < project, and pointing the global elsewhere must not
    quietly promote it above the repo's own `.codeintel.toml`."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("window = 77\nstride = 5\n")
    (tmp_path / ".codeintel.toml").write_text("window = 12\n")
    monkeypatch.setenv("CODEINTEL_HOME", str(home))

    cfg = load_config(str(tmp_path))
    assert cfg["window"] == 12   # project wins
    assert cfg["stride"] == 5    # global still applies where the project is silent


def test_config_and_auth_survive_a_host_with_no_home_directory(tmp_path, monkeypatch):
    """The environment `CODEINTEL_HOME` exists for: a container UID with no passwd entry, where
    `Path.home()` raises. `load_config` used to propagate that `RuntimeError` — every query on such
    a host died as an opaque `provider-error`, and setting the documented override did not help
    because config never consulted it. `load_auth` had the same hole against its never-raise
    promise. Absent config is not an error; it means there is no config."""
    import pathlib

    from codeintel.auth import load_auth

    def _no_home(cls):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.delenv("CODEINTEL_HOME", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(_no_home))

    cfg = load_config(str(tmp_path))
    assert cfg["model"] == _DEFAULTS["model"], "must fall back to defaults, not raise"
    assert load_auth().enabled is False, "no config to read → RBAC off, not a crash"

    # ...and with the override set it must work properly rather than merely not crash.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("window = 31\n")
    monkeypatch.setenv("CODEINTEL_HOME", str(home))
    assert load_config(str(tmp_path))["window"] == 31
