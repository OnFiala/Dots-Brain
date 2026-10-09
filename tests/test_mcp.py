import asyncio
import json
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from dots_brain.auth import BearerAuth, issue_client, revoke_client
from dots_brain.server import create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store


def test_http_protocol_scopes_project_boundaries_and_revocation(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    writer_file, reader_file = tmp_path / "writer.json", tmp_path / "reader.json"
    writer = issue_client(
        store,
        name="writer",
        scopes=["memory:read", "memory:write"],
        projects=["alpha"],
        days=1,
        output=writer_file,
        url="http://127.0.0.1:8765/mcp",
    )
    issue_client(
        store,
        name="reader",
        scopes=["memory:read"],
        projects=["beta"],
        days=1,
        output=reader_file,
        url="http://127.0.0.1:8765/mcp",
    )
    token = json.loads(writer_file.read_text())["token"]
    reader_token = json.loads(reader_file.read_text())["token"]
    assert token not in json.dumps(writer)
    server = create_server(MemoryService(store), http=True)
    app = BearerAuth(server.streamable_http_app(), store)

    async def exercise():
        async with server.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
                response = await http.post("http://127.0.0.1:8765/mcp", json={})
                assert response.status_code == 401
                http.headers["Authorization"] = "Bearer " + token
                async with streamable_http_client(
                    "http://127.0.0.1:8765/mcp",
                    http_client=http,
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 5
                        assert "memory_forget" not in {tool.name for tool in tools.tools}
                        result = await session.call_tool(
                            "memory_remember",
                            dict(
                                content="A confidential project decision",
                                source="test",
                                account="a",
                                event_id="one",
                                project="alpha",
                            ),
                        )
                        assert not result.isError, result
                        memory_id = result.structuredContent["id"]
                        assert store.get(memory_id)["writer_principal"] == (
                            "local-client:" + writer["client_id"]
                        )
                        forbidden = await session.call_tool(
                            "memory_forget", {"memory_id": memory_id, "expected_revision": 1}
                        )
                        assert forbidden.isError
                http.headers["Authorization"] = "Bearer " + reader_token
                async with streamable_http_client(
                    "http://127.0.0.1:8765/mcp",
                    http_client=http,
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        search = await session.call_tool("memory_search", {"query": "confidential"})
                        assert search.structuredContent["results"] == []
                        denied = await session.call_tool("memory_get", {"memory_id": memory_id})
                        assert denied.isError
                revoke_client(store, writer["client_id"])
                http.headers["Authorization"] = "Bearer " + token
                response = await http.post("http://127.0.0.1:8765/mcp", json={})
                assert response.status_code == 401

    asyncio.run(exercise())


def test_two_http_clients_share_revisions_and_delete_only_the_observed_revision(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    tokens, identities = [], []
    for name in ("writer", "deleter"):
        path = tmp_path / f"{name}.json"
        issued = issue_client(
            store,
            name=name,
            scopes=["memory:read", "memory:write", "memory:forget"],
            projects=["shared"],
            days=1,
            output=path,
            url="http://127.0.0.1:8765/mcp",
        )
        tokens.append(json.loads(path.read_text())["token"])
        identities.append("local-client:" + issued["client_id"])
    server = create_server(MemoryService(store), http=True)
    app = BearerAuth(server.streamable_http_app(), store)

    async def exercise():
        async with server.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:

                async def call(client, tool, arguments):
                    http.headers["Authorization"] = "Bearer " + tokens[client]
                    async with streamable_http_client(
                        "http://127.0.0.1:8765/mcp",
                        http_client=http,
                    ) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            return await session.call_tool(tool, arguments)

                event = dict(
                    content="The first shared note",
                    source="test",
                    account="owner",
                    event_id="shared-note",
                    project="shared",
                )
                saved = await call(0, "memory_remember", event)
                assert not saved.isError
                memory_id = saved.structuredContent["id"]
                observed = await call(1, "memory_get", {"memory_id": memory_id})
                assert observed.structuredContent["content"] == event["content"]
                assert observed.structuredContent["writer_principal"] == identities[0]
                updated = await call(
                    0,
                    "memory_remember",
                    event
                    | {
                        "content": "A new decision after the other client read",
                        "expected_revision": 1,
                    },
                )
                assert updated.structuredContent["revision"] == 2
                for arguments in (
                    {"memory_id": memory_id},
                    {"memory_id": memory_id, "expected_revision": 1},
                    {"memory_id": memory_id, "expected_revision": True},
                    {"memory_id": memory_id, "expected_revision": "2"},
                ):
                    rejected = await call(1, "memory_forget", arguments)
                    assert rejected.isError
                    assert store.get(memory_id)["revision"] == 2
                current = await call(1, "memory_get", {"memory_id": memory_id})
                arguments = {
                    "memory_id": memory_id,
                    "expected_revision": current.structuredContent["revision"],
                }
                removed = await call(1, "memory_forget", arguments)
                assert removed.structuredContent["deleted"] is True
                with store.connection() as db:
                    assert (
                        db.execute("SELECT deleted_by FROM scoped_suppressions").fetchone()[0]
                        == identities[1]
                    )
                retry = await call(1, "memory_forget", arguments)
                assert retry.structuredContent["deleted"] is False
                reimport = await call(0, "memory_remember", event)
                assert reimport.isError
                assert store.status()["memories"] == 0

    asyncio.run(exercise())


def test_stdio_starts_and_reads_persisted_memory(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    saved = store.remember(
        content="The selected storage is SQLite.", source="test", account="a", event_id="one"
    )

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "dots_brain.cli",
                "--data-dir",
                str(store.directory),
                "serve",
            ],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("memory_get", {"memory_id": saved["id"]})
                assert not result.isError
                assert result.structuredContent["content"] == "The selected storage is SQLite."

    asyncio.run(exercise())
