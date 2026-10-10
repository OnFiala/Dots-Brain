import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from dots_brain import capture_delivery, privacy
from dots_brain.capture import MAX_BATCH_RECORDS, JSONLCollector, normalize_audit_record
from dots_brain.errors import InputError
from dots_brain.privacy import guard_content, sanitize


def _line(command: str) -> str:
    return json.dumps({"type": "shell_command", "command": command}) + "\n"


def test_copytruncate_records_gap_and_blocks_the_current_cursor(tmp_path):
    source, cursor, events = tmp_path / "audit.jsonl", tmp_path / "run.cursor", []
    source.write_text(_line("old-one") + _line("old-two"))
    collector = JSONLCollector([source], cursor, lambda item: events.append(item) or True)
    assert collector.collect() == 2
    source.write_text(_line("new-one") + _line("new-two") + _line("new-three"))
    assert collector.collect() == 1
    assert events[-1]["action"]["reason"] == "source_rewritten_requires_reset"
    assert collector.collect() == 0
    state = json.loads(cursor.read_text())["files"][str(source)]
    assert state["offset"] == len(_line("old-one") + _line("old-two"))
    assert state["source_reset_required"] is True


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


def test_rename_preserves_forgotten_record_identity_and_imports_only_the_append(tmp_path):
    source, cursor = tmp_path / "audit.jsonl", tmp_path / "cursor"
    source.write_text(_line("forgotten") + _line("kept"))
    delivered, suppressed = [], set()

    def sink(event):
        assert event["record_id"] not in suppressed, "forgotten source record was delivered again"
        delivered.append(event)
        return True

    collector = JSONLCollector([source], cursor, sink)
    assert collector.collect() == 2
    suppressed.add(delivered[0]["record_id"])
    original = source.read_bytes()

    # Export tools commonly replace an unchanged snapshot through rename.
    replacement = source.with_suffix(".replacement")
    replacement.write_bytes(original)
    replacement.replace(source)
    assert collector.collect() == 0

    # A later replacement may carry the same consumed prefix plus a new line.
    replacement.write_bytes(original + _line("new-after-forget").encode())
    replacement.replace(source)
    assert collector.collect() == 1
    assert [event["action"].get("reason") for event in delivered if event["kind"] == "gap"] == []
    assert [event["action"].get("command") for event in delivered] == [
        "forgotten",
        "kept",
        "new-after-forget",
    ]


def test_rename_with_a_middle_prefix_rewrite_blocks_without_importing_the_append(tmp_path):
    source, cursor, events = tmp_path / "audit.jsonl", tmp_path / "cursor", []
    lines = [_line(f"row-{index:03d}") for index in range(120)]
    source.write_text("".join(lines))
    collector = JSONLCollector([source], cursor, lambda event: events.append(event) or True)
    assert collector.collect() == 120
    acknowledged = source.read_bytes()
    lines[60] = _line("new-060")  # Same length; the old 256-byte samples still match.
    replacement = source.with_suffix(".replacement")
    replacement.write_text("".join(lines) + _line("append-after-rewrite"))
    replacement.replace(source)
    assert collector.collect() == 1
    assert events[-1]["action"]["reason"] == "source_rewritten_requires_reset"
    assert len(events) == 121
    state = json.loads(cursor.read_text())["files"][str(source)]
    assert state["offset"] == len(acknowledged)
    assert state["source_reset_required"] is True


def test_rewritten_rename_records_one_gap_and_blocks_reimport(tmp_path):
    source, cursor, events = tmp_path / "audit.jsonl", tmp_path / "cursor", []
    source.write_text(_line("original"))
    collector = JSONLCollector([source], cursor, lambda event: events.append(event) or True)
    assert collector.collect() == 1
    replacement = source.with_suffix(".replacement")
    replacement.write_text(_line("rewritten-one"))
    replacement.replace(source)
    assert collector.collect() == 1
    replacement.write_text(_line("rewritten-two"))
    replacement.replace(source)
    assert collector.collect() == 0
    gaps = [event for event in events if event["kind"] == "gap"]
    assert [event["action"]["reason"] for event in gaps] == ["source_rewritten_requires_reset"]


def test_initially_missing_source_is_not_reported_as_a_rotation_when_created(tmp_path):
    source, cursor, events = tmp_path / "audit.jsonl", tmp_path / "cursor", []
    collector = JSONLCollector([source], cursor, lambda event: events.append(event) or True)
    assert collector.collect() == 1
    source.write_text(_line("available"))
    assert collector.collect() == 1
    assert [event["action"].get("reason") for event in events if event["kind"] == "gap"] == [
        "source_unavailable"
    ]


def test_pending_retry_after_rename_does_not_redeliver_a_suppressed_source(tmp_path, monkeypatch):
    source, cursor = tmp_path / "snapshot.jsonl", tmp_path / "cursor"
    source.write_text(json.dumps({"role": "user", "message": "forgotten"}) + "\n")
    calls, failed_receipt = [], True

    class Session:
        async def call_tool(self, name, arguments):
            nonlocal failed_receipt
            calls.append((name, arguments))
            if name == "memory_remember":
                return SimpleNamespace(
                    isError=True,
                    structuredContent={"error": {"code": "source_suppressed"}},
                    content=[],
                )
            if (
                name == "audit_record"
                and arguments.get("action", {}).get("type") == "collector_pass"
                and failed_receipt
            ):
                failed_receipt = False
                return SimpleNamespace(
                    isError=True,
                    structuredContent={"error": {"code": "busy"}},
                    content=[],
                )
            return SimpleNamespace(isError=False, structuredContent={"id": "ok"}, content=[])

    @asynccontextmanager
    async def connection(_credential):
        yield Session()

    monkeypatch.setattr(capture_delivery, "connect", connection)

    async def exercise():
        first = await capture_delivery.collect_remote(
            paths=[source],
            cursor=cursor,
            credential=tmp_path / "credential",
            project="alpha",
            account="bot",
            kind="transcript",
        )
        assert first["state"] == "capture_partial"
        assert first["coverage_receipt"] == "pending_retry"
        replacement = source.with_suffix(".replacement")
        replacement.write_bytes(source.read_bytes() + b'{"role":"user","message":"later"}\n')
        replacement.replace(source)
        retry = await capture_delivery.collect_remote(
            paths=[source],
            cursor=cursor,
            credential=tmp_path / "credential",
            project="alpha",
            account="bot",
            kind="transcript",
        )
        assert retry["state"] == "capture_pass_complete"

    asyncio.run(exercise())
    remembered = [args["content"] for name, args in calls if name == "memory_remember"]
    assert remembered == ["forgotten", "later"]
    omitted = [args for name, args in calls if name == "audit_record" and args.get("kind") == "gap"]
    assert len(omitted) == 2
    assert {item["action"]["reason"] for item in omitted} == {"delivery_suppressed_source"}


def test_audit_delivery_keeps_snapshot_actor_as_unverified_metadata(tmp_path, monkeypatch):
    source, cursor, calls = tmp_path / "audit.jsonl", tmp_path / "cursor", []
    source.write_text(
        json.dumps({"type": "shell_command", "command": "pwd", "agentId": "agent-7"}) + "\n"
    )

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
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
            kind="audit",
        )
        assert result["state"] == "capture_pass_complete"

    asyncio.run(exercise())
    captured = next(
        args for name, args in calls if name == "audit_record" and args["kind"] == "action"
    )
    assert captured["details"]["source_actor"] == "agent-7"
    assert captured["details"]["event"]["source_identity"] == "unverified_snapshot"


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization", "x-api-key", "Cookie"])
def test_exact_credential_header_does_not_depend_on_value_entropy(header):
    text = f"{header}: alphabeticcredentialvalue"
    with pytest.raises(InputError):
        guard_content({"content": text})
    redacted = sanitize(text).value
    assert "alphabeticcredentialvalue" not in redacted
    assert sanitize(redacted).value == redacted


def test_sanitizer_bounds_text_before_redaction_and_drops_split_secret_fragment(monkeypatch):
    observed = []
    original = privacy._redact_text

    def bounded_redactor(value):
        observed.append(len(value))
        return original(value)

    monkeypatch.setattr(privacy, "_redact_text", bounded_redactor)
    token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    result = sanitize("harmless " + token + "x" * 2_000_000, max_text=24)
    assert max(observed) <= 24
    assert token not in result.value
    assert result.truncated["text_chars"] == len(token + "x" * 2_000_000)
    assert result.truncated["boundary_fragment"] == 1
    repeated = sanitize(result.value, max_text=24)
    assert repeated.value == result.value
    assert repeated.truncated == {}


@pytest.mark.parametrize("limit", [8, 12, 24, 32])
def test_sanitizer_drops_an_initial_split_token_and_preserves_the_marker(limit):
    result = sanitize("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" * 20, max_text=limit)
    assert result.value == "[TRUNCATED]"[:limit]
    assert len(result.value) <= limit
    repeated = sanitize(result.value, max_text=limit)
    assert repeated.value == result.value
    assert repeated.truncated == {}


def test_sanitizer_never_inspects_unbounded_keys_or_their_values(monkeypatch):
    redactor, sensitive_name = privacy._redact_text, privacy._is_sensitive_name
    observed = []

    def bounded_redactor(value):
        observed.append(len(value))
        assert len(value) <= 512
        return redactor(value)

    def bounded_sensitive_name(value):
        assert len(value) <= 512
        return sensitive_name(value)

    monkeypatch.setattr(privacy, "_redact_text", bounded_redactor)
    monkeypatch.setattr(privacy, "_is_sensitive_name", bounded_sensitive_name)
    key = "x" * 2_000_000 + "_password"
    result = sanitize({key: "synthetic-hidden-value"})
    assert result.value == {"[TRUNCATED_KEY]": "[REDACTED]"}
    assert result.truncated["key_chars"] == len(key)
    assert observed
    assert sanitize(result.value).value == result.value
