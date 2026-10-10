import asyncio
import json
from contextlib import asynccontextmanager, suppress

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session

from dots_brain import __version__
from dots_brain.auth import BearerAuth, issue_client
from dots_brain.bridge import BridgeFailure, BridgeSession
from dots_brain.errors import ConflictError, NotFoundError, StoreDisabledError, SuppressedError
from dots_brain.protocol import INSTRUCTIONS, protect_tool_dispatch, safe_error
from dots_brain.server import create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store


@asynccontextmanager
async def client(store, tmp_path):
    credential = tmp_path / "reader.json"
    issue_client(
        store,
        name="test",
        scopes=["memory:read", "memory:write"],
        projects=None,
        days=1,
        output=credential,
        url="http://127.0.0.1:8765/mcp",
    )
    server = create_server(MemoryService(store), http=True)
    app = BearerAuth(server.streamable_http_app(), store)
    async with (
        server.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + json.loads(credential.read_text())["token"]},
        ) as http,
    ):
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                yield session, http, initialized


def test_strict_mcp_arguments_project_filter_and_declared_limits(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    source = dict(
        content="Synthetic protocol decision",
        source="test",
        account="a",
        event_id="a",
        project="null",
    )
    first = store.remember(**source)
    store.remember(**(source | {"project": "other"}))

    async def exercise():
        async with client(store, tmp_path) as (session, http, initialized):
            assert initialized.serverInfo.version == __version__
            assert initialized.instructions == INSTRUCTIONS
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools["memory_search"].inputSchema["properties"]["limit"]["maximum"] == 50
            assert (
                tools["memory_remember"].inputSchema["properties"]["content"]["maxLength"] == 32000
            )
            for value in (True, "1", 1.0, 2**63):
                result = await session.call_tool(
                    "memory_remember",
                    source | {"content": "Must not commit", "expected_revision": value},
                )
                assert result.isError
                assert result.structuredContent["error"]["code"] == "invalid_input"
                assert store.get(first["id"])["revision"] == 1
            result = await session.call_tool(
                "memory_search", {"query": "protocol", "project": "null"}
            )
            assert [row["project"] for row in result.structuredContent["results"]] == ["null"]
            # A valid escaped Unicode request exceeds the old 128 KiB transport cap.
            payload = {
                "jsonrpc": "2.0",
                "id": 991,
                "method": "tools/call",
                "params": {
                    "name": "memory_remember",
                    "arguments": source | {"content": "漢" * 32000, "event_id": "unicode"},
                },
            }
            raw = json.dumps(payload)
            assert len(raw) > 131072
            response = await http.post(
                "http://127.0.0.1:8765/mcp",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert response.status_code == 200
            assert response.json()["result"]["isError"] is False

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "error", [ConflictError, NotFoundError, StoreDisabledError, SuppressedError]
)
def test_mcp_returns_typed_domain_errors(tmp_path, monkeypatch, error):
    store = Store(tmp_path / "brain")
    store.initialize()

    def reject(*_, **__):
        raise error("A safe operator message.")

    monkeypatch.setattr(store, "get", reject)

    async def exercise():
        async with client(store, tmp_path) as (session, _, __):
            result = await session.call_tool("memory_get", {"memory_id": "unknown"})
            assert result.isError
            assert result.structuredContent["error"]["code"] == error.code

    asyncio.run(exercise())


def test_unexpected_error_does_not_leak_exception_or_log_payload(tmp_path, monkeypatch, caplog):
    store = Store(tmp_path / "brain")
    store.initialize()

    def reject(*_, **__):
        raise RuntimeError("PRIVATE_FAILURE_CANARY")

    monkeypatch.setattr(store, "get", reject)

    async def exercise():
        async with client(store, tmp_path) as (session, _, __):
            result = await session.call_tool("memory_get", {"memory_id": "unknown"})
            assert result.isError
            assert result.structuredContent["error"]["code"] == "internal_error"
            assert "PRIVATE_FAILURE_CANARY" not in result.model_dump_json()

    asyncio.run(exercise())
    assert "PRIVATE_FAILURE_CANARY" not in caplog.text
    assert "exception_type=RuntimeError" in caplog.text


def test_bridge_reuses_connection_and_does_not_retry_failed_mutation(monkeypatch):
    opens, calls = [], []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append(name)
            if name == "write":
                raise ExceptionGroup(
                    "unsafe request context", [httpx.ConnectError("PRIVATE_CANARY")]
                )
            return name

    @asynccontextmanager
    async def connection(_):
        opens.append(1)
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)

    async def exercise():
        remote = BridgeSession(None)
        worker = asyncio.create_task(remote.run())
        try:
            assert await remote.request("read") == "read"
            assert await remote.request("read") == "read"
            assert len(opens) == 1
            with pytest.raises(BridgeFailure) as caught:
                await remote.request("write")
            assert caught.value.code == "service_unavailable"
            assert "PRIVATE_CANARY" not in str(caught.value)
            assert await remote.request("read") == "read"
            assert len(opens) == 2
            assert calls.count("write") == 1
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    asyncio.run(exercise())


def test_bridge_error_categories_do_not_render_credentials():
    request = httpx.Request(
        "POST", "https://example.test/mcp", headers={"Authorization": "PRIVATE_CANARY"}
    )
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError("PRIVATE_CANARY", request=request, response=response)
    assert safe_error(ExceptionGroup("unsafe", [error]))["code"] == "credential_rejected"
    assert safe_error(httpx.ReadTimeout("PRIVATE_CANARY"))["code"] == "timed_out"
    assert "PRIVATE_CANARY" not in json.dumps(safe_error(error))


def test_cold_tool_call_after_disable_keeps_typed_error(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    server = create_server(MemoryService(store))

    async def exercise():
        async with create_connected_server_and_client_session(server) as session:
            (store.directory / "disabled.json").write_text("{}")
            # Deliberately omit tools/list: discovery itself now fails.
            result = await session.call_tool("memory_status", {})
            assert result.isError
            assert result.structuredContent["error"]["code"] == "store_disabled"

    asyncio.run(exercise())


def test_cold_bridge_discovery_failure_is_typed_and_does_not_send_tool():
    server = Server("Synthetic bridge")
    calls = []

    @server.list_tools()
    async def unavailable():
        raise httpx.ConnectError("PRIVATE_DISCOVERY_CANARY")

    @server.call_tool(validate_input=False)
    async def call(name, arguments):
        calls.append(name)

    protect_tool_dispatch(server)

    async def exercise():
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("memory_remember", {})
            assert result.isError
            assert result.structuredContent["error"]["code"] == "service_unavailable"
            assert "PRIVATE_DISCOVERY_CANARY" not in result.model_dump_json()

    asyncio.run(exercise())
    assert calls == []


def test_memory_context_mcp_returns_content_and_provenance(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    record = store.remember(
        content="The alpine observatory sends temperature readings every Tuesday.",
        source="contract-test",
        account="synthetic",
        event_id="context",
        project="shared",
    )

    async def exercise():
        async with client(store, tmp_path) as (session, _, __):
            result = await session.call_tool(
                "memory_context", {"task": "alpine observatory", "project": "shared"}
            )
            assert not result.isError
            value = result.structuredContent
            assert "temperature readings every Tuesday" in value["context"]
            assert value["memories"] == 1
            reference = json.loads(value["context"].splitlines()[1])
            assert reference == {
                "id": record["id"],
                "revision": 1,
                "source": "contract-test",
                "project": "shared",
            }
            invalid = await session.call_tool("memory_status", {"unexpected": "PRIVATE_CANARY"})
            assert invalid.structuredContent["error"]["code"] == "invalid_input"
            assert "PRIVATE_CANARY" not in invalid.model_dump_json()

    asyncio.run(exercise())
