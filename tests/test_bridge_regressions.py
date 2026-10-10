"""Regression coverage for bridge lifetime and explicit verification probes."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager, suppress

import httpx
import pytest

from dots_brain.bridge import BridgeFailure, BridgeSession, verify_connection, verify_session


class Result:
    def __init__(self, *, error=False, content=None):
        self.isError = error
        self.structuredContent = content


class Tools:
    def __init__(self, *names):
        self.tools = [type("Tool", (), {"name": name})() for name in names]


def test_audit_only_verification_does_not_require_memory_status():
    class AuditOnly:
        async def list_tools(self):
            return Tools("audit_report")

        async def call_tool(self, name, arguments):
            assert name == "audit_report"
            assert arguments == {}
            return Result(content={"events": []})

    result = asyncio.run(verify_session(AuditOnly()))
    assert result["state"] == "verified_read"
    assert result["read"] is True
    assert result["write"] == "not_tested"


def test_failed_probe_validation_uses_fresh_session_for_cleanup(tmp_path):
    first_calls, cleanup_calls = [], []

    class First:
        async def list_tools(self):
            return Tools("memory_status", "memory_remember", "memory_get", "memory_forget")

        async def call_tool(self, name, arguments):
            first_calls.append(name)
            if name == "memory_status":
                return Result(content={})
            if name == "memory_remember":
                return Result(content={"id": "probe-id", "revision": 1})
            if name == "memory_get":
                return Result(error=True)
            pytest.fail("Cleanup must not run on a session whose validation failed")

    class Fresh:
        async def call_tool(self, name, arguments):
            cleanup_calls.append((name, arguments))
            return Result(content={"deleted": True})

    @asynccontextmanager
    async def fresh_session():
        yield Fresh()

    receipt = tmp_path / "probe.json"
    result = asyncio.run(
        verify_session(
            First(),
            write=True,
            project="granted-project",
            probe_event_id="connection-probe-stable",
            probe_receipt_path=receipt,
            fresh_session=fresh_session,
        )
    )
    assert result["state"] == "verification_failed"
    assert result["probe_removed"] is True
    assert first_calls == ["memory_status", "memory_remember", "memory_get"]
    assert cleanup_calls == [("memory_forget", {"memory_id": "probe-id", "expected_revision": 1})]
    assert not receipt.exists()


def test_unknown_probe_write_is_recorded_and_not_replayed(tmp_path, monkeypatch):
    credential = tmp_path / "client.json"
    token = "private-test-token"
    credential.write_text(
        json.dumps(
            {"version": 1, "client_id": "client", "url": "http://127.0.0.1/mcp", "token": token}
        )
    )
    calls = []

    class Session:
        async def list_tools(self):
            return Tools("memory_status", "memory_remember", "memory_get", "memory_forget")

        async def call_tool(self, name, arguments):
            calls.append(name)
            if name == "memory_status":
                return Result(content={})
            if name == "memory_remember":
                raise httpx.ConnectError("transport lost after send")
            pytest.fail("Unknown writes must not trigger automatic recovery calls")

    @asynccontextmanager
    async def connection(_):
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)
    result = asyncio.run(verify_connection(credential, write=True, project="granted-project"))
    receipt = credential.with_name(credential.name + ".probe-recovery.json")
    pending = json.loads(receipt.read_text())
    assert result["state"] == "verification_failed"
    assert result["write"] == "unknown"
    assert result["probe_removed"] == "unknown"
    assert calls == ["memory_status", "memory_remember"]
    assert pending["state"] == "sending"
    expected_event_id = "connection-probe-" + hashlib.sha256(token.encode()).hexdigest()[:32]
    assert pending["event_id"] == expected_event_id
    assert pending["project"] == "granted-project"


def test_bridge_session_reuses_one_connection_for_concurrent_requests(monkeypatch):
    opens, calls = [], []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append(arguments["index"])
            await asyncio.sleep(0)
            return arguments["index"]

    @asynccontextmanager
    async def connection(_):
        opens.append(1)
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)

    async def exercise():
        remote = BridgeSession(None)
        worker = asyncio.create_task(remote.run())
        try:
            results = await asyncio.gather(
                *(remote.request("read", {"index": index}) for index in range(24))
            )
            assert results == list(range(24))
            assert calls == list(range(24))
            assert len(opens) == 1
        finally:
            await remote.close()
            await worker

    asyncio.run(exercise())


def test_bridge_session_queue_is_bounded_and_cancelled_callers_do_not_leak():
    async def exercise():
        remote = BridgeSession(None)
        callers = [
            asyncio.create_task(remote.request("read", {"index": index})) for index in range(48)
        ]
        await asyncio.sleep(0)
        assert remote.queue.maxsize == 32
        assert remote.queue.qsize() == 32
        for caller in callers:
            caller.cancel()
        await asyncio.gather(*callers, return_exceptions=True)
        await remote.close()
        worker = asyncio.create_task(remote.run())
        await worker
        assert remote.queue.empty()

    asyncio.run(exercise())


def test_bridge_session_shutdown_cancels_waiting_requests(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    class Session:
        async def call_tool(self, name, arguments):
            if name == "block":
                entered.set()
                await release.wait()
            return name

    @asynccontextmanager
    async def connection(_):
        yield Session()

    monkeypatch.setattr("dots_brain.bridge.connect", connection)

    async def exercise():
        remote = BridgeSession(None)
        worker = asyncio.create_task(remote.run())
        current = asyncio.create_task(remote.request("block"))
        await entered.wait()
        waiting = [
            asyncio.create_task(remote.request("read", {"index": index})) for index in range(6)
        ]
        await asyncio.sleep(0)
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        outcomes = await asyncio.wait_for(
            asyncio.gather(current, *waiting, return_exceptions=True), 1
        )
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        assert remote.queue.empty()

    asyncio.run(exercise())


def test_bridge_session_does_not_replay_failed_mutation(monkeypatch):
    opens, calls = [], []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append(name)
            if name == "write":
                raise httpx.ConnectError("lost after sending")
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
            with pytest.raises(BridgeFailure):
                await remote.request("write", {"id": "one"})
            assert calls == ["write"]
            assert await remote.request("read") == "read"
            assert calls == ["write", "read"]
            assert len(opens) == 2
        finally:
            await remote.close()
            await worker

    asyncio.run(exercise())


def test_shutdown_above_twice_queue_capacity_settles_every_caller():
    async def exercise():
        remote = BridgeSession(None)
        callers = [asyncio.create_task(remote.request("read")) for _ in range(100)]
        await asyncio.sleep(0)
        await remote.close()
        await asyncio.wait_for(remote.run(), 1)
        results = await asyncio.wait_for(asyncio.gather(*callers, return_exceptions=True), 1)
        assert len(results) == 100
        assert all(isinstance(result, BaseException) for result in results)
        assert remote.queue.empty()
        assert all(task.done() for task in callers)

    asyncio.run(exercise())
