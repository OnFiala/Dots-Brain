"""Public write ordering and claim ownership survive the W9 restructuring."""

import ast
import asyncio
import threading
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from dots_brain import cortex_connector
from dots_brain.cortex_connector import (
    CortexConnectionConfig,
    CortexConnector,
    CortexWriteUncertain,
    SqliteCortexLedger,
)
from dots_brain.errors import ConflictError
from dots_brain.store import Store


class Policy:
    projects = ("alpha",)
    principal = "oauth-grant:w9-synthetic"

    def __init__(self, events=None):
        self.events = events

    def require(self, scope):
        assert scope == "cortex:write"
        if self.events is not None:
            self.events.append(("authorize", threading.get_ident()))


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "brain")
    result.initialize()
    return result


def connector(store, transport, *, ledger=None, guard=lambda _: None):
    return CortexConnector(
        CortexConnectionConfig(
            "https://cortex.example.test/mcp",
            store.directory / "unused-synthetic-token",
            (("alpha", "mapped-alpha"),),
        ),
        ledger=ledger or SqliteCortexLedger(store),
        transport=transport,
        content_guard=guard,
    )


def arguments(**extra):
    return {
        "policy": Policy(),
        "project": "alpha",
        "source_ref": "dots://memory/w9-order@1",
        "operation_id": "cxo_w9-order",
        "title": "Synthetic order",
        "content": "Selected content",
        **extra,
    }


def test_write_orders_authorization_privacy_ledger_preflight_and_delivery_off_loop(store):
    events = []
    main_thread = threading.get_ident()

    class ObservedLedger(SqliteCortexLedger):
        def get(self, operation_id):
            events.append(("get", threading.get_ident()))
            return super().get(operation_id)

        def create_planned(self, record):
            events.append(("create", threading.get_ident()))
            return super().create_planned(record)

        def claim_sending(self, operation_id, request_digest):
            events.append(("claim", threading.get_ident()))
            return super().claim_sending(operation_id, request_digest)

        def mark_acknowledged(self, operation_id, **kwargs):
            events.append(("acknowledge", threading.get_ident()))
            return super().mark_acknowledged(operation_id, **kwargs)

    class Transport:
        async def prepare_write(self):
            events.append(("prepare", threading.get_ident()))

        async def call_tool(self, name, request):
            events.append(("send", threading.get_ident()))
            assert name == "cortex_record_note"
            assert request["content"].startswith("Selected content\n\n[Dots Brain origin]")
            return {"object_id": "w9-object", "event_id": "w9-event", "ignored": "upstream"}

    def guard(record):
        events.append(("privacy", threading.get_ident()))
        assert record["payload"]["content"] == "Selected content"

    result = asyncio.run(
        connector(store, Transport(), ledger=ObservedLedger(store), guard=guard).write_note(
            **arguments(policy=Policy(events))
        )
    )
    assert [name for name, _ in events] == [
        "authorize",
        "privacy",
        "get",
        "create",
        "prepare",
        "claim",
        "send",
        "acknowledge",
        "get",
    ]
    for name, thread in events:
        assert (thread == main_thread) is (name in {"authorize", "privacy", "prepare", "send"})
    assert result["receipt"] == {"object_id": "w9-object", "event_id": "w9-event"}
    assert result["state"] == "acknowledged" and result["replayed"] is False
    assert set(result) == {
        "operation_id",
        "request_digest",
        "state",
        "source_ref",
        "receipt",
        "retrievable_source_ref",
        "acknowledgement_only",
        "replayed",
    }


@pytest.mark.parametrize("winner", ["acknowledged", "sending", "different_digest"])
def test_claim_loser_observes_durable_winner_without_sending_again(store, winner):
    ledger = SqliteCortexLedger(store)
    calls = []
    warning = {"code": "possible_secret", "category": "assignment"}

    class Transport:
        async def prepare_write(self):
            row = await asyncio.to_thread(ledger.get, "cxo_w9-order")
            assert await asyncio.to_thread(
                ledger.claim_sending, row["operation_id"], row["request_digest"]
            )
            if winner != "sending":
                await asyncio.to_thread(
                    ledger.mark_acknowledged,
                    row["operation_id"],
                    receipt={"event_id": "winner-event"},
                    upstream_object_id=None,
                )
            if winner == "different_digest":
                with store.connection(write=True) as db:
                    db.execute(
                        "UPDATE cortex_operations SET request_digest='different' "
                        "WHERE operation_id=?",
                        (row["operation_id"],),
                    )

        async def call_tool(self, name, request):
            calls.append((name, request))
            raise AssertionError("The losing writer must never send.")

    service = connector(store, Transport(), ledger=ledger, guard=lambda _: [warning])
    if winner == "acknowledged":
        result = asyncio.run(service.write_note(**arguments()))
        assert result["state"] == "acknowledged" and result["replayed"] is True
        assert result["receipt"] == {"event_id": "winner-event"}
        assert result["warnings"] == [warning]
        assert result["acknowledgement_only"] is True
    else:
        expected = ConflictError if winner == "different_digest" else CortexWriteUncertain
        with pytest.raises(expected):
            asyncio.run(service.write_note(**arguments()))
    assert calls == []


def test_cancellation_during_preflight_leaves_retryable_plan_without_delivery(store):
    started = asyncio.Event()
    calls = []

    class Transport:
        async def prepare_write(self):
            started.set()
            await asyncio.Event().wait()

        async def call_tool(self, name, request):
            calls.append((name, request))

    async def run():
        task = asyncio.create_task(connector(store, Transport()).write_note(**arguments()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    row = SqliteCortexLedger(store).get("cxo_w9-order")
    assert row["state"] == "planned" and row["error_code"] is None
    assert calls == []


def test_cancellation_waits_for_shielded_uncertain_receipt_before_returning(store):
    sent = asyncio.Event()
    marking, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()

    class BlockingLedger(SqliteCortexLedger):
        def mark_uncertain(self, operation_id, *, error_code):
            assert threading.get_ident() != main_thread
            marking.set()
            assert release.wait(5)
            return super().mark_uncertain(operation_id, error_code=error_code)

    class Transport:
        async def call_tool(self, name, request):
            sent.set()
            await asyncio.Event().wait()

    async def run():
        task = asyncio.create_task(
            connector(store, Transport(), ledger=BlockingLedger(store)).write_note(**arguments())
        )
        await sent.wait()
        task.cancel()
        try:
            assert await asyncio.to_thread(marking.wait, 5)
            assert not task.done()
            assert SqliteCortexLedger(store).get("cxo_w9-order")["state"] == "sending"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    row = SqliteCortexLedger(store).get("cxo_w9-order")
    assert row["state"] == "uncertain" and row["error_code"] == "cancelled_after_send"


def test_write_entry_point_is_bounded_and_delegates_reviewable_responsibilities():
    tree = ast.parse(Path(cortex_connector.__file__).read_text())
    owner = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CortexConnector"
    )
    function = next(
        n for n in owner.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_write"
    )
    assert function.end_lineno - function.lineno + 1 <= 90
    assert not any(isinstance(n, ast.ExceptHandler) for n in ast.walk(function))
    assert (
        len(
            {
                n.func.attr
                for n in ast.walk(function)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "self"
            }
        )
        >= 3
    )


def test_public_write_receipt_type_declares_actual_fields_and_ledger_states():
    receipt_type = getattr(cortex_connector, "CortexOperationReceipt", None)
    assert receipt_type is not None
    fields = get_type_hints(receipt_type)
    assert receipt_type.__optional_keys__ == {"warnings"}
    assert set(get_args(fields["state"])) == {"planned", "sending", "acknowledged", "uncertain"}
    assert set(fields) == {
        "operation_id",
        "request_digest",
        "state",
        "source_ref",
        "receipt",
        "retrievable_source_ref",
        "acknowledgement_only",
        "replayed",
        "warnings",
    }
    for function in (
        CortexConnector.write_note,
        CortexConnector.write_decision,
        CortexConnector.write_outcome,
        cortex_connector._receipt_from_row,
    ):
        assert get_type_hints(function)["return"] is receipt_type
