"""Warning metadata must not change accepted memory or connector requests."""

import asyncio
import hashlib
import json
from dataclasses import dataclass

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dots_brain.auth import BearerAuth, issue_client
from dots_brain.cortex_connector import CortexConnectionConfig, CortexConnector, SqliteCortexLedger
from dots_brain.integration_tools import load_cortex
from dots_brain.server import create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store

UNCERTAIN_CONTENT = "password: rotation procedure pending"
WARNING = {"code": "possible_secret", "category": "assignment"}


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "brain")
    result.initialize()
    return result


def memory_arguments():
    return {
        "content": UNCERTAIN_CONTENT,
        "source": "test",
        "account": "owner",
        "event_id": "warning-memory",
        "project": "alpha",
    }


def assert_safe_warnings(result):
    assert result["warnings"]
    for warning in result["warnings"]:
        assert set(warning) == {"code", "category"}
        assert warning["code"] == "possible_secret"
        assert UNCERTAIN_CONTENT not in json.dumps(warning)


def test_store_warning_preserves_exact_content_digest_revisions_and_replay(store):
    arguments = memory_arguments()
    first = store.remember(**arguments)
    replay = store.remember(**arguments)
    updated_content = UNCERTAIN_CONTENT + "."
    update = store.remember(**(arguments | {"content": updated_content, "expected_revision": 1}))
    old_replay = store.remember(**arguments)
    update_replay = store.remember(
        **(arguments | {"content": updated_content, "expected_revision": 1})
    )

    for result in (first, replay, update, old_replay, update_replay):
        assert_safe_warnings(result)
        assert result["warnings"] == first["warnings"]
        assert result["id"] == first["id"]
    assert (first["revision"], update["revision"]) == (1, 2)
    assert replay["changed"] is False and replay["replayed_revision"] == 1
    assert old_replay["revision"] == 2 and old_replay["replayed_revision"] == 1
    assert update_replay["changed"] is False and update_replay["replayed_revision"] == 2
    assert store.get(first["id"], revision=1)["content"] == UNCERTAIN_CONTENT
    assert store.get(first["id"])["content"] == updated_content
    with store.connection() as db:
        rows = db.execute(
            "SELECT revision,content,digest FROM revisions ORDER BY revision"
        ).fetchall()
        assert len(rows) == 2
        for row, expected in zip(rows, (UNCERTAIN_CONTENT, updated_content), strict=True):
            assert row["content"] == expected
            assert (
                row["digest"]
                == hashlib.sha256(
                    json.dumps([expected, "", None], ensure_ascii=False).encode()
                ).hexdigest()
            )
        assert "possible_secret" not in "\n".join(db.iterdump())


def test_store_checks_each_request_once_and_omits_empty_warnings(store, monkeypatch):
    from dots_brain import privacy

    calls = []

    def check(record):
        calls.append(record.copy())
        return []

    monkeypatch.setattr(privacy, "check_content", check, raising=False)
    arguments = memory_arguments()
    first = store.remember(**arguments)
    replay = store.remember(**arguments)
    assert "warnings" not in first and "warnings" not in replay
    assert len(calls) == 2
    assert all(record["content"] == UNCERTAIN_CONTENT for record in calls)


def test_factory_injects_warning_aware_guard(store):
    from dots_brain import privacy

    (store.directory / "cortex.json").write_text(
        json.dumps(
            {
                "endpoint": "https://cortex.example.test/mcp",
                "token_file": str(store.directory / "unused-token"),
                "project_mapping": {"alpha": "mapped-alpha"},
            }
        )
    )
    service = load_cortex(store)
    assert service._content_guard is privacy.check_content


@dataclass(frozen=True)
class Policy:
    scopes: frozenset[str] = frozenset({"cortex:read", "cortex:write"})
    projects: tuple[str, ...] = ("alpha",)
    principal: str = "local-owner:stdio"

    def require(self, scope):
        assert scope in self.scopes


class Transport:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name.startswith("cortex_record_"):
            return {"object_id": "object-one", "event_id": "event-one"}
        return {"results": [{"object_id": "context-one", "snippet": "Synthetic context"}]}


def connector(store, guard, transport=None):
    return CortexConnector(
        CortexConnectionConfig(
            "https://cortex.example.test/mcp",
            store.directory / "unused-token",
            (("alpha", "mapped-alpha"),),
        ),
        transport=transport or Transport(),
        ledger=SqliteCortexLedger(store),
        content_guard=guard,
    )


@pytest.mark.parametrize("kind", ["note", "decision", "outcome"])
def test_connector_warning_is_visible_on_read_new_write_and_replay_without_payload_change(
    store, kind
):
    seen = []

    def guard(record):
        seen.append(dict(record))
        return [WARNING.copy()]

    transport = Transport()
    service = connector(store, guard, transport)
    text_field = "content" if kind == "note" else "summary"
    arguments = {
        "policy": Policy(),
        "project": "alpha",
        "source_ref": "dots://memory/warning@1",
        "title": "Synthetic warning",
        text_field: UNCERTAIN_CONTENT,
    }
    if kind == "outcome":
        arguments["status"] = "success"
    write = getattr(service, "write_" + kind)

    async def exercise():
        read = await service.read_context(policy=Policy(), project="alpha", query=UNCERTAIN_CONTENT)
        first = await write(**arguments)
        replay = await write(**arguments)
        return read, first, replay

    read, first, replay = asyncio.run(exercise())
    for result in (read, first, replay):
        assert result["warnings"] == [WARNING]
    assert first["replayed"] is False and replay["replayed"] is True
    assert first["request_digest"] == replay["request_digest"]
    writes = [(name, request) for name, request in transport.calls if "record" in name]
    assert len(writes) == 1
    request = writes[0][1]
    origin = (
        "[Dots Brain origin] principal=local-owner:stdio; "
        "source_ref=dots://memory/warning@1; "
        f"operation_id={first['operation_id']}"
    )
    assert request[text_field] == UNCERTAIN_CONTENT + "\n\n" + origin
    assert "warnings" not in request
    assert (
        first["request_digest"]
        == hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert seen[0] == {"content": UNCERTAIN_CONTENT}
    assert (
        sum(record.get("payload", {}).get(text_field) == UNCERTAIN_CONTENT for record in seen) == 2
    )
    assert len(seen) == 3  # Exactly one guard invocation per read/write/replay request.
    with store.connection() as db:
        assert "possible_secret" not in "\n".join(db.iterdump())


@pytest.mark.parametrize("guard", [lambda record: None, lambda record: record])
def test_connector_legacy_guard_results_remain_compatible(store, guard):
    service = connector(store, guard)

    async def exercise():
        read = await service.read_context(policy=Policy(), project="alpha", query="context")
        first = await service.write_note(
            policy=Policy(), project="alpha", source_ref="dots://memory/legacy@1", title="Legacy"
        )
        replay = await service.write_note(
            policy=Policy(), project="alpha", source_ref="dots://memory/legacy@1", title="Legacy"
        )
        return read, first, replay

    for result in asyncio.run(exercise()):
        assert "warnings" not in result


def test_connector_filters_unsafe_warning_metadata_and_preserves_request_guard_contract(store):
    receipt_warning = {"code": "possible_secret", "category": "token"}

    def guard(record):
        warnings = [
            {"code": "possible_secret", "category": UNCERTAIN_CONTENT},
            {"code": "possible_secret", "category": "token", "content": UNCERTAIN_CONTENT},
            {"code": "unexpected", "category": "token"},
            {"code": "possible_secret", "category": []},
        ]
        if "payload" in record:
            assert record["payload"]["title"] == "Receipt warning"
            warnings.extend([receipt_warning.copy(), receipt_warning.copy()])
        else:
            assert record == {"content": "context"}
        return warnings

    service = connector(store, guard)

    async def exercise():
        read = await service.read_context(policy=Policy(), project="alpha", query="context")
        arguments = {
            "policy": Policy(),
            "project": "alpha",
            "source_ref": "dots://memory/receipt@1",
            "title": "Receipt warning",
        }
        first = await service.write_note(**arguments)
        replay = await service.write_note(**arguments)
        return read, first, replay

    read, first, replay = asyncio.run(exercise())
    assert "warnings" not in read
    assert first["warnings"] == replay["warnings"] == [receipt_warning]
    assert replay["replayed"] is True
    with store.connection() as db:
        assert "possible_secret" not in "\n".join(db.iterdump())


def test_connector_racing_acknowledgement_replay_keeps_warnings(store, monkeypatch):
    transport = Transport()
    service = connector(store, lambda record: [WARNING.copy()], transport)

    def acknowledged_before_claim(operation_id, request_digest):
        service._ledger.mark_acknowledged(
            operation_id, receipt={"object_id": "racing-object"}, upstream_object_id="racing-object"
        )
        return False

    monkeypatch.setattr(service._ledger, "claim_sending", acknowledged_before_claim)
    result = asyncio.run(
        service.write_note(
            policy=Policy(), project="alpha", source_ref="dots://memory/race@1", title="Race"
        )
    )
    assert result["warnings"] == [WARNING]
    assert result["replayed"] is True
    assert transport.calls == []


def test_mcp_remember_returns_warnings_and_preserves_content_on_replay(store, tmp_path):
    credential = tmp_path / "client.json"
    issue_client(
        store,
        name="warning-client",
        scopes=["memory:read", "memory:write"],
        projects=["alpha"],
        days=1,
        output=credential,
        url="http://127.0.0.1:8765/mcp",
    )
    server = create_server(MemoryService(store), http=True)
    app = BearerAuth(server.streamable_http_app(), store)

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
                        first = await session.call_tool("memory_remember", memory_arguments())
                        replay = await session.call_tool("memory_remember", memory_arguments())
                        update_arguments = memory_arguments() | {
                            "content": UNCERTAIN_CONTENT + ".",
                            "expected_revision": 1,
                        }
                        update = await session.call_tool("memory_remember", update_arguments)
                        old_replay = await session.call_tool("memory_remember", memory_arguments())
                        for response in (first, replay, update, old_replay):
                            assert not response.isError
                            assert_safe_warnings(response.structuredContent)
                        assert first.structuredContent["id"] == replay.structuredContent["id"]
                        assert replay.structuredContent["changed"] is False
                        assert replay.structuredContent["replayed_revision"] == 1
                        assert update.structuredContent["revision"] == 2
                        assert old_replay.structuredContent["revision"] == 2
                        assert old_replay.structuredContent["replayed_revision"] == 1
                        fetched = await session.call_tool(
                            "memory_get", {"memory_id": first.structuredContent["id"]}
                        )
                        assert not fetched.isError
                        assert fetched.structuredContent["content"] == UNCERTAIN_CONTENT + "."
                        assert fetched.structuredContent["revision"] == 2
                        original = await session.call_tool(
                            "memory_get",
                            {"memory_id": first.structuredContent["id"], "revision": 1},
                        )
                        assert not original.isError
                        assert original.structuredContent["content"] == UNCERTAIN_CONTENT
                        assert original.structuredContent["revision"] == 1

    asyncio.run(exercise())
    with store.connection() as db:
        rows = db.execute("SELECT content,digest FROM revisions ORDER BY revision").fetchall()
        assert len(rows) == 2
        for row, expected in zip(rows, (UNCERTAIN_CONTENT, UNCERTAIN_CONTENT + "."), strict=True):
            assert row["content"] == expected
            assert (
                row["digest"]
                == hashlib.sha256(
                    json.dumps([expected, "", None], ensure_ascii=False).encode()
                ).hexdigest()
            )
