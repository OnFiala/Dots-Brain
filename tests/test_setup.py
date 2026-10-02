import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dots_brain.clients import configure, connect_client
from dots_brain.errors import InputError
from dots_brain.runtime import down, process_identity, up
from dots_brain.store import Store


def test_concurrent_up_reuses_one_process_and_recovers_after_crash(tmp_path):
    store = Store(tmp_path / "memory")
    try:
        first = up(store, port=0)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: up(store), range(4)))
        assert {row["pid"] for row in results} == {first["pid"]}
        saved = store.remember(
            content="Survives a service crash.", source="test", account="a", event_id="1"
        )
        os.kill(first["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while process_identity(first["pid"]) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        restarted = up(store)
        assert restarted["pid"] != first["pid"]
        assert restarted["url"] == first["url"]
        assert store.get(saved["id"])["content"] == "Survives a service crash."
    finally:
        down(store)


@pytest.mark.parametrize("provider", ["claude-code", "cursor", "codex", "mcp-json"])
def test_adapter_preserves_other_settings_is_idempotent_and_refuses_conflicts(tmp_path, provider):
    path = tmp_path / ("config.toml" if provider == "codex" else "config.json")
    original = (
        '# Preserve this comment\nmodel = "example"\n'
        if provider == "codex"
        else json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "other"}}})
    )
    path.write_text(original)
    entry = {"command": sys.executable, "args": ["-m", "dots_brain.cli", "bridge"]}
    assert configure(path, provider=provider, entry=entry)
    assert not configure(path, provider=provider, entry=entry)
    text = path.read_text()
    document = tomllib.loads(text) if provider == "codex" else json.loads(text)
    key = "mcp_servers" if provider == "codex" else "mcpServers"
    assert document[key]["dots-brain"] == entry
    if provider == "codex":
        assert "# Preserve this comment" in text
        assert document["model"] == "example"
    else:
        assert document["theme"] == "dark"
        assert document[key]["other"] == {"command": "other"}
    assert path.with_name(path.name + ".before-dots-brain").read_text() == original
    with pytest.raises(InputError):
        configure(path, provider=provider, entry={"command": "different"})
    assert path.read_text() == text


def test_connect_verifies_write_keeps_tokens_private_and_bridge_restarts_service(tmp_path):
    store = Store(tmp_path / "memory")
    config = tmp_path / "cursor.json"
    try:
        up(store, port=0)
        result = connect_client(store, provider="cursor", config=config, projects=["work"])
        assert result["read"] and result["write"]
        again = connect_client(store, provider="cursor", config=config, projects=["work"])
        assert not again["configuration_changed"]
        assert again["client_id"] == result["client_id"]
        assert store.status()["memories"] == 0  # Synthetic probe was removed.
        entry = json.loads(config.read_text())["mcpServers"]["dots-brain"]
        token = json.loads(Path(entry["args"][4]).read_text())["token"]
        assert token not in config.read_text() + json.dumps(result)
        down(store)
        entry = json.loads(config.read_text())["mcpServers"]["dots-brain"]

        async def launch_client():
            async with stdio_client(StdioServerParameters(**entry)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    status = await session.call_tool("memory_status", {})
                    assert not status.isError

        asyncio.run(asyncio.wait_for(launch_client(), timeout=20))
    finally:
        down(store)


def test_unsupported_provider_does_not_create_another_database(tmp_path):
    store = Store(tmp_path / "not-the-host")
    result = connect_client(store, provider="chatgpt")
    assert result["state"] == "blocked"
    assert not store.path.exists()


def test_connect_does_not_assume_a_client_device_is_the_memory_host(tmp_path):
    store = Store(tmp_path / "client-device")
    with pytest.raises(InputError, match="chosen memory host"):
        connect_client(store, provider="cursor", config=tmp_path / "cursor.json")
    assert not store.path.exists()


def test_down_does_not_stop_a_reused_or_unrelated_process(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        (store.directory / "service.json").write_text(
            json.dumps({"pid": process.pid, "process_start": "incorrect"})
        )
        down(store)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)
