import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dots_brain import capture_delivery
from dots_brain.auth import BearerAuth, issue_client
from dots_brain.cortex_connector import CortexConnectionConfig, CortexConnector, SqliteCortexLedger
from dots_brain.privacy import guard_content
from dots_brain.server import create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store


def test_scoped_mcp_audit_capture_and_selected_cortex_publication(tmp_path, monkeypatch):
    store = Store(tmp_path / "brain")
    store.initialize()
    outside = store.remember(
        content="Outside source",
        source="synthetic",
        account="a",
        event_id="outside",
        project="beta",
    )
    credential = tmp_path / "collector.json"
    issued = issue_client(
        store,
        name="collector",
        projects=["alpha"],
        days=1,
        scopes=[
            "memory:read",
            "memory:write",
            "audit:read",
            "audit:write",
            "cortex:read",
            "cortex:write",
        ],
        output=credential,
        url="http://127.0.0.1:8765/mcp",
    )

    class Upstream:
        calls = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            if name in {"cortex_brief", "cortex_search"}:
                return {"summary": "Mapped context", "api_key": "UPSTREAM_SECRET_CANARY"}
            return {"event_id": "event-1"}

    upstream = Upstream()
    connector = CortexConnector(
        CortexConnectionConfig(
            "https://cortex.example.test/mcp",
            tmp_path / "unused-token",
            (("alpha", "mapped-alpha"),),
        ),
        transport=upstream,
        ledger=SqliteCortexLedger(store),
        content_guard=guard_content,
    )
    service = MemoryService(store, cortex=connector)
    server = create_server(service, http=True)
    app = BearerAuth(server.streamable_http_app(), store)
    transcript, cursor = tmp_path / "transcript.jsonl", tmp_path / "cursor.json"
    transcript.write_text(
        "\n".join(
            json.dumps(item)
            for item in [
                {
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": "Synthetic user fact"}]},
                },
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "text", "text": "PRIVATE_CANARY"}]},
                },
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "example",
                                "input": {"private": "PRIVATE_CANARY"},
                            }
                        ]
                    },
                },
            ]
        )
        + "\n"
    )

    async def exercise():
        async with server.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + json.loads(credential.read_text())["token"]},
            ) as http:
                async with streamable_http_client(
                    "http://127.0.0.1:8765/mcp", http_client=http
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        assert "cortex_publish" in {
                            tool.name for tool in (await session.list_tools()).tools
                        }
                        result = await session.call_tool(
                            "memory_remember",
                            {
                                "content": "Selected project fact",
                                "source": "synthetic",
                                "account": "bot",
                                "event_id": "selected",
                                "project": "alpha",
                            },
                        )
                        assert not result.isError
                        memory_id = result.structuredContent["id"]
                        published = await session.call_tool(
                            "cortex_publish", {"memory_id": memory_id, "revision": 1}
                        )
                        assert not published.isError
                        assert upstream.calls[0][1]["project_id"] == "mapped-alpha"
                        assert f"dots://memory/{memory_id}@1" in upstream.calls[0][1]["content"]
                        forbidden = await session.call_tool(
                            "cortex_publish", {"memory_id": outside["id"], "revision": 1}
                        )
                        assert forbidden.isError and len(upstream.calls) == 1

                        await session.call_tool(
                            "memory_remember",
                            {
                                "content": "New selected fact",
                                "source": "synthetic",
                                "account": "bot",
                                "event_id": "selected",
                                "project": "alpha",
                                "expected_revision": 1,
                            },
                        )
                        for kind, status in [("decision", None), ("outcome", "partial")]:
                            response = await session.call_tool(
                                "cortex_publish",
                                {
                                    "memory_id": memory_id,
                                    "revision": 1,
                                    "kind": kind,
                                    "status": status,
                                },
                            )
                            assert not response.isError
                            assert "Selected project fact" in upstream.calls[-1][1]["summary"]
                            assert "New selected fact" not in upstream.calls[-1][1]["summary"]
                        context = await session.call_tool(
                            "cortex_context",
                            {
                                "project": "alpha",
                                "query": "Synthetic project context",
                            },
                        )
                        assert not context.isError
                        assert context.structuredContent["cortex_project"] == "mapped-alpha"
                        assert "UPSTREAM_SECRET_CANARY" not in json.dumps(context.structuredContent)
                        assert all(
                            call[1]["project_id"] == "mapped-alpha" for call in upstream.calls
                        )

                        class LostCoverageAck:
                            failed = False

                            async def call_tool(self, name, arguments):
                                result = await session.call_tool(name, arguments)
                                if arguments.get("kind") == "coverage" and not self.failed:
                                    self.failed = True
                                    raise RuntimeError("Synthetic lost ACK after server commit")
                                return result

                        lossy = LostCoverageAck()

                        @asynccontextmanager
                        async def connection(_path):
                            yield lossy

                        monkeypatch.setattr(capture_delivery, "connect", connection)
                        parameters = dict(
                            paths=[transcript],
                            cursor=cursor,
                            credential=credential,
                            project="alpha",
                            account="bot",
                            kind="transcript",
                        )
                        first = await capture_delivery.collect_remote(**parameters)
                        assert first["forwarded"] == 3 and first["state"] == "capture_partial"
                        pending = json.loads(
                            cursor.with_name(cursor.name + ".coverage.json").read_text()
                        )
                        assert pending["client_event_id"].startswith("capture-run:")
                        assert (await capture_delivery.collect_remote(**parameters))[
                            "forwarded"
                        ] == 0
                        assert (
                            json.loads(cursor.with_name(cursor.name + ".coverage.json").read_text())
                            == {}
                        )
                        events = await session.call_tool("audit_events", {})
                        assert (
                            sum(
                                row["client_event_id"] == pending["client_event_id"]
                                for row in events.structuredContent["events"]
                            )
                            == 1
                        )
                        assert not events.isError
                        assert "PRIVATE_CANARY" not in json.dumps(events.structuredContent)
                        assert all(
                            row["principal"] == "local-client:" + issued["client_id"]
                            for row in events.structuredContent["events"]
                        )
                        report = await session.call_tool("audit_report", {})
                        assert report.structuredContent["coverage"]["gaps"] == 1
                        assert store.status(projects=("alpha",))["memories"] == 2
                        assert "PRIVATE_CANARY" not in json.dumps(list(store.export()))

                        # Crash after record ACK/checkpoint, before finalizing the pass journal.
                        with transcript.open("a") as stream:
                            stream.write(
                                json.dumps(
                                    {
                                        "role": "user",
                                        "message": {
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": "Fact after interrupted pass",
                                                }
                                            ]
                                        },
                                    }
                                )
                                + "\n"
                            )
                        real_write = capture_delivery.write_json
                        failed = False

                        def fail_finalization(path, value):
                            nonlocal failed
                            if value.get("details", {}).get("pass_completed") and not failed:
                                failed = True
                                raise OSError("Synthetic journal finalization failure")
                            real_write(path, value)

                        monkeypatch.setattr(capture_delivery, "write_json", fail_finalization)
                        with pytest.raises(OSError):
                            await capture_delivery.collect_remote(**parameters)
                        interrupted = json.loads(
                            cursor.with_name(cursor.name + ".coverage.json").read_text()
                        )
                        assert interrupted["details"]["pass_completed"] is False
                        assert interrupted["details"]["forwarded"] is None
                        recovered = await capture_delivery.collect_remote(**parameters)
                        assert recovered["forwarded"] == 0
                        assert store.status(projects=("alpha",))["memories"] == 3
                        rows = (await session.call_tool("audit_events", {})).structuredContent[
                            "events"
                        ]
                        assert (
                            sum(
                                row["client_event_id"] == interrupted["client_event_id"]
                                for row in rows
                            )
                            == 1
                        )

    asyncio.run(exercise())


def test_cortex_read_and_write_scopes_are_independent_over_mcp(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    clients = {}
    for scope in ("read", "write"):
        credential = tmp_path / f"{scope}.json"
        issued = issue_client(
            store,
            name=scope,
            projects=["alpha"],
            days=1,
            scopes=[f"cortex:{scope}"],
            output=credential,
            url="http://127.0.0.1:8765/mcp",
        )
        clients[scope] = credential
        with store.connection(write=True) as db:
            db.execute(
                "INSERT INTO cortex_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"cxo_operation-{scope}",
                    "digest",
                    "local-client:" + issued["client_id"],
                    "alpha",
                    "mapped-alpha",
                    "note",
                    "source",
                    "uncertain",
                    None,
                    None,
                    None,
                    "now",
                    "now",
                ),
            )

    class Upstream:
        calls = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"results": []}

    upstream = Upstream()
    connector = CortexConnector(
        CortexConnectionConfig(
            "https://cortex.example.test/mcp", tmp_path / "unused", (("alpha", "mapped-alpha"),)
        ),
        transport=upstream,
        ledger=SqliteCortexLedger(store),
        content_guard=guard_content,
    )
    server = create_server(MemoryService(store, cortex=connector), http=True)
    app = BearerAuth(server.streamable_http_app(), store)

    async def exercise():
        async with server.session_manager.run():
            for scope, credential in clients.items():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    headers={
                        "Authorization": "Bearer " + json.loads(credential.read_text())["token"]
                    },
                ) as http:
                    async with streamable_http_client(
                        "http://127.0.0.1:8765/mcp", http_client=http
                    ) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            args = {"project": "alpha", "operation_id": f"cxo_operation-{scope}"}
                            if scope == "write":
                                before = len(upstream.calls)
                                denied = await session.call_tool(
                                    "cortex_context", {"project": "alpha", "query": "context"}
                                )
                                assert denied.isError and len(upstream.calls) == before
                                assert (await session.call_tool("cortex_operation", args)).isError
                                allowed = await session.call_tool("cortex_reconcile", args)
                                assert not allowed.isError and len(upstream.calls) == before + 1
                            else:
                                assert not (
                                    await session.call_tool("cortex_operation", args)
                                ).isError
                                before = len(upstream.calls)
                                assert (await session.call_tool("cortex_reconcile", args)).isError
                                assert len(upstream.calls) == before
                            foreign = {
                                **args,
                                "operation_id": "cxo_operation-"
                                + ("write" if scope == "read" else "read"),
                            }
                            tool = "cortex_operation" if scope == "read" else "cortex_reconcile"
                            before = len(upstream.calls)
                            assert (await session.call_tool(tool, foreign)).isError
                            assert len(upstream.calls) == before

    asyncio.run(exercise())
