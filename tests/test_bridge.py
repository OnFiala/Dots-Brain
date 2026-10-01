import asyncio
import json
import socket
import subprocess
import sys
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dots_brain.auth import issue_client, revoke_client
from dots_brain.bridge import verify_connection
from dots_brain.store import Store


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
