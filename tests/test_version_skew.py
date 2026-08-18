"""A server serving code older than what is installed must say so.

The failure this guards is silent by construction: upgrading the package replaces files on disk and
changes nothing about a running process, which keeps answering from the module it imported at
startup. Nothing goes red — engines stay green, `status` reports the stale version as though it
were the truth, and a fix the user can read in the CHANGELOG is absent from every answer.
"""
from __future__ import annotations

import textwrap
import types

import pytest

from codeintel import doctor


def _module_with(tmp_path, in_memory: str, on_disk: str) -> types.ModuleType:
    """A stand-in for the imported `codeintel` package whose SOURCE disagrees with its loaded value.

    That divergence is the whole condition under test: `__version__` is what the running process
    holds, the file is what a later install wrote.
    """
    path = tmp_path / "__init__.py"
    path.write_text(f'__version__ = "{on_disk}"\n', encoding="utf-8")
    mod = types.ModuleType("codeintel")
    mod.__version__ = in_memory
    mod.__file__ = str(path)
    return mod


@pytest.fixture
def fake_codeintel(monkeypatch):
    def _install(mod):
        monkeypatch.setitem(__import__("sys").modules, "codeintel", mod)
    return _install


def test_skew_is_detected_when_disk_is_newer(tmp_path, fake_codeintel):
    fake_codeintel(_module_with(tmp_path, in_memory="0.15.3", on_disk="0.15.4"))
    assert doctor.running_version_skew() == ("0.15.3", "0.15.4")


def test_no_skew_when_they_agree(tmp_path, fake_codeintel):
    fake_codeintel(_module_with(tmp_path, in_memory="0.15.4", on_disk="0.15.4"))
    assert doctor.running_version_skew() is None


def test_skew_is_reported_in_either_direction(tmp_path, fake_codeintel):
    # A DOWNGRADE on disk is still a mismatch worth surfacing — it means the process and the
    # install disagree, which is the thing that makes the other numbers untrustworthy. Reporting
    # only "disk is newer" would stay silent exactly when someone has rolled back to debug.
    fake_codeintel(_module_with(tmp_path, in_memory="0.15.4", on_disk="0.15.3"))
    assert doctor.running_version_skew() == ("0.15.4", "0.15.3")


@pytest.mark.parametrize(
    "source",
    [
        "",                                   # no __version__ at all
        "__version__ = 1\n",                  # not a string
        "def f(:\n",                          # unparseable
        "__version__ = compute()\n",          # not a literal
    ],
)
def test_unreadable_source_makes_no_claim(tmp_path, fake_codeintel, source):
    # Silence beats a false alarm: a spurious "restart your server" prompt costs more trust than a
    # missed one, so every uncertain case returns None rather than guessing.
    path = tmp_path / "__init__.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    mod = types.ModuleType("codeintel")
    mod.__version__ = "0.15.4"
    mod.__file__ = str(path)
    fake_codeintel(mod)
    assert doctor.running_version_skew() is None


def test_missing_file_makes_no_claim(tmp_path, fake_codeintel):
    mod = types.ModuleType("codeintel")
    mod.__version__ = "0.15.4"
    mod.__file__ = str(tmp_path / "does-not-exist.py")
    fake_codeintel(mod)
    assert doctor.running_version_skew() is None


def test_never_raises_on_a_hostile_module(fake_codeintel):
    class Exploding(types.ModuleType):
        @property
        def __file__(self):  # type: ignore[override]
            raise RuntimeError("boom")

    fake_codeintel(Exploding("codeintel"))
    assert doctor.running_version_skew() is None


def test_status_surfaces_the_skew(monkeypatch, tmp_path, fake_codeintel):
    # The field has to reach `code.status`, not just `doctor`: status is what a caller checks when
    # an expected fix appears to be missing.
    from codeintel import server

    fake_codeintel(_module_with(tmp_path, in_memory="0.15.3", on_disk="0.15.4"))
    monkeypatch.setattr(
        doctor, "run", lambda *a, **k: {
            "ok": True,
            "summary": {"ready": 3, "total": 3, "healthy": True},
            "engines": {},
            "versions": {},
            "version_skew": {"running": "0.15.3", "installed": "0.15.4",
                             "remediation": "restart the codeintel server"},
        },
        raising=False,
    )
    out = server.code_status_handler({"project_root": str(tmp_path)})
    assert out["version_skew"]["running"] == "0.15.3"
    assert out["version_skew"]["installed"] == "0.15.4"


def test_status_fallback_carries_the_key():
    # Shape stability: a caller that reads `version_skew` must not KeyError on the degraded path,
    # or the check silently stops running exactly when things are worst.
    from codeintel import server

    assert "version_skew" in server._STATUS_FALLBACK
    assert server._STATUS_FALLBACK["version_skew"] is None
