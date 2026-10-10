import json
import time
from argparse import Namespace

import pytest

from dots_brain.auth import authenticate, issue_client, read_connection
from dots_brain.clients import (
    bridge_entry,
    configure,
    connect_client,
    integration_key,
    target_path,
)
from dots_brain.errors import InputError
from dots_brain.local import read_json, write_json
from dots_brain.removal import disconnect_client, uninstall
from dots_brain.runtime import credential, daemon_environment, preflight, schedule_reap, up
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


def test_preflight_uses_only_explicit_validated_network_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "dots_brain.runtime.read_json", lambda _: pytest.fail("default must not read a vendor path")
    )
    assert preflight()["network_policy"] == "not_supplied"

    policy = tmp_path / "network-policy.json"
    policy.write_text(json.dumps({"version": 1, "tcp_network_access": {"domains": ["mcp"]}}))
    monkeypatch.undo()
    inspected = preflight(policy)
    assert inspected["network_policy"] == "checked"
    assert inspected["managed_network"] is True
    assert inspected["tcp_destinations_configured"] is True

    policy.write_text(json.dumps({"version": 1, "tcp_network_access": []}))
    invalid = preflight(policy)
    assert invalid["network_policy"] == "invalid"
    assert invalid["managed_network"] == "unknown"


def test_preflight_parser_exposes_optional_policy_and_run_rejects_unknown_command(tmp_path):
    from dots_brain.cli import parser, run

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"version": 1, "tcp_network_access": {}}))
    result = run(parser().parse_args(["preflight", "--network-policy", str(policy)]))
    assert result["network_policy"] == "checked"
    with pytest.raises(InputError, match="Unknown internal command"):
        run(Namespace(command="unknown", data_dir=None))


def test_background_reaper_waits_for_the_managed_child():
    class Process:
        waited = False

        def wait(self):
            self.waited = True

    process = Process()
    schedule_reap(process)
    for _ in range(100):
        if process.waited:
            break
        time.sleep(0.001)
    assert process.waited


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


def test_remote_connect_creates_only_registration_parent(tmp_path, monkeypatch):
    store = Store(tmp_path / "device")
    config = tmp_path / "remote.json"
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

    async def verified(entry, **checks):
        assert checks["write"] is False
        return {"state": "verified_read", "read": True, "write": "not_tested"}

    monkeypatch.setattr("dots_brain.clients.verify_command", verified)
    result = connect_client(store, provider="cursor", config=config, connection=connection)

    assert result["state"] == "configured_verified_bridge"
    assert store.directory.is_dir()
    assert not store.path.exists()


def test_repeated_local_connect_skips_second_write_probe(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    store.initialize()
    config = tmp_path / "cursor.json"
    calls = []

    def runtime(*args, **kwargs):
        return {"url": "http://127.0.0.1:8765/mcp"}

    async def verified(entry, **checks):
        calls.append(checks["write"])
        return {
            "state": "verified_read_write" if checks["write"] else "verified_read",
            "read": True,
            "write": True if checks["write"] else "not_tested",
        }

    monkeypatch.setattr("dots_brain.clients.up", runtime)
    monkeypatch.setattr("dots_brain.clients.verify_command", verified)
    connect_client(store, provider="cursor", config=config)
    connect_client(store, provider="cursor", config=config)

    assert calls == [True, False]


def test_repeated_uninstall_revokes_without_reenabling_the_store(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()

    assert uninstall(store)["state"] == "uninstalled"
    assert uninstall(store)["state"] == "uninstalled"
