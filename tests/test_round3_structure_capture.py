"""Neutral behavior and responsibility boundaries for capture/privacy restructuring."""

import ast
import asyncio
import copy
import errno
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from dots_brain import capture, capture_delivery, privacy
from dots_brain.capture import JSONLCollector
from dots_brain.errors import InputError


def _line(label):
    return json.dumps({"type": "shell_command", "command": label}) + "\n"


def test_rewrite_barrier_commits_only_after_gap_acknowledgement(tmp_path):
    source, cursor = tmp_path / "source.jsonl", tmp_path / "cursor.json"
    source.write_text(_line("ceramic-a") + _line("ceramic-b"))
    events, acknowledge = [], True

    def sink(event):
        events.append(event)
        return acknowledge

    collector = JSONLCollector([source], cursor, sink)
    assert collector.collect() == 2
    before = cursor.read_bytes()
    source.write_text(_line("ceramic-c") + _line("ceramic-d") + _line("ceramic-e"))
    acknowledge = False
    with pytest.raises(InputError, match="acknowledge"):
        collector.collect()
    assert cursor.read_bytes() == before
    refused = copy.deepcopy(events[-1])
    acknowledge = True
    assert collector.collect() == 1
    assert events[-1] == refused
    assert refused["action"] == {"reason": "source_rewritten_requires_reset"}
    barrier = json.loads(cursor.read_text())["files"][str(source)]
    assert barrier == {
        **json.loads(before)["files"][str(source)],
        "source_reset_required": True,
    }
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(source.read_bytes() + _line("ceramic-f").encode())
    replacement.replace(source)
    delivered = len(events)
    assert collector.collect() == 0
    assert collector.completed is False
    assert len(events) == delivered
    assert json.loads(cursor.read_text())["files"][str(source)] == barrier


def test_rejected_rewrite_gap_closes_owned_source_descriptor(tmp_path, monkeypatch):
    source, cursor = tmp_path / "source.jsonl", tmp_path / "cursor.json"
    source.write_text(_line("ceramic-a"))
    assert JSONLCollector([source], cursor, lambda _: True).collect() == 1
    before = cursor.read_bytes()
    source.write_text(_line("ceramic-b") + _line("ceramic-c"))
    source_before = source.read_bytes()
    opened, original_open = [], capture.os.open

    def tracked_open(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        if isinstance(path, str | Path) and Path(path) == source:
            opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(capture.os, "open", tracked_open)
    with pytest.raises(InputError, match="acknowledge"):
        JSONLCollector([source], cursor, lambda _: False).collect()
    assert cursor.read_bytes() == before
    assert source.read_bytes() == source_before
    assert len(opened) == 1
    descriptor = opened[0]
    try:
        with pytest.raises(OSError) as closed:
            capture.os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        # Baseline failure must not leave the fixture's leaked descriptor open.
        try:
            capture.os.close(descriptor)
        except OSError:
            pass


def test_remote_partial_batch_retries_receipt_before_next_record(tmp_path, monkeypatch):
    source, cursor = tmp_path / "source.jsonl", tmp_path / "cursor.json"
    source.write_text(_line("ceramic-a") + _line("ceramic-b"))
    calls, reject_receipt = [], True
    monkeypatch.setattr(capture, "MAX_BATCH_RECORDS", 1)

    class Session:
        async def call_tool(self, name, arguments):
            nonlocal reject_receipt
            calls.append((name, copy.deepcopy(arguments)))
            if arguments["kind"] == "coverage" and reject_receipt:
                reject_receipt = False
                return SimpleNamespace(isError=True, structuredContent=None, content=[])
            return SimpleNamespace(isError=False, structuredContent={"id": "ack"}, content=[])

    @asynccontextmanager
    async def connection(_credential):
        yield Session()

    monkeypatch.setattr(capture_delivery, "connect", connection)

    async def exercise():
        arguments = dict(
            paths=[source],
            cursor=cursor,
            credential=tmp_path / "unused-credential",
            project="pottery",
            account="fixture",
            kind="audit",
        )
        first = await capture_delivery.collect_remote(**arguments)
        assert first == {
            "state": "capture_partial",
            "forwarded": 1,
            "cursor_checkpoint": "acknowledged_records_saved",
            "coverage_receipt": "pending_retry",
            "coverage": "partial",
            "live_provider_log": "unverified",
        }
        pending = json.loads(cursor.with_name(cursor.name + ".coverage.json").read_text())
        assert pending["details"]["forwarded"] == 1
        assert pending["details"]["pass_completed"] is False
        assert pending["details"]["batch_limit_reached"] is True
        monkeypatch.setattr(capture, "MAX_BATCH_RECORDS", 2)
        second = await capture_delivery.collect_remote(**arguments)
        assert second == {
            "state": "capture_pass_complete",
            "forwarded": 1,
            "coverage_receipt": "acknowledged",
            "coverage": "partial",
            "live_provider_log": "unverified",
        }
        assert calls[1][1] == calls[2][1] == pending
        assert calls[3][1]["details"]["event"]["source_identity"] == "unverified_snapshot"
        assert calls[3][1]["action"]["command"] == "ceramic-b"
        assert calls[0][1]["client_event_id"] != calls[3][1]["client_event_id"]
        assert json.loads(cursor.with_name(cursor.name + ".coverage.json").read_text()) == {}

    asyncio.run(exercise())


@pytest.mark.parametrize("redact_uncertain", [True, False])
def test_recursive_redaction_preserves_summary_labels_and_uncertainty(redact_uncertain):
    value = {
        "rows": [
            {
                "token": "next_item",
                "password": "SYNTHETIC_CAPTURE_72",
                "content": "Authorization: Bearer SYNTHETIC_CAPTURE_88",
            },
            ("plain", float("inf")),
        ],
        "sanitization": {
            "categories": {"token": 2, "unknown": 7},
            "truncated": {"depth": 3, "unknown": 9},
        },
    }
    result = privacy.sanitize(value, _redact_uncertain=redact_uncertain)
    assert result.value == {
        "rows": [
            {
                "token": "[REDACTED]" if redact_uncertain else "next_item",
                "password": "[REDACTED]",
                "content": "Authorization: [REDACTED]",
            },
            ["plain", "[UNSUPPORTED_NON_FINITE_FLOAT]"],
        ],
        "sanitization": {
            "categories": {"token": 2, "unknown": "[REDACTED]"},
            "truncated": {"depth": 3, "unknown": "[REDACTED]"},
        },
    }
    assert result.summary() == {
        "categories": {
            "sensitive_field": 4 if redact_uncertain else 3,
            "bearer": 1,
            "credential_header": 1,
        },
        "redactions": 6 if redact_uncertain else 5,
        "truncated": {"non_finite_float": 1},
    }
    assert result.warnings == {"sensitive_field": 1}
    assert value["rows"][0]["password"] == "SYNTHETIC_CAPTURE_72"


@pytest.mark.parametrize(
    ("module", "owner", "name"),
    [
        (capture, "JSONLCollector", "_collect"),
        (capture_delivery, None, "collect_remote"),
        (privacy, None, "sanitize"),
    ],
)
def test_entry_point_delegates_distinct_capture_privacy_responsibilities(module, owner, name):
    tree = ast.parse(Path(module.__file__).read_text())
    scope = (
        next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
        if owner
        else tree
    )
    function = next(
        node
        for node in scope.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    )
    # These entry points coordinate a pass or traversal. Per-file continuity,
    # receipt recovery and recursive value handling need separate review units.
    assert function.end_lineno - function.lineno + 1 <= 90
    assert not any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(function)
        if node is not function
    )
