import asyncio
import json
import socket
import subprocess
import sys
import time

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dots_brain.auth import issue_client, revoke_client
from dots_brain.bridge import verify_connection
from dots_brain.store import Store


def test_local_resume_rejects_missing_store_before_startup(tmp_path, monkeypatch):
    from dots_brain.bridge import resume_local_connection
    from dots_brain.errors import InputError

    monkeypatch.setattr("dots_brain.runtime.up", lambda *_: pytest.fail("Must not start"))
    missing = tmp_path / "must-not-create"
    with pytest.raises(InputError):
        resume_local_connection(tmp_path / "no-credential.json", missing)
    assert not missing.exists()


@pytest.mark.parametrize("problem", ["other-store", "url", "revoked", "state", "disabled"])
def test_local_resume_rejects_mismatched_connection_before_startup(tmp_path, monkeypatch, problem):
    from dots_brain.bridge import resume_local_connection
    from dots_brain.errors import InputError, StoreDisabledError
    from dots_brain.local import write_json

    store = Store(tmp_path / "memory")
    store.initialize()
    other = Store(tmp_path / "other")
    other.initialize()
    path = tmp_path / "client.json"
    url = "http://127.0.0.1:8765/mcp"
    client = issue_client(
        other if problem == "other-store" else store,
        name="test",
        scopes=["memory:read"],
        projects=None,
        days=1,
        output=path,
        url="http://127.0.0.1:9999/mcp" if problem == "url" else url,
    )
    if problem != "state":
        write_json(store.directory / "service.json", {"url": url, "port": 8765})
    if problem == "revoked":
        revoke_client(store, client["client_id"])
    if problem == "disabled":
        write_json(store.directory / "disabled.json", {"disabled": True})
    monkeypatch.setattr("dots_brain.runtime.up", lambda *_: pytest.fail("Must not start"))
    with pytest.raises((InputError, StoreDisabledError)):
        resume_local_connection(path, store.directory)
    assert store.status()["memories"] == 0
    assert not (store.directory / "probe.connection.json").exists()


def test_local_resume_accepts_an_existing_matching_store(tmp_path, monkeypatch):
    from dots_brain.bridge import resume_local_connection
    from dots_brain.local import write_json

    store = Store(tmp_path / "memory")
    store.initialize()
    path = tmp_path / "client.json"
    url = "http://127.0.0.1:8765/mcp"
    issue_client(
        store,
        name="test",
        scopes=["memory:read"],
        projects=["work"],
        days=1,
        output=path,
        url=url,
    )
    write_json(store.directory / "service.json", {"url": url, "port": 8765})
    calls = []
    monkeypatch.setattr("dots_brain.runtime.up", lambda selected: calls.append(selected.path))
    resume_local_connection(path, store.directory)
    assert calls == [store.path]


def test_real_http_process_and_stdio_bridge_share_one_store(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/mcp"
    credential = tmp_path / "client.json"
    client = issue_client(
        store,
        name="bridge",
        scopes=["memory:read", "memory:write"],
        projects=["bridge-test"],
        days=1,
        output=credential,
        url=endpoint,
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dots_brain.cli",
            "--data-dir",
            str(store.directory),
            "serve",
            "--transport",
            "http",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        with httpx.Client(timeout=1, trust_env=False) as http:
            deadline = time.monotonic() + 10
            while True:
                try:
                    assert http.post(endpoint, json={}).status_code == 401
                    break
                except httpx.ConnectError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise AssertionError("The local MCP server did not start.") from None
                    time.sleep(0.05)
        assert asyncio.run(verify_connection(credential))["read"] is True

        async def exercise():
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "dots_brain.cli",
                    "bridge",
                    "--credential-file",
                    str(credential),
                ],
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert "memory_remember" in {tool.name for tool in tools.tools}
                    saved = await session.call_tool(
                        "memory_remember",
                        dict(
                            content="The bridge uses the canonical database.",
                            source="test",
                            account="local",
                            event_id="bridge-event",
                            project="bridge-test",
                        ),
                    )
                    assert not saved.isError, saved
                    assert store.get(saved.structuredContent["id"])["event_id"] == "bridge-event"

        asyncio.run(asyncio.wait_for(exercise(), timeout=15))
        revoke_client(store, client["client_id"])
        token = json.loads(credential.read_text())["token"]
        with httpx.Client(timeout=2, trust_env=False) as http:
            assert (
                http.post(
                    endpoint, headers={"Authorization": "Bearer " + token}, json={}
                ).status_code
                == 401
            )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.stderr is not None:
                process.stderr.close()
