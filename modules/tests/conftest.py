"""Explicit source-only harness; none of this activates installed core modules."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_optional_environment(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def explicit_legacy_registration(monkeypatch):
    from dots_brain_cortex.tools import register_tools

    from dots_brain import integration_tools

    core_registration = integration_tools.register_tools

    def register(server, service, policy, read_call, write_call):
        scopes = core_registration(server, service, policy, read_call, write_call)
        if getattr(service, "cortex", None) is not None:
            scopes.update(register_tools(server, service, policy, read_call, write_call))
        return scopes

    monkeypatch.setattr(integration_tools, "register_tools", register)
