import asyncio
import json
import multiprocessing
import os
import socket
import socketserver
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from dots_brain import cortex_connector
from dots_brain.cortex_connector import (
    CortexConnectionConfig,
    CortexConnector,
    CortexConnectorError,
    CortexPreSendError,
    CortexWriteUncertain,
    SqliteCortexLedger,
    StreamableHttpCortexTransport,
    _read_token,
)
from dots_brain.errors import BusyError, InputError, NotFoundError
from dots_brain.local import locked
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
    return Policy(frozenset(scopes), projects, "oauth-grant:writer-a")


def connector(transport=None, ledger=None):
    return CortexConnector(
        config(),
        transport=transport or Transport(),
        ledger=ledger or Ledger(),
        content_guard=lambda _record: None,
    )


def _claim_then_exit(directory, operation_id, request_digest):
    """Simulate an uncatchable process death immediately after the durable claim."""
    ledger = SqliteCortexLedger(Store(Path(directory)))
    assert ledger.claim_sending(operation_id, request_digest)
    os._exit(0)


def _hold_writer_lease(directory, ready, release):
    with locked(Path(directory) / "writers.lock", timeout=0, shared=True):
        ready.set()
        release.wait(5)


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


def test_timeout_keeps_uncertain_note_blocked_without_an_implicit_retry():
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
    assert len(transport.calls) == 1
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
    assert "principal=oauth-grant:writer-a" in request["content"]
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


def test_concurrent_sqlite_claim_sends_once_and_survives_reconstruction(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    entered, release = threading.Event(), threading.Event()

    class BlockingTransport(Transport):
        async def call_tool(self, name, arguments):
            self.calls.append((name, dict(arguments)))
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            return {"object_id": "ctx-object-1"}

    transport = BlockingTransport()
    ledger = SqliteCortexLedger(store)
    service = connector(transport, ledger)
    arguments = dict(
        policy=policy("cortex:write"),
        project="alpha",
        source_ref="dots://memory/1@1",
        title="Selected",
        content="safe",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(lambda: asyncio.run(service.write_note(**arguments)))
        assert entered.wait(timeout=5)
        loser = pool.submit(lambda: asyncio.run(service.write_note(**arguments)))
        with pytest.raises(CortexWriteUncertain):
            loser.result(timeout=5)
        release.set()
        result = winner.result(timeout=5)
    assert result["state"] == "acknowledged"
    assert len(transport.calls) == 1
    reopened = connector(transport, SqliteCortexLedger(store))
    assert asyncio.run(reopened.write_note(**arguments))["replayed"]
    assert len(transport.calls) == 1
    operation_id = result["operation_id"]
    ledger.mark_uncertain(operation_id, error_code="stale_failure")
    assert SqliteCortexLedger(store).get(operation_id)["state"] == "acknowledged"


def test_planned_release_cannot_overwrite_an_active_sending_claim(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    ledger = SqliteCortexLedger(store)
    ledger.create_planned(
        {
            "operation_id": "cxo_claim-barrier",
            "request_digest": "digest-a",
            "principal": "oauth-grant:writer-a",
            "local_project": "alpha",
            "cortex_project": "cortex-alpha",
            "operation_kind": "note",
            "source_ref": "dots://memory/claim@1",
            "created_at": time.time(),
        }
    )
    assert ledger.claim_sending("cxo_claim-barrier", "digest-a")
    ledger.release_planned("cxo_claim-barrier", error_code="stale_release")
    assert ledger.get("cxo_claim-barrier")["state"] == "sending"
    assert not ledger.return_pre_send("cxo_claim-barrier", "wrong-digest", error_code="wrong_claim")
    assert ledger.get("cxo_claim-barrier")["state"] == "sending"
    assert ledger.return_pre_send("cxo_claim-barrier", "digest-a", error_code="connection_refused")
    assert ledger.get("cxo_claim-barrier")["state"] == "planned"


def test_owner_recovers_sending_after_sender_process_dies_without_replay(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    ledger = SqliteCortexLedger(store)
    transport = Transport()
    service = connector(transport, ledger)
    arguments = dict(
        policy=policy("cortex:write"),
        project="alpha",
        source_ref="dots://memory/crashed@1",
        title="Crash recovery",
        content="safe",
        operation_id="cxo_crashed-sender",
    )
    request = service._upstream_request(
        "note",
        "cortex-alpha",
        "oauth-grant:writer-a",
        arguments["source_ref"],
        {
            "title": arguments["title"],
            "summary": "",
            "content": arguments["content"],
            "topic": None,
        },
        arguments["operation_id"],
    )
    digest = cortex_connector._digest(request)
    ledger.create_planned(
        {
            "operation_id": arguments["operation_id"],
            "request_digest": digest,
            "principal": "oauth-grant:writer-a",
            "local_project": "alpha",
            "cortex_project": "cortex-alpha",
            "operation_kind": "note",
            "source_ref": arguments["source_ref"],
            "created_at": time.time(),
        }
    )
    process = multiprocessing.get_context("fork").Process(
        target=_claim_then_exit,
        args=(store.directory, arguments["operation_id"], digest),
    )
    process.start()
    process.join(timeout=5)
    assert process.exitcode == 0
    assert ledger.get(arguments["operation_id"])["state"] == "sending"

    owner = Policy(frozenset({"cortex:write"}), ("alpha",), "local-owner:stdio")
    recovered = service.resolve_operation(
        policy=owner,
        project="alpha",
        operation_id=arguments["operation_id"],
        resolution="recover-sending",
        writers_stopped=True,
    )
    assert recovered["state"] == "uncertain"
    assert recovered["resolution"] == "sending_recovered"
    assert recovered["retry_allowed"] is False
    with pytest.raises(CortexWriteUncertain):
        asyncio.run(service.write_note(**arguments))
    assert transport.calls == []


def test_sending_recovery_refuses_an_active_writer_lease(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    ledger = SqliteCortexLedger(store)
    ledger.create_planned(
        {
            "operation_id": "cxo_active-sender",
            "request_digest": "digest-a",
            "principal": "oauth-grant:writer-a",
            "local_project": "alpha",
            "cortex_project": "cortex-alpha",
            "operation_kind": "note",
            "source_ref": "dots://memory/active@1",
            "created_at": time.time(),
        }
    )
    assert ledger.claim_sending("cxo_active-sender", "digest-a")
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_writer_lease, args=(store.directory, ready, release))
    process.start()
    assert ready.wait(timeout=5)
    owner = Policy(frozenset({"cortex:write"}), ("alpha",), "local-owner:stdio")
    try:
        with pytest.raises(BusyError, match="writers.lock"):
            connector(ledger=ledger).resolve_operation(
                policy=owner,
                project="alpha",
                operation_id="cxo_active-sender",
                resolution="recover-sending",
                writers_stopped=True,
            )
    finally:
        release.set()
        process.join(timeout=5)
    assert process.exitcode == 0
    assert ledger.get("cxo_active-sender")["state"] == "sending"


def test_corrected_outcome_supersedes_only_a_proven_unsent_outcome(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    ledger = SqliteCortexLedger(store)
    common = dict(
        policy=policy("cortex:write"),
        project="alpha",
        source_ref="dots://memory/outcome@1",
        title="Corrected outcome",
        summary="safe",
    )
    with pytest.raises(CortexPreSendError):
        asyncio.run(
            connector(Transport(failure=CortexPreSendError("offline")), ledger).write_outcome(
                **common, status="partial"
            )
        )
    old = ledger.list_owned("oauth-grant:writer-a", "alpha")[0]
    assert old["state"] == "planned"
    assert old["error_code"] == "cortex_pre_send_failed"

    transport = Transport()
    result = asyncio.run(connector(transport, ledger).write_outcome(**common, status="success"))
    prior = ledger.get(old["operation_id"])
    assert result["state"] == "acknowledged"
    assert prior["state"] == "uncertain"
    assert prior["error_code"] == "superseded_cortex_pre_send_failed"
    owner = Policy(frozenset({"cortex:write"}), ("alpha",), "local-owner:stdio")
    with pytest.raises(InputError):
        connector(transport, ledger).resolve_operation(
            policy=owner, project="alpha", operation_id=old["operation_id"], resolution="retry"
        )
    assert not ledger.retry_uncertain(old["operation_id"], error_code="operator_authorized_retry")
    with pytest.raises(CortexWriteUncertain):
        asyncio.run(connector(transport, ledger).write_outcome(**common, status="partial"))
    assert len(transport.calls) == 1


def test_real_sdk_connection_refusal_is_pre_send_and_keeps_the_operation_retryable(tmp_path):
    """The MCP SDK wraps an unopened localhost connection in an ExceptionGroup."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = tmp_path / "token"
    token.write_text("synthetic-token\n")
    token.chmod(0o600)
    store = Store(tmp_path / "brain")
    store.initialize()
    service = CortexConnector(
        CortexConnectionConfig(
            endpoint=f"http://127.0.0.1:{port}/mcp",
            token_file=token,
            project_mapping=(("alpha", "cortex-alpha"),),
        ),
        transport=StreamableHttpCortexTransport(
            CortexConnectionConfig(
                endpoint=f"http://127.0.0.1:{port}/mcp",
                token_file=token,
                project_mapping=(("alpha", "cortex-alpha"),),
            )
        ),
        ledger=SqliteCortexLedger(store),
        content_guard=lambda _record: None,
    )
    arguments = dict(
        policy=policy("cortex:write"),
        project="alpha",
        source_ref="dots://memory/connection-refused@1",
        title="Synthetic decision",
        summary="No upstream request can reach a closed local port.",
    )
    for _ in range(2):
        with pytest.raises(CortexPreSendError):
            asyncio.run(service.write_decision(**arguments))
    row = SqliteCortexLedger(store).list_owned("oauth-grant:writer-a", "alpha")[0]
    assert row["state"] == "planned"
    assert row["error_code"] == "cortex_pre_send_failed"


def test_context_uses_the_available_6000_character_budget():
    transport = Transport(
        {
            "cortex_brief": {
                "cards": [
                    {"object_id": f"card-{index}", "content_text": "card " * 200}
                    for index in range(5)
                ]
            },
            "cortex_search": {
                "results": [
                    {"object_id": f"result-{index}", "content_text": "result " * 160}
                    for index in range(10)
                ]
            },
        }
    )
    result = asyncio.run(
        connector(transport).read_context(
            policy=policy("cortex:read"), project="alpha", query="synthetic", max_chars=6000
        )
    )
    assert 4_000 <= result["characters"] <= 6_000
    assert "card-0" in result["context"] and "result-0" in result["context"]


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
