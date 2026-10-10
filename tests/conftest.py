"""Test environment isolation shared by subprocess and in-process checks."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_user_environment(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Keep default HOME and XDG paths inside each disposable test fixture."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
