import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from dots_brain import capture_delivery
from dots_brain.capture import MAX_BATCH_RECORDS, JSONLCollector, normalize_audit_record
from dots_brain.errors import InputError
from dots_brain.privacy import guard_content, sanitize


def _line(command: str) -> str:
    return json.dumps({"type": "shell_command", "command": command}) + "\n"


def test_copytruncate_records_acknowledged_gap_and_new_generation(tmp_path):
    source, cursor, events = tmp_path / "audit.jsonl", tmp_path / "run.cursor", []
    source.write_text(_line("old-one") + _line("old-two"))
    collector = JSONLCollector([source], cursor, lambda item: events.append(item) or True)
    assert collector.collect() == 2
    source.write_text(_line("new-one") + _line("new-two") + _line("new-three"))
    assert collector.collect() == 4
    assert events[2]["action"]["reason"] == "file_rotated_or_truncated"
    assert len({event["record_id"] for event in events}) == len(events)


def test_collector_requires_explicit_ack_and_uses_full_name_lock(tmp_path):
    source, cursor = tmp_path / "audit.jsonl", tmp_path / "x.lock"
    source.write_text(_line("one"))
    with pytest.raises(InputError, match="acknowledge"):
        JSONLCollector([source], cursor, lambda _: None).collect()
    assert not cursor.exists()
    events = []
    assert JSONLCollector([source], cursor, lambda item: events.append(item) or True).collect() == 1
    assert cursor.with_name("x.lock.lock").exists()


def test_regular_file_guard_and_batch_completion_state(tmp_path):
    with pytest.raises(InputError, match="regular file"):
        JSONLCollector(["/dev/zero"], tmp_path / "cursor", lambda _: True).collect()
    source, cursor = tmp_path / "audit.jsonl", tmp_path / "cursor"
    source.write_text("".join(_line(str(index)) for index in range(MAX_BATCH_RECORDS + 1)))
    collector = JSONLCollector([source], cursor, lambda _: True)
    assert collector.collect() == MAX_BATCH_RECORDS
    assert collector.completed is False


def test_untrusted_snapshot_metadata_is_sanitized_and_not_identity_proof():
    event = normalize_audit_record(
        {
            "type": "shell_command",
            "agentId": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
            "ts": "Cookie: session=SYNTHETIC_CREDENTIAL",
        }
    )
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in json.dumps(event)
    assert event["details"]["source_identity"] == "unverified_snapshot"


def test_sanitizer_bounds_keys_nodes_bytes_and_preserves_summary_labels():
    secret_key = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    initial = sanitize(
        {secret_key: "value", "items": ["x" * 200 for _ in range(500)]},
        max_nodes=30,
        max_output_bytes=512,
    )
    assert secret_key not in json.dumps(initial.value)
    assert initial.truncated
    summary = sanitize({"sanitization": initial.summary()}).value["sanitization"]
    assert "token" in summary["categories"]
    assert "[REDACTED]" in sanitize("https://x.test/?token=abc123").value
    with pytest.raises(InputError):
        guard_content({"content": "safe", "event_id": secret_key})


def test_terminal_delivery_becomes_acknowledged_gap_and_invalid_journal_needs_recovery(
    tmp_path, monkeypatch
):
    source, cursor, calls = tmp_path / "snapshot.jsonl", tmp_path / "cursor", []
    source.write_text(json.dumps({"role": "user", "message": "one"}) + "\n")

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if name == "memory_remember":
                return SimpleNamespace(
                    isError=True,
                    structuredContent=None,
                    content=[SimpleNamespace(text="source is suppressed")],
                )
            return SimpleNamespace(isError=False, structuredContent={"id": "ok"}, content=[])

    @asynccontextmanager
    async def connection(_credential):
        yield Session()

    monkeypatch.setattr(capture_delivery, "connect", connection)

    async def exercise():
        result = await capture_delivery.collect_remote(
            paths=[source],
            cursor=cursor,
            credential=tmp_path / "credential",
            project="alpha",
            account="bot",
            kind="transcript",
        )
        assert result["state"] == "capture_pass_complete"
        gaps = [
            args for name, args in calls if name == "audit_record" and args.get("kind") == "gap"
        ]
        assert gaps and gaps[0]["action"]["reason"] == "delivery_suppressed_source"

        pending = cursor.with_name(cursor.name + ".coverage.json")
        pending.write_text(json.dumps({"project": "beta", "kind": "coverage"}))
        blocked = await capture_delivery.collect_remote(
            paths=[source],
            cursor=cursor,
            credential=tmp_path / "credential",
            project="alpha",
            account="bot",
            kind="transcript",
        )
        assert blocked["state"] == "capture_recovery_required"
        recovered = await capture_delivery.collect_remote(
            paths=[source],
            cursor=cursor,
            credential=tmp_path / "credential",
            project="alpha",
            account="bot",
            kind="transcript",
            recover_pending=True,
        )
        assert recovered["recovered_pending_receipt"] is True

    asyncio.run(exercise())


def test_appending_small_source_does_not_rotate_or_replay_acknowledged_lines(tmp_path):
    source, cursor, events = tmp_path / "audit.jsonl", tmp_path / "cursor", []
    source.write_text(_line("first"))
    collector = JSONLCollector([source], cursor, lambda item: events.append(item) or True)
    assert collector.collect() == 1
    first_id = events[0]["record_id"]
    with source.open("a") as stream:
        stream.write("".join(_line(f"next-{i}") for i in range(40)))
    assert collector.collect() == 40
    assert len(events) == 41
    assert len({event["record_id"] for event in events}) == 41
    assert sum(event["record_id"] == first_id for event in events) == 1
    assert all(event.get("kind") != "gap" for event in events)
    assert collector.collect() == 0


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization", "x-api-key", "Cookie"])
def test_exact_credential_header_does_not_depend_on_value_entropy(header):
    text = f"{header}: alphabeticcredentialvalue"
    with pytest.raises(InputError):
        guard_content({"content": text})
    redacted = sanitize(text).value
    assert "alphabeticcredentialvalue" not in redacted
    assert sanitize(redacted).value == redacted
