import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from dots_brain.activity import AuditLog
from dots_brain.auth import Policy
from dots_brain.capture import JSONLCollector, normalize_transcript_record
from dots_brain.errors import ConflictError, ForbiddenError, InputError, NotFoundError
from dots_brain.privacy import guard_content, sanitize
from dots_brain.store import Store


def audit_store(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    return store


def policy(*scopes, projects=None):
    return Policy(frozenset(scopes), projects, "bot:one")


def test_secret_canaries_never_survive_sanitized_serialization():
    canary = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    result = sanitize(
        {"Authorization": f"Bearer {canary}", "url": f"https://x.test/?token={canary}", "x": canary}
    )
    rendered = json.dumps(result.value)
    assert canary not in rendered and result.redactions >= 3
    assert sanitize("sha256:abcdef0123456789").value == "sha256:abcdef0123456789"
    with pytest.raises(InputError, match="appears to contain a secret"):
        guard_content({"content": canary})


def test_audit_scopes_idempotence_project_boundaries_and_missing_receipts(tmp_path):
    log = AuditLog(audit_store(tmp_path))
    write = policy("audit:write", projects=("alpha",))
    with pytest.raises(NotFoundError):
        log.record(write, project="beta", kind="intent", client_event_id="x")
    first = log.record(
        write, project="alpha", kind="intent", client_event_id="i", action={"command": "echo ok"}
    )
    assert log.record(
        write, project="alpha", kind="intent", client_event_id="i", action={"command": "echo ok"}
    )["replayed"]
    with pytest.raises(ConflictError):
        log.record(
            write,
            project="alpha",
            kind="intent",
            client_event_id="i",
            action={"command": "changed"},
        )
    read = policy("audit:read", projects=("alpha",))
    report = log.report(read, project="alpha")
    assert report["coverage"]["missing_receipts"] == 1 and first["id"] == 1
    with pytest.raises(ForbiddenError):
        log.events(write, project="alpha")


def test_audit_chain_rejects_tampering_and_receipt_requires_actor_intent(tmp_path):
    store = audit_store(tmp_path)
    log = AuditLog(store)
    writer = policy("audit:write")
    with pytest.raises(InputError):
        log.record(writer, project="a", kind="receipt", client_event_id="r", intent_event_id="none")
    log.record(writer, project="a", kind="intent", client_event_id="i")
    with store.connection(write=True) as db:
        with pytest.raises(Exception, match="append-only"):
            db.execute("UPDATE audit_events SET kind='gap'")


def test_collector_waits_for_ack_partial_line_and_rotation(tmp_path):
    export, cursor, delivered = tmp_path / "audit.jsonl", tmp_path / "cursor.json", []
    export.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "type": "shell_command", "command": "echo hi"})
        + "\n"
        + "{"
    )
    collector = JSONLCollector([export], cursor, lambda item: delivered.append(item) or True)
    assert collector.collect() == 1 and len(delivered) == 1
    assert collector.collect() == 0
    export.write_text(json.dumps({"type": "shell_command", "command": "x"}) + "\n")
    assert collector.collect() == 2 and delivered[-1]["kind"] == "action"
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in json.dumps(delivered[-1])


def test_transcript_excludes_system_and_marks_unknown_shape():
    system = normalize_transcript_record({"role": "system", "message": "secret"})
    unknown = normalize_transcript_record({"role": "assistant", "message": {"reasoning": "hidden"}})
    visible = normalize_transcript_record(
        {"role": "user", "message": {"content": [{"type": "text", "text": "hello"}]}}
    )
    assert system["kind"] == unknown["kind"] == "gap"
    assert visible["details"]["content"] == "hello"


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Basic SYNTHETIC_CREDENTIAL_VALUE",
        "Cookie: session=SYNTHETIC_CREDENTIAL_VALUE",
        "curl -H 'X-Api-Key: SYNTHETIC_CREDENTIAL_VALUE' https://example.test",
        "https://user:SYNTHETIC_CREDENTIAL_VALUE@example.test/path",
    ],
)
def test_raw_header_and_url_credentials_never_survive(text):
    assert "SYNTHETIC_CREDENTIAL_VALUE" not in sanitize(text).value
    with pytest.raises(InputError) as error:
        guard_content({"content": text})
    assert "SYNTHETIC_CREDENTIAL_VALUE" not in str(error.value)


def test_strict_guard_preserves_long_valid_content_and_rejects_truncation():
    value = {"content": "A valid conversation. " * 1000}
    assert guard_content(value) == value
    with pytest.raises(InputError, match="size or structure"):
        guard_content({"content": "x" * 32001})


def test_rotation_checkpoint_survives_without_a_complete_next_line(tmp_path):
    export, cursor, delivered = tmp_path / "audit.jsonl", tmp_path / "cursor.json", []
    export.write_text('{"type":"shell_command","command":"echo hello"}\n')
    collector = JSONLCollector([export], cursor, lambda item: delivered.append(item) or True)
    assert collector.collect() == 1
    export.write_text("{")
    assert collector.collect() == 1
    assert delivered[-1]["kind"] == "gap"
    assert collector.collect() == 0
    assert len(delivered) == 2


def test_collector_overlap_serializes_delivery_and_checkpoints(tmp_path):
    export, cursor, delivered = tmp_path / "audit.jsonl", tmp_path / "cursor.json", []
    export.write_text('{"type":"shell_command","command":"echo hello"}\n')

    def run(_):
        return JSONLCollector(
            [export], cursor, lambda event: delivered.append(event) or True
        ).collect()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == [0, 1]
    assert len(delivered) == 1


def test_collector_missing_source_and_oversized_lines_are_acknowledged_gaps(tmp_path):
    from dots_brain.capture import MAX_LINE_BYTES

    export, cursor, delivered = tmp_path / "audit.jsonl", tmp_path / "cursor.json", []
    collector = JSONLCollector([export], cursor, lambda event: delivered.append(event) or True)
    assert collector.collect() == 1
    assert collector.collect() == 0
    assert delivered[0]["action"]["reason"] == "source_unavailable"
    export.write_text("x" * (MAX_LINE_BYTES * 2) + '\n{"type":"shell_command"}\n')
    collector.collect()
    assert sum(event["action"].get("reason") == "oversized_line" for event in delivered) == 1
    assert delivered[-1]["kind"] == "action"
    assert collector.collect() == 0


def test_report_cannot_hide_unmatched_actor_or_unscanned_tail(tmp_path):
    log = AuditLog(audit_store(tmp_path))
    first = Policy(frozenset({"audit:write", "audit:read"}), ("alpha",), "bot:one")
    second = Policy(first.scopes, first.projects, "bot:two")
    for caller in (first, second):
        log.record(caller, project="alpha", kind="intent", client_event_id="same")
    log.record(second, project="alpha", kind="receipt", client_event_id="r", intent_event_id="same")
    assert log.report(first, project="alpha")["coverage"]["missing_receipts"] == 1
    assert log.report(first, project="alpha", limit=2)["window"]["has_more"]
    assert log.report(first, project="alpha", limit=2)["status"] == "partial"
    with pytest.raises(InputError, match="timezone"):
        log.record(
            first,
            project="alpha",
            kind="action",
            client_event_id="bad-time",
            occurred_at="2026-01-01T00:00:00",
        )


def test_audit_redacts_persisted_payloads_and_filters_unqualified_reads(tmp_path):
    store = audit_store(tmp_path)
    log = AuditLog(store)
    caller = policy("audit:write", "audit:read", projects=("alpha",))
    log.record(
        caller,
        project="alpha",
        kind="action",
        client_event_id="one",
        action="Authorization: Basic SYNTHETIC_CREDENTIAL",
    )
    log.record(policy("audit:write"), project="beta", kind="action", client_event_id="private")
    rows = log.events(caller)
    assert len(rows) == 1 and "SYNTHETIC_CREDENTIAL" not in json.dumps(rows)
    with store.connection() as db:
        assert (
            "SYNTHETIC_CREDENTIAL"
            not in db.execute("SELECT action FROM audit_events WHERE project='alpha'").fetchone()[0]
        )


@pytest.mark.parametrize(
    "value",
    [
        "DB_PASSWORD=safe-looking-value",
        '"password": "x"',
        "postgres://admin:synthetic-password@db.example/test",
        "xoxb-1234567890abcdef",
        "sk_live_1234567890abcdef",
        "AIza1234567890abcdefghijkl",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "curl -u admin:synthetic-password",
    ],
)
def test_memory_guard_rejects_common_credential_forms(value):
    with pytest.raises(InputError):
        guard_content({"content": value})


@pytest.mark.parametrize(
    "value",
    [
        "Token: budget for this sprint is 40 points.",
        "Secret: Santa draw happens on Friday.",
        "The flag bearer unfortunately tripped on stage.",
        "Clone with https://andrew@dev.azure.com/org/repo",
        "def login(user, password=None, token=None):",
    ],
)
def test_memory_guard_keeps_noncredential_prose(value):
    assert guard_content({"content": value})["content"] == value


def test_snapshot_excludes_ambiguous_assistant_text_and_tool_payloads():
    canary = "PRIVATE_CONTENT_CANARY"
    inputs = [
        {"role": "assistant", "message": {"content": [{"type": "text", "text": canary}]}},
        {
            "role": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "example", "input": {"payload": canary}}]
            },
        },
        {
            "role": "tool",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "name": "example",
                        "result": {"success": True, "text": canary},
                    }
                ]
            },
        },
        {"role": "tool", "message": {"unexpected": "shape"}},
    ]
    results = [normalize_transcript_record(item) for item in inputs]
    assert canary not in json.dumps(results)
    assert [item["kind"] for item in results] == ["gap", "action", "action", "gap"]
    assert results[2]["action"][0]["success"] is True
