import json

import pytest

from dots_brain.auth import authenticate, issue_client, read_connection
from dots_brain.clients import (
    bridge_entry,
    configure,
    integration_key,
    target_path,
)
from dots_brain.errors import InputError
from dots_brain.local import read_json, write_json
from dots_brain.removal import disconnect_client, uninstall
from dots_brain.runtime import credential, daemon_environment, up
from dots_brain.store import Store


def test_corrupt_service_state_is_quarantined_without_starting_a_replacement(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    store.initialize()
    state = store.directory / "service.json"
    state.write_text("{broken")
    monkeypatch.setattr("dots_brain.runtime.sys.platform", "linux")

    def unexpected_start(*args, **kwargs):
        raise AssertionError("a corrupt state must not start another daemon")

    monkeypatch.setattr("dots_brain.runtime.subprocess.Popen", unexpected_start)
    with pytest.raises(InputError, match="corrupt"):
        up(store)
    assert not state.exists()
    assert list(store.directory.glob("service.json.corrupt-*"))


def test_probe_credential_can_replace_corrupt_file_with_read_only_scope(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    probe = store.directory / "probe.connection.json"
    probe.write_text("not json")

    client_id = credential(
        store,
        name="installation-probe",
        path=probe,
        url="http://127.0.0.1:8765/mcp",
        projects=["__dots_brain_probe__"],
        write=False,
        replace_invalid=True,
    )

    connection = read_connection(probe)
    assert connection["client_id"] == client_id
    assert authenticate(store, connection["token"]).scopes == frozenset({"memory:read"})


def test_daemon_environment_drops_caller_import_and_proxy_state(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/unsafe/imports")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("LANG", "C.UTF-8")

    environment = daemon_environment()

    assert environment["LANG"] == "C.UTF-8"
    assert "PYTHONPATH" not in environment
    assert "HTTPS_PROXY" not in environment


def test_normalized_client_path_has_one_registration_identity(tmp_path):
    canonical = tmp_path / "config.json"
    spelled_differently = tmp_path / "nested" / ".." / "config.json"

    assert target_path("cursor", spelled_differently) == canonical
    assert integration_key("cursor", canonical) == integration_key("cursor", spelled_differently)


def test_invalid_registry_row_does_not_hide_valid_uninstall_work(tmp_path):
    store = Store(tmp_path / "memory")
    config = tmp_path / "cursor.json"
    store.initialize()
    private = store.directory / "connections"
    private.mkdir()
    key = integration_key("cursor", config)
    connection = private / f"cursor-{key}.json"
    issued = issue_client(
        store,
        name="cursor",
        scopes=["memory:read"],
        projects=None,
        days=1,
        output=connection,
        url="http://127.0.0.1:8765/mcp",
    )
    entry = bridge_entry(connection, store.directory, "cursor")
    configure(config, provider="cursor", entry=entry)
    record = {
        "provider": "cursor",
        "config_file": str(config),
        "entry": entry,
        "connection_file": str(connection),
        "client_id": issued["client_id"],
        "local": True,
    }
    write_json(
        store.directory / "integrations.json",
        {"version": 1, "items": {key: record, "bad": {"provider": "cursor"}}},
    )

    result = uninstall(store)

    assert result["state"] == "partial"
    assert result["clients"] == [
        {"provider": "cursor", "config_file": str(config), "state": "removed"}
    ]
    assert "dots-brain" not in read_json(config)["mcpServers"]
    persisted = read_json(store.directory / "integrations.json")
    assert persisted["items"] == {"bad": {"provider": "cursor"}}


def test_remote_disconnect_is_terminal_without_local_database(tmp_path):
    store = Store(tmp_path / "client")
    config = tmp_path / "remote.json"
    config.parent.mkdir(exist_ok=True)
    connection = tmp_path / "remote-connection.json"
    connection.write_text(
        json.dumps(
            {
                "version": 1,
                "client_id": "remote",
                "token": "secret",
                "url": "https://host.example/mcp",
            }
        )
    )
    entry = bridge_entry(connection, None, "cursor")
    configure(config, provider="cursor", entry=entry)
    key = integration_key("cursor", config)
    store.directory.mkdir()
    write_json(
        store.directory / "integrations.json",
        {
            "version": 1,
            "items": {
                key: {
                    "provider": "cursor",
                    "config_file": str(config),
                    "entry": entry,
                    "connection_file": str(connection),
                    "client_id": "remote",
                    "local": False,
                }
            },
        },
    )

    result = disconnect_client(store, provider="cursor", config=config)

    assert result["state"] == "disconnected_remote"
    assert result["access"] == "issuer_revocation_required"
    assert not store.path.exists()
    assert read_json(store.directory / "integrations.json")["items"] == {}
