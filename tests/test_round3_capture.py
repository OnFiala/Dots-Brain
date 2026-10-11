"""Disposable cursor upgrade regressions and the declared capture identity boundary."""

import copy
import json
import shlex
import subprocess

import pytest

from dots_brain.capture import JSONLCollector
from dots_brain.errors import InputError, SuppressedError
from dots_brain.store import Store


def _line(label):
    return json.dumps({"type": "shell_command", "command": label}) + "\n"


def _seed(tmp_path):
    source, cursor, events = tmp_path / "source.jsonl", tmp_path / "cursor.json", []
    source.write_text(_line("pottery-schedule-a") + _line("pottery-schedule-b"))
    collector = JSONLCollector([source], cursor, lambda event: events.append(event) or True)
    assert collector.collect() == 2
    return source, cursor, events


def test_new_cursor_declares_version_one(tmp_path):
    _, cursor, _ = _seed(tmp_path)
    assert json.loads(cursor.read_text())["format_version"] == 1


@pytest.mark.parametrize("legacy_version", [None, 0])
def test_legacy_upgrade_is_deterministic_and_preserves_all_checkpoint_evidence(
    tmp_path, legacy_version
):
    source, cursor, events = _seed(tmp_path)
    legacy = json.loads(cursor.read_text())
    legacy.pop("format_version", None)
    if legacy_version is not None:
        legacy["format_version"] = legacy_version
    checkpoint = copy.deepcopy(legacy["files"])
    cursor.write_text(json.dumps(legacy))
    delivered = []
    collector = JSONLCollector([source], cursor, lambda event: delivered.append(event) or True)
    assert collector.collect() == 0
    upgraded = json.loads(cursor.read_text())
    assert upgraded == {**legacy, "format_version": 1}
    assert upgraded["files"] == checkpoint
    saved = cursor.read_bytes()
    assert collector.collect() == 0
    assert cursor.read_bytes() == saved

    # Upgrading cannot reset the acknowledged generation or resend a forgotten row.
    forgotten_id = events[0]["record_id"]
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(source.read_bytes() + _line("pottery-schedule-c").encode())
    replacement.replace(source)
    assert collector.collect() == 1
    assert delivered[0]["action"]["command"] == "pottery-schedule-c"
    assert all(event["record_id"] != forgotten_id for event in delivered)
    assert json.loads(cursor.read_text())["files"][str(source)]["generation"] == 0


@pytest.mark.parametrize("variant", ["bare", "fingerprint", "sampled", "chain", "barrier"])
def test_supported_legacy_checkpoints_upgrade_without_fabricating_history(tmp_path, variant):
    source, cursor, _ = _seed(tmp_path)
    if variant == "fingerprint":
        with source.open("a") as stream:
            stream.write(_line("pottery-schedule-c") * 10)
        assert JSONLCollector([source], cursor, lambda _: True).collect() == 10
    data = json.loads(cursor.read_text())
    data.pop("format_version", None)
    state = data["files"][str(source)]
    state["generation"] = 4
    if variant in {"bare", "fingerprint", "sampled"}:
        for field in list(state):
            if field.startswith("acknowledged_prefix_"):
                state.pop(field)
    if variant in {"bare", "fingerprint"}:
        fingerprint = state["prefix_fingerprint"]
        for field in list(state):
            if field.startswith("prefix_") or field.startswith("boundary_"):
                state.pop(field)
        if variant == "fingerprint":
            state["fingerprint"] = fingerprint
    if variant == "barrier":
        state["source_reset_required"] = True
        state["unavailable"] = True
    cursor.write_text(json.dumps(data))
    expected = copy.deepcopy(state)
    collector = JSONLCollector([source], cursor, lambda _: pytest.fail("checkpoint replayed"))
    assert collector.collect() == 0
    upgraded = json.loads(cursor.read_text())
    assert upgraded["format_version"] == 1
    if variant == "barrier":
        assert collector.completed is False
    assert upgraded["files"][str(source)] == expected


def test_upgraded_legacy_without_chain_requires_reset_for_replacement_inode(tmp_path):
    source, cursor, _ = _seed(tmp_path)
    data = json.loads(cursor.read_text())
    data.pop("format_version", None)
    state = data["files"][str(source)]
    for field in list(state):
        if field.startswith("acknowledged_prefix_"):
            state.pop(field)
    cursor.write_text(json.dumps(data))
    events = []
    collector = JSONLCollector([source], cursor, lambda event: events.append(event) or True)
    assert collector.collect() == 0
    assert json.loads(cursor.read_text())["format_version"] == 1
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(source.read_bytes() + _line("pottery-schedule-c").encode())
    replacement.replace(source)
    assert collector.collect() == 1
    assert events[0]["action"]["reason"] == "source_rewritten_requires_reset"
    assert collector.completed is False
    assert collector.collect() == 0


@pytest.mark.parametrize("version", [2, -1, True, False, "1", 1.0, None, {}, []])
def test_unknown_or_malformed_version_refuses_delivery_and_preserves_cursor(tmp_path, version):
    source, cursor, _ = _seed(tmp_path)
    data = json.loads(cursor.read_text())
    data["format_version"] = version
    _assert_recovery_preserves_cursor(source, cursor, data)


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": True},
        {"source_reset_required": "false"},
        {"discard_until_newline": 1},
        {"prefix_length": 257},
        {"prefix_fingerprint": "invalid"},
        {"boundary_start": 999},
        {"acknowledged_prefix_length": 0},
        {"acknowledged_prefix_tail": "!not-base64!"},
        {"acknowledged_prefix_digest": None},
    ],
)
@pytest.mark.parametrize("version", [None, 1])
def test_malformed_checkpoint_is_rejected_before_legacy_upgrade(tmp_path, changes, version):
    source, cursor, _ = _seed(tmp_path)
    data = json.loads(cursor.read_text())
    if version is None:
        data.pop("format_version", None)
    data["files"][str(source)].update(changes)
    _assert_recovery_preserves_cursor(source, cursor, data)


def _assert_recovery_preserves_cursor(source, cursor, data):
    cursor.write_text(json.dumps(data, indent=3) + "\n")
    before = cursor.read_bytes()
    source_before = source.read_bytes()
    collector = JSONLCollector([source], cursor, lambda _: pytest.fail("invalid cursor delivered"))
    with pytest.raises(InputError, match="Preserve the cursor and source files"):
        collector.collect()
    assert cursor.read_bytes() == before
    assert source.read_bytes() == source_before


@pytest.mark.parametrize(
    "fault", ["future_version", "malformed_version", "checkpoint", "json_syntax"]
)
def test_cursor_refusal_diagnostic_command_and_backup_recovery_resume_without_replay(
    tmp_path, fault
):
    source, cursor, initial_events = _seed(tmp_path)
    quoted_cursor = tmp_path / "operator's capture cursor.json"
    cursor.rename(quoted_cursor)
    known_good = quoted_cursor.read_bytes()
    invalid = json.loads(known_good)
    if fault == "future_version":
        invalid["format_version"] = 2
    elif fault == "malformed_version":
        invalid["format_version"] = "1"
    elif fault == "checkpoint":
        invalid["files"][str(source)]["generation"] = -1
    quoted_cursor.write_text('{"files":' if fault == "json_syntax" else json.dumps(invalid))
    before = quoted_cursor.read_bytes()
    delivered = []
    collector = JSONLCollector(
        [source], quoted_cursor, lambda event: delivered.append(event) or True
    )
    with pytest.raises(InputError) as refused:
        collector.collect()
    command = str(refused.value).split("Next command (read-only):\n", 1)[1]
    arguments = shlex.split(command)
    assert arguments == ["python3", "-m", "json.tool", str(quoted_cursor)]
    diagnostic = subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=10)
    if fault == "json_syntax":
        assert diagnostic.returncode == 1
        assert diagnostic.stderr
    else:
        assert diagnostic.returncode == 0
        assert json.loads(diagnostic.stdout) == invalid
    assert quoted_cursor.read_bytes() == before
    assert delivered == []

    # Operator recovery is performed only against this test's disposable backup.
    quoted_cursor.write_bytes(known_good)
    with source.open("a") as stream:
        stream.write(_line("pottery-schedule-c"))
    assert collector.collect() == 1
    assert [event["action"]["command"] for event in delivered] == ["pottery-schedule-c"]
    assert not {event["record_id"] for event in initial_events}.intersection(
        event["record_id"] for event in delivered
    )


@pytest.mark.parametrize("boundary", ["path", "account", "project"])
def test_forget_suppresses_same_identity_but_other_capture_scope_can_import(tmp_path, boundary):
    """Negative control: forgetting is scoped provenance suppression, not content erasure."""
    store = Store(tmp_path / "memory")
    store.initialize()
    source = tmp_path / "original.jsonl"
    source.write_text(
        json.dumps({"role": "user", "message": "The kiln uses schedule delta."}) + "\n"
    )
    events = []
    assert (
        JSONLCollector(
            [source],
            tmp_path / "initial.cursor",
            lambda event: events.append(event) or True,
            kind="transcript",
        ).collect()
        == 1
    )
    event = events[0]
    fields = dict(
        content=event["details"]["content"],
        source=event["source"],
        event_id=event["record_id"],
        account="ceramic-assistant",
        project="workshop",
    )
    remembered = store.remember(**fields)
    assert store.forget(remembered["id"], expected_revision=1)["deleted"] is True
    with pytest.raises(SuppressedError):
        store.remember(**fields)

    if boundary == "path":
        other = tmp_path / "copied-export.jsonl"
        other.write_bytes(source.read_bytes())
        events.clear()
        assert (
            JSONLCollector(
                [other],
                tmp_path / "other.cursor",
                lambda item: events.append(item) or True,
                kind="transcript",
            ).collect()
            == 1
        )
        assert events[0]["record_id"] != fields["event_id"]
        fields["event_id"] = events[0]["record_id"]
    elif boundary == "account":
        fields["account"] = "ceramic-assistant-secondary"
    else:
        fields["project"] = "annex"
    imported = store.remember(**fields)
    assert imported["id"] != remembered["id"]
    assert store.get(imported["id"])["content"] == "The kiln uses schedule delta."
