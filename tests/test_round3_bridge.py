"""Public synthetic regressions for request isolation and caller policy."""

import asyncio
import json
import socket
import threading
import time
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dots_brain.auth import BearerAuth, issue_client
from dots_brain.bridge import BridgeFailure, BridgeSession, verify_connection
from dots_brain.errors import InputError
from dots_brain.protocol import safe_error
from dots_brain.server import create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store


def test_oversize_is_rejected_before_queue_and_connection():
    async def exercise():
        remote = BridgeSession(None)
        remote.REQUEST_TIMEOUT = 0.05
        with pytest.raises(BridgeFailure) as caught:
            await remote.request("audit_record", {"details": "é" * 600000})
        assert caught.value.code == "request_too_large"
        assert not remote.pending and remote.queue.empty()
        assert "é" not in str(caught.value)

    asyncio.run(exercise())


def test_transport_413_is_safe_and_request_local(monkeypatch):
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append(name)
            if name == "audit_record":
                request = httpx.Request("POST", "http://127.0.0.1/mcp")
                raise httpx.HTTPStatusError(
                    "PRIVATE_HTTP_CANARY",
                    request=request,
                    response=httpx.Response(413, request=request),
                )
            return name

    opens = []

    @asynccontextmanager
    async def connection(_):
        opens.append(1)
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)

    async def exercise():
        remote = BridgeSession(None)
        worker = asyncio.create_task(remote.run())
        try:
            outcomes = await asyncio.gather(
                remote.request("audit_record", {}),
                remote.request("memory_status", {}),
                return_exceptions=True,
            )
            assert isinstance(outcomes[0], BridgeFailure)
            assert outcomes[0].code == "request_too_large"
            assert outcomes[1] == "memory_status"
            assert await remote.request("memory_status", {}) == "memory_status"
            assert opens == [1]
            assert "PRIVATE_HTTP_CANARY" not in str(outcomes[0])
        finally:
            await remote.close()
            await worker

    asyncio.run(exercise())
    assert calls.count("audit_record") == 1


def test_exact_budget_is_rechecked_with_sdk_id_before_forwarding(monkeypatch):
    import dots_brain.bridge as bridge

    calls = []

    class Session:
        _request_id = 123456789

        async def call_tool(self, name, arguments):
            calls.append(name)
            return name

    @asynccontextmanager
    async def connection(_):
        yield Session()

    monkeypatch.setattr(bridge, "connect", connection)
    empty = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "tools/call",
        "params": {"name": "audit_record", "arguments": {"details": ""}},
    }
    overhead = len(httpx.Request("POST", "http://127.0.0.1/mcp", json=empty).content)
    arguments = {"details": "x" * (1048576 - overhead)}
    exact = empty | {"params": {"name": "audit_record", "arguments": arguments}}
    assert len(httpx.Request("POST", "http://127.0.0.1/mcp", json=exact).content) == 1048576

    async def exercise():
        remote = BridgeSession(None)
        remote.REQUEST_TIMEOUT = 0.1
        oversized_at_send = asyncio.create_task(remote.request("audit_record", arguments))
        await asyncio.sleep(0)
        assert remote.queue.qsize() == 1  # Exact initial budget accepted before queue.
        worker = asyncio.create_task(remote.run())
        try:
            with pytest.raises(BridgeFailure) as caught:
                await oversized_at_send
            assert caught.value.code == "request_too_large"
            assert await remote.request("memory_status", {}) == "memory_status"
            assert calls == ["memory_status"]
        finally:
            await remote.close()
            await worker

    asyncio.run(exercise())


def test_unserializable_request_never_enters_queue():
    async def exercise():
        remote = BridgeSession(None)
        remote.REQUEST_TIMEOUT = 0.05
        with pytest.raises(InputError):
            await remote.request("audit_record", {"details": object()})
        assert remote.queue.empty() and not remote.pending

    asyncio.run(exercise())


def test_absolute_deadline_includes_local_budget_check(monkeypatch):
    def slow_check(*args, **kwargs):
        time.sleep(0.03)

    monkeypatch.setattr("dots_brain.bridge._check_request_size", slow_check)

    async def exercise():
        remote = BridgeSession(None)
        remote.REQUEST_TIMEOUT = 0.01
        with pytest.raises(BridgeFailure) as caught:
            await remote.request("memory_status", {})
        assert caught.value.code == "timed_out"
        assert remote.queue.empty() and not remote.pending

    asyncio.run(exercise())


def test_expired_deadline_before_send_never_sends_mutation(monkeypatch):
    checks, sends = [], []

    def check(*args, **kwargs):
        checks.append(1)
        if len(checks) == 2:
            time.sleep(0.03)

    class Session:
        async def call_tool(self, name, arguments):
            sends.append(name)
            return name

    @asynccontextmanager
    async def connection(_):
        yield Session()

    monkeypatch.setattr("dots_brain.bridge._check_request_size", check)
    monkeypatch.setattr("dots_brain.bridge.connect", connection)

    async def exercise():
        remote = BridgeSession(None)
        remote.REQUEST_TIMEOUT = 0.01
        worker = asyncio.create_task(remote.run())
        try:
            with pytest.raises(BridgeFailure) as caught:
                await remote.request("memory_remember", {"event_id": "must-not-send"})
            assert caught.value.code == "timed_out"
            assert sends == []
        finally:
            await remote.close()
            await worker

    asyncio.run(exercise())


def test_shared_loss_marks_only_sent_writes_unknown_with_stable_reference(monkeypatch):
    sent = []
    started = asyncio.Event()

    class Session:
        async def call_tool(self, name, arguments):
            sent.append((name, arguments))
            if len(sent) == 2:
                started.set()
            await started.wait()
            if name == "memory_status":
                raise httpx.ConnectError("PRIVATE_TRANSPORT_CANARY")
            await asyncio.Event().wait()

    @asynccontextmanager
    async def connection(_):
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)

    async def exercise():
        remote = BridgeSession(None)
        remote.MAX_CONCURRENT = 2
        worker = asyncio.create_task(remote.run())
        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    remote.request("memory_remember", {"event_id": "sent"}),
                    remote.request("memory_status", {}),
                    remote.request("memory_remember", {"event_id": "unsent"}),
                    return_exceptions=True,
                ),
                2,
            )
            assert [outcome.code for outcome in outcomes] == [
                "outcome_unknown",
                "service_unavailable",
                "service_unavailable",
            ]
            error = safe_error(outcomes[0], operation="tools/call")
            assert len(error["reference"]) == 32
            assert error == safe_error(outcomes[0], operation="tools/call")
            assert "PRIVATE_TRANSPORT_CANARY" not in json.dumps(error)
            assert "Do not replay" in error["message"]
            assert sent == [
                ("memory_remember", {"event_id": "sent"}),
                ("memory_status", {}),
            ]
        finally:
            await remote.close()
            await worker

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["oversize", "timeout"])
def test_sdk_context_failure_preserves_sibling_request_outcomes(monkeypatch, failure):
    started = asyncio.Event()
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append(name)
            if len(calls) == 2:
                started.set()
            await asyncio.Event().wait()

    async def transport_failure():
        await started.wait()
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE_CONTEXT_CANARY")
        request = httpx.Request("POST", "http://127.0.0.1/mcp")
        raise httpx.HTTPStatusError(
            "PRIVATE_CONTEXT_CANARY",
            request=request,
            response=httpx.Response(413, request=request),
        )

    @asynccontextmanager
    async def connection(_):
        async with asyncio.TaskGroup() as transport:
            transport.create_task(transport_failure())
            yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)

    async def exercise():
        remote = BridgeSession(None)
        remote.MAX_CONCURRENT = 2
        worker = asyncio.create_task(remote.run())
        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    remote.request("memory_remember", {}),
                    remote.request("memory_status", {}),
                    remote.request("memory_remember", {}),
                    return_exceptions=True,
                ),
                1,
            )
            assert [outcome.code for outcome in outcomes] == [
                "outcome_unknown",
                "service_unavailable",
                "service_unavailable",
            ]
            assert calls == ["memory_remember", "memory_status"]
        finally:
            await remote.close()
            await worker

    asyncio.run(exercise())


def test_sent_write_timeout_keeps_ambiguity_and_unsent_deadline(monkeypatch):
    sent = []

    class Session:
        async def call_tool(self, name, arguments):
            sent.append(arguments["event_id"])
            await asyncio.Event().wait()

    @asynccontextmanager
    async def connection(_):
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)

    async def exercise():
        remote = BridgeSession(None)
        remote.MAX_CONCURRENT = 1
        remote.REQUEST_TIMEOUT = 0.05
        worker = asyncio.create_task(remote.run())
        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    remote.request("memory_remember", {"event_id": "first"}),
                    remote.request("memory_remember", {"event_id": "queued"}),
                    return_exceptions=True,
                ),
                1,
            )
            assert [outcome.code for outcome in outcomes] == ["outcome_unknown", "timed_out"]
            assert sent == ["first"]
        finally:
            await remote.close()
            await worker

    asyncio.run(exercise())


@asynccontextmanager
async def _http_session(app, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": "Bearer " + token},
    ) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@pytest.mark.parametrize("order", ["cold", "reader", "writer", "writer-reader"])
def test_forbidden_write_is_independent_of_other_callers_discovery(tmp_path, order):
    store = Store(tmp_path / "brain")
    store.initialize()
    tokens = {}
    for name, scopes in (("reader", ["memory:read"]), ("writer", ["memory:write"])):
        path = tmp_path / (name + ".json")
        issue_client(
            store,
            name=name,
            scopes=scopes,
            projects=["shared"],
            days=1,
            output=path,
            url="http://127.0.0.1:8765/mcp",
        )
        tokens[name] = json.loads(path.read_text())["token"]
    server = create_server(MemoryService(store), http=True)
    app = BearerAuth(server.streamable_http_app(), store)

    async def exercise():
        async with server.session_manager.run():
            async with _http_session(app, tokens["reader"]) as reader:
                async with _http_session(app, tokens["writer"]) as writer:
                    if order in {"writer", "writer-reader"}:
                        assert "memory_remember" in {
                            tool.name for tool in (await writer.list_tools()).tools
                        }
                    if order in {"reader", "writer-reader"}:
                        assert "memory_remember" not in {
                            tool.name for tool in (await reader.list_tools()).tools
                        }
                    denied = await reader.call_tool("memory_remember", {})
                    assert denied.isError
                    assert denied.structuredContent["error"]["code"] == "forbidden"
                    valid = dict(
                        content="Synthetic star chart",
                        source="test",
                        account="a",
                        event_id="allowed",
                        project="shared",
                    )
                    saved = await writer.call_tool("memory_remember", valid)
                    assert not saved.isError
                    invalid = await writer.call_tool("memory_remember", valid | {"extra": 1})
                    assert invalid.structuredContent["error"]["code"] == "invalid_input"

    asyncio.run(exercise())
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_sdk_loopback_oversize_does_not_erase_concurrent_committed_write_results(tmp_path):
    """Use actual HTTP sockets, pinned SDK client and a disposable memory service."""
    store = Store(tmp_path / "brain")
    store.initialize()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    credential = tmp_path / "bridge.json"
    issue_client(
        store,
        name="synthetic",
        scopes=["memory:read", "memory:write", "audit:write"],
        projects=["shared"],
        days=1,
        output=credential,
        url=f"http://127.0.0.1:{port}/mcp",
    )
    upstream = create_server(MemoryService(store), http=True, port=port)
    app = BearerAuth(upstream.streamable_http_app(), store)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    async def exercise():
        remote = BridgeSession(credential)
        worker = asyncio.create_task(remote.run())
        base = dict(
            content="Synthetic comet timetable", source="test", account="a", project="shared"
        )
        try:
            # Warm the one shared session, as a running bridge does through discovery.
            await remote.request(None)
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    remote.request("memory_remember", base | {"event_id": "one"}),
                    remote.request(
                        "audit_record",
                        {
                            "project": "shared",
                            "kind": "action",
                            "client_event_id": "oversize",
                            "details": {"text": "λ" * 600000},
                        },
                    ),
                    remote.request("memory_remember", base | {"event_id": "two"}),
                    return_exceptions=True,
                ),
                10,
            )
            assert not isinstance(outcomes[0], BaseException), outcomes[0]
            assert not outcomes[0].isError
            assert isinstance(outcomes[1], BridgeFailure)
            assert outcomes[1].code == "request_too_large"
            assert not isinstance(outcomes[2], BaseException), outcomes[2]
            assert not outcomes[2].isError
            status = await remote.request("memory_status", {})
            assert not status.isError
        finally:
            await remote.close()
            await worker

    try:
        asyncio.run(exercise())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert not thread.is_alive()
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


def _receipt(credential, *, state="written"):
    path = credential.with_name(credential.name + ".probe-recovery.json")
    data = dict(
        version=1,
        state=state,
        event_id="connection-probe-" + "a" * 32,
        project="shared",
        source="dots-brain-probe",
        account="connection-verifier",
    )
    if state == "written":
        data.update(memory_id="synthetic-probe", revision=3)
    path.write_text(json.dumps(data))
    return path, data


def test_boolean_probe_revision_is_not_an_observed_revision():
    from dots_brain.bridge import _matches_probe

    record = dict(
        id="synthetic",
        revision=True,
        event_id="probe",
        project="shared",
        source="dots-brain-probe",
        account="connection-verifier",
    )
    assert not _matches_probe(
        record, memory_id="synthetic", revision=1, event_id="probe", project="shared"
    )


def test_existing_receipt_blocks_write_without_replaying_or_overwriting(tmp_path, monkeypatch):
    credential = tmp_path / "client.json"
    receipt, _ = _receipt(credential)
    original = receipt.read_bytes()
    calls = []

    @asynccontextmanager
    async def connection(_):
        calls.append("connect")
        raise AssertionError("Unresolved probe must stop before another write session")
        yield

    monkeypatch.setattr("dots_brain.bridge.connect", connection)
    with pytest.raises(InputError) as caught:
        asyncio.run(verify_connection(credential, write=True, project="shared"))
    assert "--recover-probe" in str(caught.value)
    assert receipt.read_bytes() == original
    assert calls == []


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "id",
        "revision",
        "event_id",
        "project",
        "source",
        "account",
        "boolean_revision",
        "get_error",
    ],
)
def test_explicit_recovery_checks_every_identity_before_deletion(tmp_path, monkeypatch, mismatch):
    credential = tmp_path / "client.json"
    receipt, data = _receipt(credential)
    if mismatch == "boolean_revision":
        data["revision"] = 1
        receipt.write_text(json.dumps(data))
    original = receipt.read_bytes()
    calls = []

    class Result:
        isError = False
        structuredContent = {}

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            result = Result()
            if name == "memory_get":
                record = {
                    key: data[key]
                    for key in ("revision", "event_id", "project", "source", "account")
                }
                record["id"] = data["memory_id"]
                if mismatch == "boolean_revision":
                    record["revision"] = True
                elif mismatch == "revision":
                    record["revision"] = 8
                elif mismatch is not None:
                    record[mismatch] = "different"
                result.structuredContent = record
                result.isError = mismatch == "get_error"
            else:
                result.structuredContent = {"deleted": True}
            return result

    @asynccontextmanager
    async def connection(_):
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)
    result = asyncio.run(verify_connection(credential, recover_probe=True))
    if mismatch is None:
        assert calls == [
            ("memory_get", {"memory_id": "synthetic-probe"}),
            ("memory_forget", {"memory_id": "synthetic-probe", "expected_revision": 3}),
        ]
        assert result["probe_removed"] is True
        assert not receipt.exists()
    else:
        assert calls == [("memory_get", {"memory_id": "synthetic-probe"})]
        assert result["probe_removed"] == "pending_recovery"
        assert "--archive-probe-receipt" in result["next_command"]
        assert receipt.read_bytes() == original


def test_sending_recovery_needs_explicit_target_and_does_not_replay(tmp_path, monkeypatch):
    credential = tmp_path / "client.json"
    receipt, _ = _receipt(credential, state="sending")
    original = receipt.read_bytes()
    calls = []

    @asynccontextmanager
    async def connection(_):
        calls.append("connect")
        raise AssertionError("Unknown probe needs an explicit observed identity")
        yield

    monkeypatch.setattr("dots_brain.bridge.connect", connection)
    result = asyncio.run(verify_connection(credential, recover_probe=True))
    assert result["write"] == "unknown" and result["probe_removed"] == "unknown"
    assert "--probe-memory-id" in result["next_command"]
    assert "host" in result["inspection"].lower()
    assert "audit events" in result["inspection"]
    assert receipt.read_bytes() == original and calls == []


def test_read_verification_surfaces_preserved_recovery_route(tmp_path, monkeypatch):
    credential = tmp_path / "client.json"
    receipt, _ = _receipt(credential)
    original = receipt.read_bytes()

    class Session:
        async def list_tools(self):
            return type("Tools", (), {"tools": [type("Tool", (), {"name": "memory_status"})()]})()

        async def call_tool(self, *args):
            return type("Result", (), {"isError": False})()

    @asynccontextmanager
    async def connection(_):
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)
    result = asyncio.run(verify_connection(credential))
    assert result["state"] == "verified_read"
    assert result["probe_recovery"] == "pending"
    assert "--recover-probe" in result["next_command"]
    assert receipt.read_bytes() == original


@pytest.mark.parametrize(
    "content", ["not-json", "[]", '{"version": 2}', '{"version": true}', '{"event_id": "invalid"}']
)
def test_invalid_receipt_recovery_preserves_evidence_and_names_archive(tmp_path, content):
    credential = tmp_path / "client.json"
    receipt = credential.with_name(credential.name + ".probe-recovery.json")
    receipt.write_text(content)
    with pytest.raises(InputError) as caught:
        asyncio.run(verify_connection(credential, recover_probe=True))
    assert "--archive-probe-receipt" in str(caught.value)
    assert receipt.read_text() == content


def test_archive_is_private_exact_nonoverwriting_and_never_calls_service(tmp_path, monkeypatch):
    credential = tmp_path / "client.json"
    receipt, _ = _receipt(credential, state="sending")
    original = receipt.read_bytes()
    calls = []

    @asynccontextmanager
    async def connection(_):
        calls.append("connect")
        raise AssertionError("Archive is local acknowledgement, not deletion")
        yield

    monkeypatch.setattr("dots_brain.bridge.connect", connection)
    first = asyncio.run(verify_connection(credential, archive_probe_receipt=True))
    first_path = credential.parent / first["archive"]
    assert first_path.read_bytes() == original
    assert first_path.stat().st_mode & 0o777 == 0o600
    assert first["remaining_probe"] == "unknown"
    assert first["probe_removed"] == "unknown"
    assert not receipt.exists() and calls == []
    receipt.write_bytes(original)
    second = asyncio.run(verify_connection(credential, archive_probe_receipt=True))
    assert second["archive"] != first["archive"]
    assert first_path.read_bytes() == original
    assert (credential.parent / second["archive"]).read_bytes() == original
    # A subsequent write may use a genuinely fresh identity.
    from dots_brain.bridge import _probe_identity

    event_id, _ = _probe_identity(credential)
    assert event_id != "connection-probe-" + "a" * 32


def test_archive_collision_preserves_existing_archive_and_receipt(tmp_path, monkeypatch):
    credential = tmp_path / "client.json"
    receipt, _ = _receipt(credential)
    original = receipt.read_bytes()
    collision = receipt.with_name(receipt.name + ".archived-" + "a" * 32)
    collision.write_bytes(b"Earlier synthetic evidence")
    monkeypatch.setattr("dots_brain.bridge.uuid4", lambda: type("ID", (), {"hex": "a" * 32})())
    with pytest.raises(InputError) as caught:
        asyncio.run(verify_connection(credential, archive_probe_receipt=True))
    assert "--archive-probe-receipt" in str(caught.value)
    assert receipt.read_bytes() == original
    assert collision.read_bytes() == b"Earlier synthetic evidence"


def test_archive_rejects_symlink_without_reading_or_modifying_target(tmp_path):
    credential = tmp_path / "client.json"
    target = tmp_path / "untouched.json"
    target.write_bytes(b"Synthetic unrelated file")
    receipt = credential.with_name(credential.name + ".probe-recovery.json")
    receipt.symlink_to(target)
    with pytest.raises(InputError):
        asyncio.run(verify_connection(credential, archive_probe_receipt=True))
    assert receipt.is_symlink() and target.read_bytes() == b"Synthetic unrelated file"
    assert list(tmp_path.glob(receipt.name + ".archived-*")) == []


def test_sending_recovery_explicit_target_deletes_only_verified_probe(tmp_path, monkeypatch):
    credential = tmp_path / "client.json"
    receipt, data = _receipt(credential, state="sending")
    calls = []

    class Result:
        isError = False
        structuredContent = {}

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            result = Result()
            result.structuredContent = (
                data | {"id": "observed-probe", "revision": 8}
                if name == "memory_get"
                else {"deleted": True}
            )
            return result

    @asynccontextmanager
    async def connection(_):
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)
    result = asyncio.run(
        verify_connection(
            credential, recover_probe=True, probe_memory_id="observed-probe", probe_revision=8
        )
    )
    assert result["probe_removed"] is True and not receipt.exists()
    assert calls == [
        ("memory_get", {"memory_id": "observed-probe"}),
        ("memory_forget", {"memory_id": "observed-probe", "expected_revision": 8}),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"recover_probe": True, "write": True},
        {"archive_probe_receipt": True, "recover_probe": True},
        {"probe_memory_id": "id"},
        {"recover_probe": True, "probe_memory_id": "id"},
    ],
)
def test_recovery_rejects_ambiguous_options_with_next_command(tmp_path, kwargs):
    with pytest.raises(InputError) as caught:
        asyncio.run(verify_connection(tmp_path / "client.json", **kwargs))
    assert "dots-brain verify" in str(caught.value)
