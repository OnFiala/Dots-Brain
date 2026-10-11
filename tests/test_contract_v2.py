"""D4, safe MCP boundaries and truthful memory mutation receipts."""

import asyncio
import hashlib
import json
import subprocess
import sys
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dots_brain import __version__
from dots_brain.activity import AuditLog
from dots_brain.auth import Policy, issue_client
from dots_brain.errors import ConflictError, InputError
from dots_brain.privacy import check_content, sanitize
from dots_brain.protocol import INSTRUCTIONS
from dots_brain.server import create_http_app, create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store

CANARY = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
UNCERTAIN = "password: rotation procedure pending"


@pytest.fixture
def contract_store(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    return store


def arguments(**changes):
    return dict(content=UNCERTAIN, source="test", account="owner", event_id="event") | changes


@asynccontextmanager
async def mcp_client(store, *, service=None, projects=("alpha",)):
    credential = store.directory / "test-connection.json"
    credential.unlink(missing_ok=True)
    issue_client(
        store,
        name="test",
        scopes=["memory:read", "memory:write", "memory:forget"],
        projects=list(projects) if projects is not None else None,
        days=1,
        output=credential,
        url="http://127.0.0.1:8765/mcp",
    )
    server = create_server(service or MemoryService(store), http=True)
    app = create_http_app(server, service or MemoryService(store))
    async with server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            trust_env=False,
            headers={"Authorization": "Bearer " + json.loads(credential.read_text())["token"]},
        ) as http:
            async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    yield session, initialized, http


def test_warning_preserves_source_digest_history_and_each_replay(contract_store):
    store = contract_store
    first = store.remember(**arguments())
    update = store.remember(**arguments(content=UNCERTAIN + ".", expected_revision=1))
    for result in (
        first,
        update,
        store.remember(**arguments()),
        store.remember(**arguments(content=UNCERTAIN + ".", expected_revision=1)),
    ):
        assert result["warnings"] == [{"code": "possible_secret", "category": "assignment"}]
    assert store.get(first["id"], revision=1)["content"] == UNCERTAIN
    assert store.get(first["id"])["content"] == UNCERTAIN + "."
    with store.connection() as db:
        assert [row[0] for row in db.execute("SELECT digest FROM revisions ORDER BY revision")] == [
            hashlib.sha256(json.dumps([text, "", None], ensure_ascii=False).encode()).hexdigest()
            for text in (UNCERTAIN, UNCERTAIN + ".")
        ]
    assert "warnings" not in store.remember(**arguments(content="A neutral fact", event_id="clean"))


@pytest.mark.parametrize(
    "text",
    [
        CANARY,
        "PREFIX_" + CANARY,
        "postgresql://user:SYNTHETIC_CREDENTIAL_VALUE@example.test/db",
        "DB_PASSWORD=SYNTHETIC_CREDENTIAL_VALUE",
        "Authorization: Bearer " + CANARY,
        "Cookie: session=SYNTHETIC_CREDENTIAL_VALUE",
        "-----BEGIN PRIVATE KEY-----\nSYNTHETIC_CREDENTIAL_VALUE",
    ],
)
def test_certain_secret_is_rejected_without_echo_or_storage(contract_store, text, caplog):
    with pytest.raises(InputError, match="Fields: content") as failure:
        contract_store.remember(**arguments(content=text))
    assert "SYNTHETIC_CREDENTIAL_VALUE" not in str(failure.value) + caplog.text
    assert CANARY not in str(failure.value) + caplog.text
    assert contract_store.status()["memories"] == 0


@pytest.mark.parametrize(
    "text", [UNCERTAIN, "token=identifier", "Authorization: permission requested"]
)
def test_uncertain_content_is_stored_verbatim_with_warning(contract_store, text):
    result = contract_store.remember(**arguments(content=text))
    assert result["warnings"] and contract_store.get(result["id"])["content"] == text
    assert text not in json.dumps(result["warnings"])


def test_sensitive_names_do_not_redact_token_counts_or_unrelated_words():
    value = {
        "token_count": 12,
        "tokenizer": "local",
        "secretariat": "meeting",
        "url": "https://example.test/?token_count=12",
    }
    assert sanitize(value).value == value
    assert not check_content(value)
    assert sanitize({"apiKey": CANARY, "db_password": CANARY}).value == {
        "apiKey": "[REDACTED]",
        "db_password": "[REDACTED]",
    }


@pytest.mark.parametrize(
    "key",
    ["session_token", "sessionToken", "request_cookie", "http_authorization", "service_credential"],
)
def test_prefixed_sensitive_fields_and_url_values_never_enter_audit(contract_store, key):
    sentinel = "SYNTHETIC_OPAQUE_VALUE_123"
    log = AuditLog(contract_store)
    log.record(
        Policy(),
        project="alpha",
        kind="action",
        client_event_id="sensitive-field",
        details={key: sentinel, "url": f"https://example.test/?{key}={sentinel}"},
    )
    assert sentinel not in json.dumps(log.events(Policy()))
    with contract_store.connection() as db:
        assert sentinel not in db.execute("SELECT details FROM audit_events").fetchone()[0]


def test_complete_filter_is_bounded_on_long_adversarial_inputs():
    # A child timeout also catches a regression that monopolizes the interpreter.
    code = """
from dots_brain.privacy import check_content, sanitize
from dots_brain.errors import InputError
patterns = ['password' + ' ' * 1000, 'eyJ' + 'a' * 16 + '-', '-----BEGIN PRIVATE KEY-----',
            'sk-' * 1000, 'http-' * 1000, 'token=' + 'a' * 1000]
for pattern in patterns:
    text = (pattern * ((32000 + len(pattern) - 1) // len(pattern)))[:32000]
    sanitize(text, max_text=32000)
    try: check_content({'content': text})
    except InputError: pass
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, timeout=5)


@pytest.mark.parametrize("phase", ["intent", "receipt_success", "receipt_failed"])
def test_audit_failure_never_misreports_memory_outcome(contract_store, monkeypatch, caplog, phase):
    service = MemoryService(contract_store)
    observed = service.audit.observed
    original = ConflictError("Content changed; supply the current expected_revision.")

    def audit(*args, **kwargs):
        if kwargs["kind"] == ("intent" if phase == "intent" else "receipt"):
            raise OSError(CANARY)
        return observed(*args, **kwargs)

    def mutation(**kwargs):
        if phase == "receipt_failed":
            raise original
        return contract_store.remember(**kwargs)

    monkeypatch.setattr(service.audit, "observed", audit)

    def invoke():
        return service.mutate(
            mutation,
            policy=Policy(),
            audit_project="default",
            action="memory_remember",
            **arguments(content="A neutral fact"),
        )

    if phase == "intent":
        with pytest.raises(OSError):
            invoke()
        assert contract_store.status()["memories"] == 0
    elif phase == "receipt_failed":
        with pytest.raises(ConflictError) as failure:
            invoke()
        assert failure.value is original and contract_store.status()["memories"] == 0
    else:
        result = invoke()
        assert result["changed"] is True
        assert result["audit_warning"]["code"] == "audit_receipt_missing"
        assert contract_store.get(result["id"])["content"] == "A neutral fact"
        assert (
            service.audit.report(Policy(), project="default")["coverage"]["missing_receipts"] == 1
        )
    assert CANARY not in caplog.text


def test_two_mcp_clients_observe_warning_content_and_revision(contract_store):
    async def exercise():
        async with mcp_client(contract_store) as (first, _, _):
            saved = await first.call_tool("memory_remember", arguments())
            assert not saved.isError
            assert saved.structuredContent["warnings"]
            memory_id = saved.structuredContent["id"]
        async with mcp_client(contract_store) as (second, _, _):
            found = await second.call_tool("memory_get", {"memory_id": memory_id})
            assert found.structuredContent["content"] == UNCERTAIN
            assert found.structuredContent["project"] == "alpha"
            updated = await second.call_tool(
                "memory_remember", arguments(content=UNCERTAIN + ".", expected_revision=1)
            )
            assert (
                updated.structuredContent["revision"] == 2 and updated.structuredContent["warnings"]
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["validation", "unexpected", "unknown_tool"])
def test_mcp_errors_and_logs_never_echo_input_or_exception(
    contract_store, monkeypatch, caplog, mode
):
    if mode == "unexpected":

        def fail(**kwargs):
            raise OSError("private path and token " + CANARY)

        monkeypatch.setattr(contract_store, "remember", fail)

    async def exercise():
        async with mcp_client(contract_store) as (session, _, _):
            tool = CANARY if mode == "unknown_tool" else "memory_remember"
            data = arguments(content=[CANARY] if mode == "validation" else "A neutral fact")
            result = await session.call_tool(tool, data)
            assert result.isError and result.structuredContent["error"]["code"] in {
                "invalid_input",
                "internal_error",
            }
            assert CANARY not in result.model_dump_json()

    asyncio.run(exercise())
    assert CANARY not in caplog.text


def test_mcp_version_instructions_limits_and_legacy_argument_conversion(contract_store):
    async def exercise():
        async with mcp_client(contract_store) as (session, initialized, _):
            assert initialized.serverInfo.version == __version__
            assert initialized.instructions == INSTRUCTIONS
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            schema = tools["memory_remember"].inputSchema["properties"]
            assert schema["content"]["maxLength"] == 32000
            assert schema["source"]["maxLength"] == 200
            result = await session.call_tool("memory_remember", arguments(content="A neutral fact"))
            assert not result.isError
            updated = await session.call_tool(
                "memory_remember",
                arguments(
                    content="An updated fact", expected_revision="1", ignored_argument="legacy"
                ),
            )
            assert not updated.isError and updated.structuredContent["revision"] == 2
            search = await session.call_tool("memory_search", {"query": "fact", "limit": "2"})
            assert not search.isError

    asyncio.run(exercise())


@pytest.mark.parametrize("projects", [None, (), ("alpha", "beta")])
def test_implicit_write_project_requires_one_allowed_project(contract_store, projects):
    async def exercise():
        async with mcp_client(contract_store, projects=projects) as (session, _, _):
            result = await session.call_tool("memory_remember", arguments(content="A neutral fact"))
            assert result.isError is (projects is not None)
            if projects is None:
                assert contract_store.get(result.structuredContent["id"])["project"] == "default"
            else:
                assert contract_store.status()["memories"] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("chunked", [False, True])
def test_http_rejects_oversized_serialized_body(contract_store, chunked):
    async def exercise():
        async with mcp_client(contract_store) as (_, _, http):

            async def chunks():
                yield b" " * 70000
                yield b" " * 70000

            content = chunks() if chunked else b" " * 140000
            response = await http.post(
                "http://127.0.0.1:8765/mcp",
                content=content,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert response.status_code == 413
            assert contract_store.status()["memories"] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("variant", ["request", "notification", "envelope"])
def test_invalid_mcp_envelopes_never_echo_payload(contract_store, caplog, variant):
    async def exercise():
        async with mcp_client(contract_store) as (_, _, http):
            payload = {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": "memory_remember", "arguments": [CANARY]},
            }
            if variant == "notification":
                payload = {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"progress": [CANARY]},
                }
            elif variant == "envelope":
                payload["jsonrpc"] = CANARY
            response = await http.post(
                "http://127.0.0.1:8765/mcp",
                json=payload,
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert response.status_code in {200, 202, 400}
            assert CANARY not in response.text

    asyncio.run(exercise())
    assert CANARY not in caplog.text


def test_stdio_invalid_envelope_logs_are_safe_and_next_request_works(contract_store):
    import select

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dots_brain.cli",
            "--data-dir",
            str(contract_store.directory),
            "serve",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        invalid = {"jsonrpc": CANARY, "id": 0, "method": "ping"}
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "fixture", "version": "1"},
            },
        }
        process.stdin.write(json.dumps(invalid) + "\n")
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0], "stdio error notification timed out"
        notification = process.stdout.readline()
        assert CANARY not in notification
        assert json.loads(notification)["method"] == "notifications/message"
        process.stdin.write(json.dumps(initialize) + "\n")
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0], "stdio initialize timed out"
        response = process.stdout.readline()
        assert json.loads(response)["result"]["serverInfo"]["version"] == __version__
    finally:
        process.terminate()
        try:
            output, errors = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, errors = process.communicate(timeout=5)
    assert CANARY not in response + output + errors
    assert "payload omitted" in errors
