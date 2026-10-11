"""Synthetic lifecycle regressions for absent installations and scoped removal."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dots_brain.auth import authenticate, issue_client, read_connection
from dots_brain.clients import bridge_entry, configure, integration_key
from dots_brain.local import read_json, write_json
from dots_brain.removal import disconnect_client, uninstall
from dots_brain.store import Store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def registered_client(store, config):
    store.initialize()
    key = integration_key("cursor", config)
    connection = store.directory / "connections" / f"cursor-{key}.json"
    connection.parent.mkdir()
    issued = issue_client(
        store,
        name="Synthetic editor",
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
    write_json(store.directory / "integrations.json", {"version": 1, "items": {key: record}})
    return record, read_connection(connection)["token"]


@pytest.mark.parametrize("exists", [False, True])
def test_uninstall_absent_installation_leaves_directory_untouched_and_allows_initialize(
    tmp_path, exists
):
    store = Store(tmp_path / "memory")
    if exists:
        store.directory.mkdir()
    assert uninstall(store, dry_run=True)["changes_applied"] is False
    result = uninstall(store)
    assert result["state"] == "not_installed", result
    assert result.get("managed_process_stopped") == "unknown"
    assert result["service_disabled"] is False
    assert store.directory.exists() is exists
    assert not list(store.directory.glob("*"))
    store.initialize()
    assert store.path.is_file()


def test_deleted_installation_does_not_claim_a_living_server_was_stopped(tmp_path):
    store = Store(tmp_path / "memory")
    # Use the real server lease in a separate process. Removing its pathname cannot
    # release the open lease or stop the process that holds it.
    script = (
        "import sys,time; from dots_brain.store import Store; "
        "from dots_brain.runtime import serve_locks; "
        "store=Store(sys.argv[1]); store.initialize(); "
        "lease=serve_locks(store,http=False); lease.__enter__(); "
        "print('ready',flush=True); time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(store.directory)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline() == "ready\n"
        shutil.rmtree(store.directory)
        result = uninstall(store)
        assert result.get("managed_process_stopped") == "unknown", result
        assert process.poll() is None
        assert not store.directory.exists()
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_missing_store_with_owned_bridge_cannot_recreate_disable_state(tmp_path):
    store = Store(tmp_path / "removed-memory")
    config = Path.home() / ".cursor/mcp.json"
    key = integration_key("cursor", config)
    connection = store.directory / "connections" / f"cursor-{key}.json"
    configure(config, provider="cursor", entry=bridge_entry(connection, store.directory, "cursor"))
    preview = uninstall(store, dry_run=True)
    result = uninstall(store)
    assert not store.directory.exists(), result
    assert result.get("managed_process_stopped") == "unknown"
    assert result["state"] == "partial"
    assert preview["issues"] == result["issues"]
    assert read_json(config) == {"mcpServers": {}}


def test_no_recorded_daemon_does_not_claim_stop_proof_from_absent_writer_lock(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    result = uninstall(store)
    assert result["state"] == "uninstalled", result
    assert result["managed_process_stopped"] == "unknown"


def unrelated_config(kind):
    home = Path.home()
    home.mkdir(exist_ok=True)
    if kind == "symlink":
        source = home / "preferences.toml"
        source.write_text('model = "synthetic-model"\n')
        path = home / ".codex/config.toml"
        path.parent.mkdir()
        path.symlink_to(source)
    elif kind == "malformed":
        path = home / ".claude.json"
        path.write_text("{ incomplete preferences")
    else:
        path = home / ".claude.json"
        write_json(
            path,
            {"mcpServers": {"dots-brain": {"command": "different-installation"}}},
        )
    return path


@pytest.mark.parametrize("kind", ["symlink", "malformed", "foreign"])
@pytest.mark.parametrize("operation", ["disconnect", "uninstall"])
def test_unrelated_default_config_cannot_turn_owned_removal_into_partial(tmp_path, kind, operation):
    store = Store(tmp_path / "memory")
    config = tmp_path / "selected.json"
    record, token = registered_client(store, config)
    other = unrelated_config(kind)
    original = other.read_bytes()
    files_before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    if operation == "disconnect":
        preview = disconnect_client(store, provider="cursor", config=config, dry_run=True)
        expected = "disconnected"
    else:
        preview = uninstall(store, dry_run=True)
        expected = "uninstalled"
    assert preview.get("issues", []) == []
    assert files_before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = (
        disconnect_client(store, provider="cursor", config=config)
        if operation == "disconnect"
        else uninstall(store)
    )
    assert result["state"] == expected, result
    assert result["issues"] == []
    assert other.read_bytes() == original
    assert read_json(config) == {"mcpServers": {}}
    assert authenticate(store, token) is None
    assert not Path(record["connection_file"]).exists()
    if operation == "uninstall":
        assert result["notes"] == preview["notes"]
        assert result["managed_process_stopped"] == "unknown"
    else:
        assert "notes" in preview


@pytest.mark.parametrize("operation", ["disconnect", "uninstall"])
def test_owned_malformed_config_remains_partial_and_visible_in_preview(tmp_path, operation):
    store = Store(tmp_path / "memory")
    config = tmp_path / "selected.json"
    record, token = registered_client(store, config)
    config.write_text("{ incomplete selected integration")
    original = config.read_bytes()
    call = (
        (lambda **kw: disconnect_client(store, provider="cursor", config=config, **kw))
        if operation == "disconnect"
        else (lambda **kw: uninstall(store, **kw))
    )
    preview = call(dry_run=True)
    client = preview["client"] if operation == "disconnect" else preview["clients"][0]
    assert client["state"] == "preserved_unreadable"
    assert authenticate(store, token) is not None
    assert config.read_bytes() == original
    result = call()
    assert result["state"] == "partial"
    assert config.read_bytes() == original
    if operation == "disconnect":
        assert authenticate(store, token) is not None
        assert Path(record["connection_file"]).exists()
    else:
        assert authenticate(store, token) is None


def test_absent_disconnect_does_not_discover_other_provider_integration(tmp_path):
    store = Store(tmp_path / "memory")
    other_config = Path.home() / ".cursor/mcp.json"
    _, token = registered_client(store, other_config)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = disconnect_client(store, provider="claude-code")
    assert result["state"] == "already_absent", result
    assert authenticate(store, token) is not None
    assert before == {
        p: p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file() and p.name != "installation.lock"
    }


def test_orphan_owned_unreadable_credential_remains_material_in_preview(tmp_path):
    store = Store(tmp_path / "memory")
    config = Path.home() / ".cursor/mcp.json"
    record, _ = registered_client(store, config)
    (store.directory / "integrations.json").unlink()
    connection = Path(record["connection_file"])
    connection.write_text("{ damaged synthetic credential")
    before = config.read_bytes()
    preview = uninstall(store, dry_run=True)
    result = uninstall(store)
    assert preview["issues"] == result["issues"]
    assert result["state"] == "partial"
    assert any(issue["state"] == "preserved_unreadable" for issue in result["issues"])
    assert config.read_bytes() == before
    assert json.loads((store.directory / "integrations.json").read_text())["items"] == {}
