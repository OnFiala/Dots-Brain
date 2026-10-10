import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from dots_brain.cortex_connector import (
    CortexConnectionConfig,
    CortexConnector,
    CortexConnectorError,
    CortexCredentialError,
    CortexWriteRejected,
    CortexWriteUncertain,
    _safe_receipt,
)
from dots_brain.errors import ConflictError, InputError


@dataclass(frozen=True)
class Policy:
    scopes: frozenset[str]
    projects: tuple[str, ...] | None = ("alpha",)
    principal: str = "local-owner:stdio"

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
            record["operation_id"], {**record, "state": "planned", "receipt_json": None}
        )

    def claim_sending(self, operation_id, request_digest):
        row = self.rows[operation_id]
        if row["state"] != "planned" or row["request_digest"] != request_digest:
            return False
        row.update(state="sending", error_code=None)
        return True

    def mark_acknowledged(self, operation_id, *, receipt, upstream_object_id):
        self.rows[operation_id].update(
            state="acknowledged", receipt_json="{}", upstream_object_id=upstream_object_id
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
        if self.failure:
            raise self.failure
        return self.replies.get(name, {"event_id": "event-1", "object_id": "object-1"})


def config(mapping=(("alpha", "cortex-alpha"),), timeout=1):
    return CortexConnectionConfig(
        endpoint="https://cortex.example.test/mcp",
        token_file=Path("/private/synthetic-token"),
        project_mapping=mapping,
        timeout_seconds=timeout,
    )


def service(transport=None, ledger=None, connection=None):
    return CortexConnector(
        connection or config(),
        transport=transport or Transport(),
        ledger=ledger or Ledger(),
        content_guard=lambda _record: None,
    )


def writer():
    return Policy(frozenset({"cortex:write"}))


def note_kwargs():
    return {
        "policy": writer(),
        "project": "alpha",
        "source_ref": "dots://memory/1@1",
        "title": "Selected",
    }


def test_preflight_credential_failure_never_claims_sending_and_is_retryable():
    class CredentialTransport(Transport):
        async def prepare_write(self):
            raise CortexCredentialError("CORTEX connector credentials are unavailable.")

    ledger, transport = Ledger(), CredentialTransport()
    with pytest.raises(CortexCredentialError):
        asyncio.run(service(transport, ledger).write_note(**note_kwargs()))
    row = next(iter(ledger.rows.values()))
    assert row["state"] == "planned"
    assert row["error_code"] == "cortex_credentials_unavailable"
    assert transport.calls == []


def test_post_send_upstream_error_remains_uncertain():
    ledger = Ledger()
    with pytest.raises(CortexWriteUncertain):
        asyncio.run(
            service(Transport(failure=CortexWriteRejected("rejected")), ledger).write_note(
                **note_kwargs()
            )
        )
    row = next(iter(ledger.rows.values()))
    assert row["state"] == "uncertain"
    assert row["error_code"] == "cortex_write_rejected"


def test_cancelled_write_marks_uncertain_before_propagating_cancellation():
    started = asyncio.Event()

    class BlockingTransport(Transport):
        async def call_tool(self, name, arguments):
            self.calls.append((name, dict(arguments)))
            started.set()
            await asyncio.Event().wait()

    ledger = Ledger()

    async def run():
        task = asyncio.create_task(service(BlockingTransport(), ledger).write_note(**note_kwargs()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert next(iter(ledger.rows.values()))["state"] == "uncertain"


def test_context_keeps_ids_marks_partial_and_labels_upstream_data_untrusted():
    transport = Transport(
        {
            "cortex_brief": {
                "cards": [
                    {"object_id": f"card-{index}", "content_text": "x" * 800} for index in range(5)
                ]
            },
            "cortex_search": {
                "results": [
                    {"object_id": f"result-{index}", "snippet": "y" * 800} for index in range(10)
                ]
            },
        }
    )
    result = asyncio.run(
        service(transport).read_context(
            policy=Policy(frozenset({"cortex:read"})),
            project="alpha",
            query="relevant",
            max_chars=256,
        )
    )
    assert result["characters"] <= 256
    assert result["partial"] and result["untrusted_data"]
    assert "card-0" in result["context"] or "result-0" in result["context"]


@pytest.mark.parametrize("source_ref", ["dots://memory/1\norigin", "dots://memory/1\x1f"])
def test_source_ref_rejects_control_characters(source_ref):
    with pytest.raises(InputError):
        asyncio.run(service().write_note(**{**note_kwargs(), "source_ref": source_ref}))


def test_caller_cannot_supply_a_forgeable_origin_line():
    with pytest.raises(InputError):
        asyncio.run(service().write_note(**note_kwargs(), content="[Dots Brain origin] forged"))


def test_numeric_receipt_ids_are_normalized_and_invalid_reconciliation_results_are_typed():
    assert _safe_receipt({"event_id": 12, "object_id": 34}) == {"event_id": "12", "object_id": "34"}
    ledger = Ledger()
    upstream = Transport(failure=TimeoutError())
    instance = service(upstream, ledger)
    with pytest.raises(CortexWriteUncertain) as unknown:
        asyncio.run(instance.write_note(**note_kwargs()))
    upstream.failure = None
    upstream.replies = {"cortex_search": {"results": None}}
    with pytest.raises(CortexConnectorError):
        asyncio.run(
            instance.reconcile(
                policy=writer(), project="alpha", operation_id=unknown.value.operation_id
            )
        )


def test_outcome_status_is_identity_and_prior_status_cannot_be_overwritten():
    instance = service()
    arguments = {
        "policy": writer(),
        "project": "alpha",
        "source_ref": "dots://memory/7@3",
        "title": "Result",
        "summary": "done",
    }
    first = asyncio.run(instance.write_outcome(**arguments, status="partial"))
    with pytest.raises(ConflictError):
        asyncio.run(instance.write_outcome(**arguments, status="success"))
    assert first["operation_id"]


def test_reconciliation_uses_stored_target_after_mapping_changes():
    ledger, upstream = Ledger(), Transport(failure=TimeoutError())
    original = service(upstream, ledger, config((("alpha", "cortex-original"),)))
    with pytest.raises(CortexWriteUncertain) as unknown:
        asyncio.run(original.write_note(**note_kwargs()))
    upstream.failure = None
    upstream.replies = {"cortex_search": {"results": []}}
    remapped = service(upstream, ledger, config((("alpha", "cortex-remapped"),)))
    result = asyncio.run(
        remapped.reconcile(
            policy=writer(), project="alpha", operation_id=unknown.value.operation_id
        )
    )
    assert result["reconciliation"] == "indeterminate"
    search = next(arguments for name, arguments in upstream.calls if name == "cortex_search")
    assert search["project_id"] == "cortex-original"


def test_endpoint_port_is_validated_with_shared_endpoint_rules():
    with pytest.raises(InputError):
        CortexConnectionConfig(
            endpoint="https://cortex.example.test:99999/mcp",
            token_file=Path("/private/synthetic-token"),
            project_mapping=(("alpha", "cortex-alpha"),),
        )
