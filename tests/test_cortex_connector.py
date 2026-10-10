import asyncio
import json
import socketserver
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from dots_brain.cortex_connector import (
    SCHEMA_SQL,
    CortexConnectionConfig,
    CortexConnector,
    CortexConnectorError,
    CortexWriteUncertain,
    SqliteCortexLedger,
    StreamableHttpCortexTransport,
    _read_token,
    setup_cortex_operations,
)
from dots_brain.errors import InputError, NotFoundError
from dots_brain.privacy import guard_content
from dots_brain.store import Store


def _monitoring_proxy():
    """Start a disposable proxy that records every attempted request."""
    requests = []

    class Proxy(socketserver.BaseRequestHandler):
        def handle(self):
            requests.append(self.request.recv(8192))
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Proxy)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return f"http://{host}:{port}", requests, server, thread


@dataclass(frozen=True)
class Policy:
    scopes: frozenset[str]
    projects: tuple[str, ...] | None
    principal: str

    def require(self, scope):
        if scope not in self.scopes:
            raise InputError("missing scope")


class Ledger:
    def __init__(self):
        self.rows = {}

    def get(self, operation_id):
        row = self.rows.get(operation_id)
        return None if row is None else dict(row)

    def list_owned(self, principal, project):
        return [
            dict(row)
            for row in self.rows.values()
            if row["principal"] == principal and row["local_project"] == project
        ]

    def create_planned(self, record):
        self.rows.setdefault(
            record["operation_id"], dict(record, state="planned", receipt_json=None)
        )

    def claim_sending(self, operation_id, request_digest):
        row = self.rows[operation_id]
        if row["state"] != "planned" or row["request_digest"] != request_digest:
            return False
        row["state"] = "sending"
        return True

    def mark_acknowledged(self, operation_id, *, receipt, upstream_object_id):
        self.rows[operation_id].update(
            state="acknowledged",
            receipt_json=json.dumps(receipt),
            upstream_object_id=upstream_object_id,
        )

    def mark_uncertain(self, operation_id, *, error_code):
        self.rows[operation_id].update(state="uncertain", error_code=error_code)

    def release_planned(self, operation_id, *, error_code):
        self.rows[operation_id].update(state="planned", error_code=error_code)


class Transport:
    def __init__(self, replies=None, failure=None):
        self.calls = []
        self.replies = replies or {}
        self.failure = failure

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.failure is not None:
            raise self.failure
        return self.replies.get(name, {"object_id": "ctx-object-1", "ok": True})


def config():
    return CortexConnectionConfig(
        endpoint="https://cortex.example.test/mcp",
        token_file=Path("/private/cortex-token"),
        project_mapping=(("alpha", "cortex-alpha"),),
    )


def policy(*scopes, projects=("alpha",)):
    return Policy(frozenset(scopes), projects, "oauth-grant:botter")


def connector(transport=None, ledger=None):
    return CortexConnector(
        config(),
        transport=transport or Transport(),
        ledger=ledger or Ledger(),
        content_guard=lambda _record: None,
    )


def test_read_requires_scope_and_project_before_upstream_call():
    transport = Transport()

    async def exercise():
        with pytest.raises(InputError):
            await connector(transport).read_context(
                policy=policy("cortex:write"), project="alpha", query="context"
            )
        with pytest.raises(NotFoundError):
            await connector(transport).read_context(
                policy=policy("cortex:read", projects=("beta",)), project="alpha", query="context"
            )
        with pytest.raises(NotFoundError):
            await connector(transport).read_context(
                policy=policy("cortex:read"), project="unmapped", query="context"
            )

    asyncio.run(exercise())
    assert transport.calls == []


def test_read_context_uses_only_mapped_project_and_is_bounded():
    transport = Transport(
        {
            "cortex_brief": {"cards": [{"object_id": "one", "content_text": "x" * 1000}]},
            "cortex_search": {"results": [{"object_id": "two", "snippet": "y" * 1000}]},
        }
    )

    result = asyncio.run(
        connector(transport).read_context(
            policy=policy("cortex:read"), project="alpha", query="context", max_chars=256
        )
    )

    assert [call[0] for call in transport.calls] == ["cortex_brief", "cortex_search"]
    assert all(call[1]["project_id"] == "cortex-alpha" for call in transport.calls)
    assert result["characters"] <= 256
    assert "cortex-alpha" in result["context"]


def test_read_only_client_cannot_write():
    transport = Transport()

    async def exercise():
        with pytest.raises(InputError):
            await connector(transport).write_note(
                policy=policy("cortex:read"),
                project="alpha",
                source_ref="dots://memory/1@1",
                title="Selected fact",
                content="safe content",
            )

    asyncio.run(exercise())
    assert transport.calls == []


def test_timeout_marks_uncertain_and_replays_idempotent_note_with_same_key():
    transport, ledger = Transport(failure=TimeoutError()), Ledger()
    service = connector(transport, ledger)

    async def exercise():
        kwargs = dict(
            policy=policy("cortex:write"),
            project="alpha",
            source_ref="dots://memory/1@1",
            title="Selected fact",
            content="safe content",
        )
        with pytest.raises(CortexWriteUncertain) as first:
            await service.write_note(**kwargs)
        with pytest.raises(CortexWriteUncertain) as second:
            await service.write_note(**kwargs)
        assert first.value.operation_id == second.value.operation_id

    asyncio.run(exercise())
    assert len(transport.calls) == 2
    row = next(iter(ledger.rows.values()))
    assert row["state"] == "uncertain"
    assert "safe content" not in json.dumps(row)


def test_note_reuses_stable_operation_receipt_and_origin_metadata():
    transport, ledger = Transport(), Ledger()
    service = connector(transport, ledger)

    async def exercise():
        kwargs = dict(
            policy=policy("cortex:write"),
            project="alpha",
            source_ref="dots://memory/1@1",
            title="Selected fact",
            content="safe content",
        )
        first = await service.write_note(**kwargs)
        second = await service.write_note(**kwargs)
        return first, second

    first, second = asyncio.run(exercise())
    assert len(transport.calls) == 1
    name, request = transport.calls[0]
    assert name == "cortex_record_note"
    assert request["project_id"] == "cortex-alpha"
    assert request["idempotency_key"] == first["operation_id"]
    assert "principal=oauth-grant:botter" in request["content"]
    assert "source_ref=dots://memory/1@1" in request["content"]
    assert first["retrievable_source_ref"] == "cortex://object/ctx-object-1"
    assert second["replayed"] is True


def test_same_operation_id_with_different_request_is_rejected():
    service = connector()

    async def exercise():
        common = dict(
            policy=policy("cortex:write"),
            project="alpha",
            source_ref="dots://memory/1@1",
            title="Selected fact",
            operation_id="cxo_fixed",
        )
        await service.write_note(**common, content="first")
        with pytest.raises(Exception) as exc:
            await service.write_note(**common, content="second")
        assert exc.value.code == "revision_conflict"

    asyncio.run(exercise())


def test_parent_schema_helper_installs_only_the_distinct_ledger_table():
    connection = sqlite3.connect(":memory:")
    try:
        setup_cortex_operations(connection)
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    assert names == {"cortex_operations"}
    assert "CREATE TABLE cortex_operations" in SCHEMA_SQL


def test_concurrent_sqlite_claim_sends_once_and_survives_reconstruction(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    barrier = threading.Barrier(2)
    local = threading.local()

    class RacedLedger(SqliteCortexLedger):
        def get(self, operation_id):
            row = super().get(operation_id)
            if not getattr(local, "checked", False):
                local.checked = True
                barrier.wait(timeout=5)
            return row

    transport = Transport()
    ledger = RacedLedger(store)
    service = connector(transport, ledger)
    arguments = dict(
        policy=policy("cortex:write"),
        project="alpha",
        source_ref="dots://memory/1@1",
        title="Selected",
        content="safe",
    )

    def send():
        try:
            return asyncio.run(service.write_note(**arguments))
        except CortexWriteUncertain:
            return None  # The first caller can still be sending.

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert any(result and result["state"] == "acknowledged" for result in results)
    assert len(transport.calls) == 1
    reopened = connector(transport, SqliteCortexLedger(store))
    assert asyncio.run(reopened.write_note(**arguments))["replayed"]
    assert len(transport.calls) == 1
    operation_id = next(result["operation_id"] for result in results if result)
    ledger.mark_uncertain(operation_id, error_code="stale_failure")
    assert SqliteCortexLedger(store).get(operation_id)["state"] == "acknowledged"


def test_read_and_receipt_boundaries_remove_upstream_secrets(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    transport = Transport(
        {
            "cortex_brief": {
                "cards": [
                    {
                        "object_id": "one",
                        "content_text": "Authorization: Basic SYNTHETIC_CREDENTIAL_VALUE",
                    }
                ]
            },
            "cortex_search": {"results": []},
            "cortex_record_note": {"event_id": "event-1", "echo": "sk-12345678901234567890"},
        }
    )
    service = CortexConnector(
        config(), transport=transport, ledger=SqliteCortexLedger(store), content_guard=guard_content
    )
    result = asyncio.run(
        service.read_context(policy=policy("cortex:read"), project="alpha", query="safe")
    )
    assert "SYNTHETIC_CREDENTIAL_VALUE" not in result["context"]
    assert "one" in result["context"] and result["partial"]
    with pytest.raises(InputError):
        asyncio.run(
            service.read_context(
                policy=policy("cortex:read"),
                project="alpha",
                query="Authorization: Basic SYNTHETIC_QUERY",
            )
        )
    assert len(transport.calls) == 2
    receipt = asyncio.run(
        service.write_note(
            policy=policy("cortex:write"),
            project="alpha",
            source_ref="dots://memory/1@1",
            title="Safe",
        )
    )
    assert receipt["acknowledgement_only"]
    with store.connection() as db:
        assert json.loads(
            db.execute("SELECT receipt_json FROM cortex_operations").fetchone()[0]
        ) == {"event_id": "event-1"}


def test_reconciliation_requires_actor_project_and_exact_origin():
    transport, ledger = Transport(failure=TimeoutError()), Ledger()
    service = connector(transport, ledger)
    caller = policy("cortex:write")
    with pytest.raises(CortexWriteUncertain) as error:
        asyncio.run(
            service.write_note(
                policy=caller, project="alpha", source_ref="dots://memory/1@1", title="Safe"
            )
        )
    operation_id = error.value.operation_id
    transport.failure = None
    transport.replies = {
        "cortex_search": {"results": [{"object_id": "obj-1"}]},
        "cortex_fetch": {
            "stored_object": {
                "project_id": "wrong",
                "content_text": operation_id + " dots://memory/1@1",
            }
        },
    }
    assert (
        asyncio.run(service.reconcile(policy=caller, project="alpha", operation_id=operation_id))[
            "reconciliation"
        ]
        == "indeterminate"
    )
    transport.replies["cortex_fetch"]["stored_object"]["project_id"] = "cortex-alpha"
    assert (
        asyncio.run(service.reconcile(policy=caller, project="alpha", operation_id=operation_id))[
            "reconciliation"
        ]
        == "indeterminate"
    )
    from dots_brain.cortex_connector import _origin

    transport.replies["cortex_fetch"]["stored_object"]["content_text"] = _origin(
        caller.principal, "dots://memory/1@1", operation_id
    )
    result = asyncio.run(
        service.reconcile(policy=caller, project="alpha", operation_id=operation_id)
    )
    assert result["reconciliation"] == "candidate_unverified"
    assert result["candidate_source_ref"] == "cortex://object/obj-1"
    with pytest.raises(NotFoundError):
        service.operation_status(
            policy=Policy(frozenset({"cortex:read"}), caller.projects, "different-bot"),
            project="alpha",
            operation_id=operation_id,
        )
    assert len([call for call in transport.calls if call[0] == "cortex_record_note"]) == 1


def test_connection_token_requires_private_owned_regular_file(tmp_path):
    token = tmp_path / "secret"
    token.write_text("synthetic-private-value\n")
    token.chmod(0o644)
    with pytest.raises(CortexConnectorError):
        _read_token(token)
    token.chmod(0o600)
    assert _read_token(token) == "synthetic-private-value"
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(CortexConnectorError):
        _read_token(link)


def test_real_streamable_http_transport_uses_pinned_sdk_client(tmp_path, monkeypatch):
    """Exercise the concrete mcp 1.30 transport against a disposable FastMCP server."""
    import socket

    import uvicorn
    from mcp.server.fastmcp import FastMCP

    upstream = FastMCP("synthetic-cortex", stateless_http=True)

    @upstream.tool()
    def cortex_brief(query: str, project_id: str):
        return {"query": query, "project_id": project_id, "cards": []}

    notes = []

    @upstream.tool()
    def cortex_record_note(project_id: str, title: str, content: str, idempotency_key: str):
        notes.append(
            {
                "project_id": project_id,
                "title": title,
                "content": content,
                "idempotency_key": idempotency_key,
            }
        )
        return {"event_id": "synthetic-note-1", "project_id": project_id}

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            upstream.streamable_http_app(), host="127.0.0.1", port=port, log_level="error"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    token = tmp_path / "token"
    token.write_text("synthetic-token\n")
    token.chmod(0o600)
    transport = StreamableHttpCortexTransport(
        CortexConnectionConfig(
            endpoint=f"http://127.0.0.1:{port}/mcp",
            token_file=token,
            project_mapping=(("alpha", "cortex-alpha"),),
        )
    )
    proxy, proxy_requests, proxy_server, proxy_thread = _monitoring_proxy()
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    try:
        assert (
            asyncio.run(
                transport.call_tool(
                    "cortex_brief", {"query": "synthetic", "project_id": "cortex-alpha"}
                )
            )["project_id"]
            == "cortex-alpha"
        )
        receipt = asyncio.run(
            transport.call_tool(
                "cortex_record_note",
                {
                    "project_id": "cortex-alpha",
                    "title": "Synthetic transport write",
                    "content": "No production data.",
                    "idempotency_key": "synthetic-note-operation",
                },
            )
        )
        assert receipt == {"event_id": "synthetic-note-1", "project_id": "cortex-alpha"}
        assert notes == [
            {
                "project_id": "cortex-alpha",
                "title": "Synthetic transport write",
                "content": "No production data.",
                "idempotency_key": "synthetic-note-operation",
            }
        ]
        assert proxy_requests == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        proxy_server.shutdown()
        proxy_server.server_close()
        proxy_thread.join(timeout=5)


@pytest.mark.parametrize(
    "mapping",
    [(), (("alpha", "shared"), ("beta", "shared")), (("alpha", "one"), ("alpha", "two"))],
)
def test_project_mapping_cannot_collapse_caller_boundaries(mapping):
    with pytest.raises(InputError):
        CortexConnectionConfig(
            endpoint="https://cortex.example.test/mcp",
            token_file=Path("/private/cortex-token"),
            project_mapping=mapping,
        )


@pytest.mark.parametrize("failure", ["transport", "acknowledgement", "readback"])
def test_receipt_storage_failure_reports_uncertain_without_resending(tmp_path, failure):
    store = Store(tmp_path / "brain")
    store.initialize()

    class FailedLedger(SqliteCortexLedger):
        fail_readback = False

        def mark_acknowledged(self, *args, **kwargs):
            if failure == "acknowledgement":
                raise sqlite3.OperationalError("synthetic unavailable storage")
            super().mark_acknowledged(*args, **kwargs)
            self.fail_readback = failure == "readback"

        def get(self, operation_id):
            if self.fail_readback:
                self.fail_readback = False
                raise sqlite3.OperationalError("synthetic failed readback")
            return super().get(operation_id)

        def mark_uncertain(self, *args, **kwargs):
            raise sqlite3.OperationalError("synthetic failed uncertainty receipt")

    transport = Transport(failure=TimeoutError() if failure == "transport" else None)
    service = connector(transport, FailedLedger(store))
    arguments = dict(
        policy=policy("cortex:write"),
        project="alpha",
        source_ref="dots://memory/1@1",
        title="Synthetic fact",
    )
    with pytest.raises(CortexWriteUncertain) as first:
        asyncio.run(service.write_note(**arguments))
    reopened = connector(transport, SqliteCortexLedger(store))
    if failure == "readback":
        receipt = asyncio.run(reopened.write_note(**arguments))
        assert receipt["operation_id"] == first.value.operation_id
        assert receipt["state"] == "acknowledged" and receipt["replayed"]
    else:
        with pytest.raises(CortexWriteUncertain) as retried:
            asyncio.run(reopened.write_note(**arguments))
        assert retried.value.operation_id == first.value.operation_id
    # If persisting uncertainty itself fails, the original ``sending`` record
    # remains intentionally blocking: it must never be replayed automatically.
    assert len(transport.calls) == 1


def test_reconciliation_preserves_original_acknowledgement(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    transport = Transport({"cortex_record_note": {"event_id": "event-ack-1"}})
    service = connector(transport, SqliteCortexLedger(store))
    caller = policy("cortex:write")
    receipt = asyncio.run(
        service.write_note(
            policy=caller, project="alpha", source_ref="dots://memory/1@1", title="Synthetic"
        )
    )
    transport.replies.update(
        cortex_search={"results": [{"object_id": "projected-object-1"}]},
        cortex_fetch={
            "stored_object": {
                "project_id": "cortex-alpha",
                "content_text": transport.calls[0][1]["content"],
            }
        },
    )
    result = asyncio.run(
        service.reconcile(policy=caller, project="alpha", operation_id=receipt["operation_id"])
    )
    assert result["receipt"] == {"event_id": "event-ack-1"}
    assert result["reconciliation"] == "candidate_unverified"
    assert len([call for call in transport.calls if call[0] == "cortex_record_note"]) == 1
