from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness import guard


def test_is_ancestor_accepts_descendant(monkeypatch):
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""),
    )
    assert guard.is_ancestor("base", "head")


def test_is_ancestor_rejects_unrelated_ref(monkeypatch):
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr=""),
    )
    assert not guard.is_ancestor("base", "head")


def test_is_ancestor_surfaces_git_errors(monkeypatch):
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=128, stderr="bad ref"),
    )
    with pytest.raises(RuntimeError, match="bad ref"):
        guard.is_ancestor("base", "missing")
