"""Real MCP authorization and SQLite receipt boundaries for optional publishing."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dots_brain.auth import BearerAuth, Policy, issue_client
from dots_brain.cortex_connector import CortexConnectionConfig, CortexConnector, SqliteCortexLedger
from dots_brain.privacy import guard_content
from dots_brain.server import create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store


@asynccontextmanager
async def session(app, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), headers={"Authorization": "Bearer " + token}
    ) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (
            r,
            w,
            _,
        ):
            async with ClientSession(r, w) as client:
                await client.initialize()
                yield client


def fixture(tmp_path, upstream):
    store = Store(tmp_path / "brain")
    store.initialize()
    connector = CortexConnector(
        CortexConnectionConfig(
            "https://cortex.example.test/mcp",
            tmp_path / "unused",
            (("shared", "upstream-project"),),
        ),
        transport=upstream,
        ledger=SqliteCortexLedger(store),
        content_guard=guard_content,
    )
    service = MemoryService(store, cortex=connector)
    server = create_server(service, http=True)
    app = BearerAuth(server.streamable_http_app(), store)
    tokens = []
    for name in ("client-a", "client-b"):
        path = tmp_path / (name + ".json")
        issue_client(
            store,
            name=name,
            scopes=["memory:read", "cortex:read", "cortex:write"],
            projects=["shared"],
            days=1,
            output=path,
            url="http://127.0.0.1:8765/mcp",
        )
        tokens.append(json.loads(path.read_text())["token"])
    record = store.remember(
        content="Synthetic selected source",
        source="fixture",
        account="test",
        project="shared",
        event_id="one",
    )
    return service, server, app, tokens, {"memory_id": record["id"], "revision": 1}


def test_sqlite_receipts_stay_principal_scoped_and_local_owner_can_release_note(tmp_path):
    class Upstream:
        async def call_tool(self, name, arguments):
            raise RuntimeError("Response lost after possible commit")

    service, server, app, tokens, source = fixture(tmp_path, Upstream())

    async def exercise():
        async with server.session_manager.run():
            async with session(app, tokens[0]) as a:
                failed = await a.call_tool("cortex_publish", source)
                assert failed.isError
                operation = failed.structuredContent["error"]["operation_id"]
                query = {"project": "shared", "operation_id": operation}
                owned = await a.call_tool("cortex_operation", query)
                assert not owned.isError and owned.structuredContent["state"] == "uncertain"
            async with session(app, tokens[1]) as b:
                for tool in ("cortex_operation", "cortex_reconcile"):
                    result = await b.call_tool(tool, query)
                    assert result.isError
                    assert result.structuredContent["error"]["code"] == "not_found"
                listed = await b.call_tool("cortex_operations", {"project": "shared"})
                assert listed.structuredContent["operations"] == []
            owned = service.cortex.operations(policy=Policy(), project="shared")
            assert [item["operation_id"] for item in owned] == [operation]
            result = service.cortex.resolve_operation(
                policy=Policy(), project="shared", operation_id=operation, resolution="retry"
            )
            assert result["state"] == "planned"
            assert result["resolution"] == "retry_allowed"

    asyncio.run(exercise())


def test_acknowledged_publish_survives_audit_receipt_failure_without_duplicate(
    tmp_path, monkeypatch
):
    class Upstream:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"event_id": "synthetic-upstream-event"}

    upstream = Upstream()
    service, server, app, tokens, source = fixture(tmp_path, upstream)
    original = service.audit.observed

    def receipt_fails(*args, **kwargs):
        if kwargs.get("kind") == "receipt":
            raise OSError("Synthetic receipt disk failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.audit, "observed", receipt_fails)

    async def exercise():
        async with server.session_manager.run(), session(app, tokens[0]) as client:
            for _ in range(2):
                result = await client.call_tool("cortex_publish", source)
                assert not result.isError
                assert result.structuredContent["state"] == "acknowledged"
                assert result.structuredContent["audit_receipt"] == "pending"
            assert len(upstream.calls) == 1

    asyncio.run(exercise())
