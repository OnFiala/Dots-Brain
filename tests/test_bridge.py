import asyncio
import json
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dots_brain.auth import issue_client, revoke_client
from dots_brain.bridge import call_remote, verify_connection, verify_session
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
    from dots_brain.errors import InputError
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
    with pytest.raises(InputError):
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


def test_real_http_process_and_stdio_bridge_share_one_store(tmp_path, monkeypatch):
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
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(variable, "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
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
                    from dots_brain import __version__
                    from dots_brain.protocol import INSTRUCTIONS

                    initialized = await session.initialize()
                    assert initialized.serverInfo.version == __version__
                    assert initialized.instructions == INSTRUCTIONS
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
                    other = store.remember(
                        content="Another bridge result.",
                        source="test",
                        account="local",
                        event_id="second",
                        project="bridge-test",
                    )
                    ids = [saved.structuredContent["id"], other["id"]] * 3
                    replies = await asyncio.gather(
                        *[session.call_tool("memory_get", {"memory_id": key}) for key in ids]
                    )
                    assert [reply.structuredContent["id"] for reply in replies] == ids

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


def test_parallel_calls_own_sessions_and_keep_out_of_order_results(monkeypatch, tmp_path):
    opened, completed = [], []

    async def exercise():
        second_done = asyncio.Event()

        class Session:
            async def call_tool(self, name, arguments):
                number = arguments["number"]
                if number == 1:
                    await second_done.wait()
                else:
                    second_done.set()
                completed.append(number)
                return (self, number)

        @asynccontextmanager
        async def connection(path):
            session = Session()
            opened.append(session)
            yield session

        monkeypatch.setattr("dots_brain.bridge.connect", connection)
        return await asyncio.gather(
            *[
                call_remote(tmp_path / "connection", "memory_get", {"number": number})
                for number in (1, 2)
            ]
        )

    replies = asyncio.run(asyncio.wait_for(exercise(), timeout=2))
    assert len(set(opened)) == 2 and completed == [2, 1]
    assert [number for _, number in replies] == [1, 2]
    assert [session for session, _ in replies] == opened


@pytest.mark.parametrize(
    "name,code", [("memory_remember", "outcome_unknown"), ("memory_get", "service_unavailable")]
)
def test_remote_failure_is_safe_and_never_retried(monkeypatch, tmp_path, name, code):
    calls = []

    class Session:
        async def call_tool(self, tool, arguments):
            calls.append(tool)
            raise httpx.ReadError("synthetic-secret-canary")

    @asynccontextmanager
    async def connection(path):
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)
    result = asyncio.run(call_remote(tmp_path / "connection", name, {}))
    assert result.isError and result.structuredContent["error"]["code"] == code
    assert "synthetic-secret-canary" not in result.model_dump_json()
    assert calls == [name]


def test_default_verification_only_reads_status():
    from types import SimpleNamespace

    calls = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="memory_status")])

        async def call_tool(self, name, arguments):
            calls.append(name)
            return SimpleNamespace(isError=False)

    result = asyncio.run(verify_session(Session()))
    assert result["state"] == "verified_read" and result["write"] == "not_tested"
    assert calls == ["memory_status"]


def test_remote_commit_with_lost_response_is_unknown_and_not_retried(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/mcp"
    credential = tmp_path / "client.json"
    issue_client(
        store,
        name="lost-response",
        scopes=["memory:read", "memory:write"],
        projects=["synthetic"],
        days=1,
        output=credential,
        url=endpoint,
    )
    program = """
import os, runpy
from dots_brain.service import MemoryService
original = MemoryService.mutate
def commit_then_exit(self, *args, **kwargs):
    original(self, *args, **kwargs)
    os._exit(0)
MemoryService.mutate = commit_then_exit
runpy.run_module('dots_brain.cli', run_name='__main__')
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            program,
            "--data-dir",
            str(store.directory),
            "serve",
            "--transport",
            "http",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(timeout=1, trust_env=False) as http:
            deadline = time.monotonic() + 10
            while True:
                try:
                    assert http.post(endpoint, json={}).status_code == 401
                    break
                except httpx.ConnectError:
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(0.05)
        result = asyncio.run(
            call_remote(
                credential,
                "memory_remember",
                {
                    "content": "A committed synthetic fact with a lost response.",
                    "source": "synthetic",
                    "account": "test",
                    "event_id": "lost-response",
                    "project": "synthetic",
                },
            )
        )
        assert result.isError and result.structuredContent["error"]["code"] == "outcome_unknown"
        process.wait(timeout=5)
        with store.connection() as db:
            rows = db.execute("SELECT event_id,current_revision FROM memories").fetchall()
            assert [tuple(row) for row in rows] == [("lost-response", 1)]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
