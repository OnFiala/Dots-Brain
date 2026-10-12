"""Preserved snapshot collector characterizations, outside the core distribution."""

import json
from concurrent.futures import ThreadPoolExecutor

from dots_brain_capture.collector import JSONLCollector, normalize_transcript_record


def test_collector_waits_for_ack_partial_line_and_rotation(tmp_path):
    export, cursor, delivered = tmp_path / "audit.jsonl", tmp_path / "cursor.json", []
    export.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "type": "shell_command", "command": "echo hi"})
        + "\n"
        + "{"
    )
    collector = JSONLCollector([export], cursor, lambda item: delivered.append(item))
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


def test_rotation_checkpoint_survives_without_a_complete_next_line(tmp_path):
    export, cursor, delivered = tmp_path / "audit.jsonl", tmp_path / "cursor.json", []
    export.write_text('{"type":"shell_command","command":"echo hello"}\n')
    collector = JSONLCollector([export], cursor, lambda item: delivered.append(item))
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
        return JSONLCollector([export], cursor, lambda event: delivered.append(event)).collect()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == [0, 1]
    assert len(delivered) == 1


def test_collector_missing_source_and_oversized_lines_are_acknowledged_gaps(tmp_path):
    from dots_brain_capture.collector import MAX_LINE_BYTES

    export, cursor, delivered = tmp_path / "audit.jsonl", tmp_path / "cursor.json", []
    collector = JSONLCollector([export], cursor, lambda event: delivered.append(event))
    assert collector.collect() == 1
    assert collector.collect() == 0
    assert delivered[0]["action"]["reason"] == "source_unavailable"
    export.write_text("x" * (MAX_LINE_BYTES * 2) + '\n{"type":"shell_command"}\n')
    collector.collect()
    assert sum(event["action"].get("reason") == "oversized_line" for event in delivered) == 1
    assert delivered[-1]["kind"] == "action"
    assert collector.collect() == 0


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
