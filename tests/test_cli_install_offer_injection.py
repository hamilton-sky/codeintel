"""`codeintel install` now offers to wire an agent into codeintel via AGENTS.md after a successful
registration (`codeintel.injector.offer_injection`) — previously "Start a new agent session to
pick up the tools" was the only thing printed, so a registered, verified, indexed server sat there
with no agent ever told to prefer `code.query` over grep.

`offer_injection` is itself consent-gated (prompts only on a TTY, prints the command otherwise,
never writes without an explicit "y") — these tests pin the CALLER's half of that contract: the
offer must not fire when nothing actually succeeded, and running it must never write a file when
there is nobody there to consent (which is always true under pytest — stdin is not a tty).
"""
from __future__ import annotations

import argparse
from importlib import import_module


def _args(**kw) -> argparse.Namespace:
    defaults = {"agent": "auto", "no_verify": False, "relative_command": False, "dry_run": False}
    return argparse.Namespace(**{**defaults, **kw})


class _FakeInstaller:
    def __init__(self, results, skipped=()):
        self.results = results
        self.skipped = list(skipped)

    def register_detected(self, *, verify, absolute):
        return self.results, self.skipped

    def register_all(self, *, verify, absolute):
        return self.results

    def register_many(self, agents, *, verify, absolute):
        return self.results


def _install(monkeypatch, results, skipped=(), **overrides):
    monkeypatch.setattr("codeintel.installer.Installer", lambda: _FakeInstaller(results, skipped))
    return import_module("codeintel.commands.install").run(_args(**overrides))


def test_offer_is_made_after_a_successful_install(monkeypatch):
    calls = []
    monkeypatch.setattr("codeintel.injector.offer_injection", lambda *a, **kw: calls.append((a, kw)))

    code = _install(monkeypatch, [
        {"agent": "claude", "path": "/c/.claude.json", "action": "registered"},
    ])

    assert code == 0
    assert len(calls) == 1


def test_offer_is_not_made_when_every_registration_failed(monkeypatch):
    calls = []
    monkeypatch.setattr("codeintel.injector.offer_injection", lambda *a, **kw: calls.append((a, kw)))

    code = _install(monkeypatch, [
        {"agent": "codex", "path": "/c/codex.toml", "action": "failed", "reason": "unwritable"},
    ])

    assert code == 1
    assert calls == []


def test_offer_is_not_made_when_verification_fails(monkeypatch):
    """A written config that the handshake proved unusable is not a success worth building on —
    the exit code already treats it as a failure (`any_ok = False`); the offer follows the same
    signal."""
    calls = []
    monkeypatch.setattr("codeintel.injector.offer_injection", lambda *a, **kw: calls.append((a, kw)))

    code = _install(monkeypatch, [
        {"agent": "claude", "path": "/c/.claude.json", "action": "registered",
         "verified": {"ok": False, "detail": "handshake timed out"}},
    ])

    assert code == 1
    assert calls == []


def test_offer_is_not_made_when_auto_finds_no_agent(monkeypatch):
    calls = []
    monkeypatch.setattr("codeintel.injector.offer_injection", lambda *a, **kw: calls.append((a, kw)))

    code = _install(monkeypatch, [], skipped=["claude", "codex", "zed"])

    assert code == 1
    assert calls == []


def test_a_successful_install_writes_nothing_without_a_tty_to_consent(monkeypatch, tmp_path, capsys):
    """The real `offer_injection`, not a mock: off a TTY (always true under pytest — stdin is not
    a tty) it must print the command instead of writing, so a successful `codeintel install` never
    silently touches AGENTS.md/CLAUDE.md in whatever directory it happens to run from."""
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "AGENTS.md").exists()

    code = _install(monkeypatch, [
        {"agent": "claude", "path": "/c/.claude.json", "action": "registered"},
    ])

    assert code == 0
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    out = capsys.readouterr().out
    assert "codeintel map --inject" in out


def test_dry_run_never_offers_injection_either(monkeypatch):
    """`--dry-run` returns before the real registration path — and before the offer — entirely."""
    calls = []
    monkeypatch.setattr("codeintel.injector.offer_injection", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr("codeintel.commands.install._run_dry", lambda args: 0)

    code = import_module("codeintel.commands.install").run(_args(dry_run=True))

    assert code == 0
    assert calls == []
