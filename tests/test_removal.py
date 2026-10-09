import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dots_brain.auth import authenticate, issue_client, read_connection
from dots_brain.clients import bridge_entry, configure, connect_client, integration_key
from dots_brain.errors import InputError, SuppressedError
from dots_brain.local import read_json, write_json
from dots_brain.removal import disconnect_client, remove_entry, uninstall
from dots_brain.runtime import active, down, up
from dots_brain.store import Store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def record_connection(store, config, provider="cursor", *, legacy=False):
    store.initialize()
    private = store.directory / "connections"
    private.mkdir(exist_ok=True)
    key = integration_key(provider, config)
    connection = private / (f"{provider}.json" if legacy else f"{provider}-{key}.json")
    issued = issue_client(
        store,
        name=provider,
        scopes=["memory:read"],
        projects=None,
        days=1,
        output=connection,
        url="http://localhost:8765/mcp",
    )
    entry = bridge_entry(connection, store.directory, provider)
    configure(config, provider=provider, entry=entry)
    record = {
        "provider": provider,
        "config_file": str(config),
        "entry": entry,
        "connection_file": str(connection),
        "client_id": issued["client_id"],
        "local": True,
    }
    if not legacy:
        write_json(store.directory / "integrations.json", {"version": 1, "items": {key: record}})
    return record, read_connection(connection)["token"]


def test_complete_lifecycle_keeps_memories_and_blocks_restart_until_explicit_resume(tmp_path):
    store = Store(tmp_path / "memory")
    config = tmp_path / "cursor.json"
    try:
        running = up(store, port=0)
        connect_client(store, provider="cursor", config=config)
        memory = store.remember(
            content="Keep this memory.", source="test", account="a", event_id="1"
        )
        deleted = store.remember(content="Forget this.", source="test", account="a", event_id="2")
        store.forget(deleted["id"], expected_revision=deleted["revision"])
        config_doc = read_json(config)
        entry = config_doc["mcpServers"]["dots-brain"]
        token = read_connection(Path(entry["args"][4]))["token"]
        config_doc["theme"] = "new preference after setup"
        write_json(config, config_doc)
        before = {p: p.read_bytes() for p in store.directory.rglob("*") if p.is_file()}
        preview = uninstall(store, dry_run=True)
        assert preview["state"] == "preview" and preview["clients"][0]["state"] == "would_remove"
        assert before == {p: p.read_bytes() for p in store.directory.rglob("*") if p.is_file()}
        assert active(read_json(store.directory / "service.json"))
        result = uninstall(store)
        assert result["state"] == "uninstalled"
        assert not active(read_json(store.directory / "service.json"))
        assert read_json(config) == {"mcpServers": {}, "theme": "new preference after setup"}
        assert store.get(memory["id"])["content"] == "Keep this memory."
        with pytest.raises(SuppressedError):
            store.remember(content="Forget this.", source="test", account="a", event_id="2")
        assert authenticate(store, token) is None
        assert uninstall(store)["state"] == "uninstalled"
        with pytest.raises(InputError, match="disabled"):
            up(store)
        for command in ("setup", "doctor"):
            checked = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "dots_brain.cli",
                    "--data-dir",
                    str(store.directory),
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert json.loads(checked.stdout)["state"] == "disabled"
        bridge = subprocess.run(
            [entry["command"], *entry["args"]], capture_output=True, text=True, timeout=10
        )
        assert bridge.returncode == 1 and "disabled" in bridge.stderr
        assert token not in json.dumps(result) + bridge.stdout + bridge.stderr
        restarted = up(store, resume=True)
        assert restarted["pid"] != running["pid"]
        assert authenticate(store, token) is None
        connected = connect_client(store, provider="cursor", config=config)
        assert connected["read"] and connected["write"]
        assert store.get(memory["id"])["content"] == "Keep this memory."
    finally:
        down(store)


@pytest.mark.parametrize("provider", ["claude-code", "cursor", "codex", "mcp-json"])
def test_removal_preserves_current_settings_and_never_restores_whole_backup(tmp_path, provider):
    store = Store(tmp_path / "memory")
    config = tmp_path / ("config.toml" if provider == "codex" else "config.json")
    config.write_text('# comment\nmodel = "old"\n' if provider == "codex" else '{"theme":"old"}')
    record, _ = record_connection(store, config, provider)
    backup = config.with_name(config.name + ".before-dots-brain")
    backup_before = backup.read_bytes()
    if provider == "codex":
        config.write_text(config.read_text().replace('model = "old"', 'model = "new"'))
    else:
        doc = read_json(config)
        doc["theme"] = "new"
        doc["mcpServers"]["other"] = {"command": "keep"}
        write_json(config, doc)
    assert remove_entry(record, dry_run=False)["state"] == "removed"
    if provider == "codex":
        assert tomlkit.parse(config.read_text())["model"] == "new"
        assert "# comment" in config.read_text()
    else:
        assert read_json(config) == {"theme": "new", "mcpServers": {"other": {"command": "keep"}}}
    assert backup.read_bytes() == backup_before
    assert remove_entry(record, dry_run=False)["state"] == "already_absent"


@pytest.mark.parametrize("kind", ["modified", "malformed", "symlink"])
def test_edited_or_unreadable_config_is_preserved_but_host_access_is_revoked(tmp_path, kind):
    store = Store(tmp_path / "memory")
    config = tmp_path / "cursor.json"
    _, token = record_connection(store, config)
    if kind == "modified":
        doc = read_json(config)
        doc["mcpServers"]["dots-brain"]["env"] = {"USER_SETTING": "keep"}
        write_json(config, doc)
    elif kind == "malformed":
        config.write_text("{ unfinished user edit")
    else:
        other = tmp_path / "other.json"
        config.rename(other)
        config.symlink_to(other)
    before = config.read_bytes()
    result = uninstall(store)
    assert result["state"] == "partial" and result["service_disabled"]
    assert result["clients"][0]["state"].startswith("preserved_")
    assert config.read_bytes() == before
    assert authenticate(store, token) is None
    assert uninstall(store)["state"] == "partial"


def test_profiles_have_distinct_credentials_and_disconnect_does_not_break_other_profile(tmp_path):
    store = Store(tmp_path / "memory")
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    try:
        up(store, port=0)
        a = connect_client(store, provider="cursor", config=first)
        b = connect_client(store, provider="cursor", config=second)
        assert a["client_id"] != b["client_id"]
        entries = [read_json(path)["mcpServers"]["dots-brain"] for path in (first, second)]
        tokens = [read_connection(Path(entry["args"][4]))["token"] for entry in entries]
        assert disconnect_client(store, provider="cursor", config=first)["state"] == "disconnected"
        assert authenticate(store, tokens[0]) is None
        assert authenticate(store, tokens[1]) is not None
        assert read_json(second)["mcpServers"]["dots-brain"] == entries[1]
        assert (
            disconnect_client(store, provider="cursor", config=first)["state"] == "already_absent"
        )
    finally:
        down(store)


def test_legacy_custom_path_requires_inventory_and_shared_token_is_not_silently_revoked(tmp_path):
    store = Store(tmp_path / "memory")
    config = tmp_path / "custom.json"
    _, token = record_connection(store, config, legacy=True)
    assert uninstall(store, dry_run=True)["clients"] == []
    preview = uninstall(store, dry_run=True, extra_configs=[f"cursor={config}"])
    assert preview["clients"][0]["state"] == "would_remove"
    result = disconnect_client(store, provider="cursor", config=config)
    assert result["state"] == "partial" and result["access"] == "shared_credential_retained"
    assert authenticate(store, token) is not None
    assert uninstall(store)["state"] == "uninstalled"
    assert authenticate(store, token) is None


def test_missing_installation_and_dry_run_do_not_create_files(tmp_path):
    store = Store(tmp_path / "missing")
    assert uninstall(store, dry_run=True)["state"] == "preview"
    assert uninstall(store)["state"] == "not_installed"
    assert disconnect_client(store, provider="cursor", dry_run=True)["state"] == "already_absent"
    assert not store.directory.exists()


def test_corrupt_registry_is_preserved_and_reported_while_service_is_disabled(tmp_path):
    store = Store(tmp_path / "memory")
    _, token = record_connection(store, tmp_path / "cursor.json")
    registry = store.directory / "integrations.json"
    registry.write_text("{broken")
    result = uninstall(store)
    assert result["state"] == "partial" and result["service_disabled"]
    assert any(issue["state"] == "registry_unreadable" for issue in result["issues"])
    assert registry.read_text() == "{broken"
    assert authenticate(store, token) is None


def test_remote_disconnect_preserves_supplied_credential_and_never_creates_database(tmp_path):
    host, client = Store(tmp_path / "host"), Store(tmp_path / "device")
    config = tmp_path / "remote.json"
    try:
        up(host, port=0)
        supplied = host.directory / "probe.connection.json"
        token = read_connection(supplied)["token"]
        connect_client(client, provider="cursor", config=config, connection=supplied)
        result = disconnect_client(client, provider="cursor", config=config)
        assert result["state"] == "partial" and result["access"] == "issuer_revocation_required"
        result = uninstall(client)
        assert result["state"] == "partial"
        assert supplied.exists() and authenticate(host, token) is not None
        assert not client.path.exists()
        assert read_json(config) == {"mcpServers": {}}
    finally:
        down(host)


def test_running_owner_stdio_rejects_tools_after_uninstall(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "dots_brain.cli", "--data-dir", str(store.directory), "serve"],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert not (await session.call_tool("memory_status", {})).isError
                uninstall(store)
                assert (await session.call_tool("memory_status", {})).isError

    asyncio.run(asyncio.wait_for(run(), timeout=15))
