"""Independent neutral regressions for review progress and state upgrades."""

import asyncio
import copy
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dots_brain.activity import AuditLog
from dots_brain.auth import BearerAuth, Policy, issue_client
from dots_brain.cli import parser, run
from dots_brain.errors import ConflictError, InputError
from dots_brain.server import create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store

spec = importlib.util.spec_from_file_location(
    "round3_audit_review", Path(__file__).parents[1] / "scripts/audit_review.py"
)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


@pytest.fixture
def source(tmp_path):
    store = Store(tmp_path / "store")
    store.initialize()
    audit = AuditLog(store)

    def append(key, kind="action", **kwargs):
        return audit.record(Policy(), project="fixture", kind=kind, client_event_id=key, **kwargs)

    def fetch(after, limit):
        return audit.page(Policy(), after_id=after, limit=limit)

    return append, fetch


def envelope(scan):
    return {
        "review_id": scan["review_id"],
        "events_digest": scan["events_digest"],
        "model": "fixture",
        "findings": [],
    }


@pytest.mark.parametrize("clear_key", [False, True])
@pytest.mark.parametrize("legacy_version", [None, 0])
def test_legacy_checkpoint_upgrades_only_after_review_without_losing_anchor(
    tmp_path, source, clear_key, legacy_version
):
    append, fetch = source
    append("opening", "intent")
    first = review.stage(tmp_path, "first", fetch)
    review.complete(tmp_path, envelope(first))
    legacy = review.read_json(tmp_path / "checkpoint.json")
    legacy.pop("format_version", None)
    if legacy_version is not None:
        legacy["format_version"] = legacy_version
    if clear_key:
        unresolved = legacy["unresolved"][0]
        unresolved.pop("key_hash")
        unresolved["key"] = review.canonical(["fixture", Policy().principal, "opening"])
    legacy["anomaly_counts"] = {"capture_gap": 7}
    legacy["findings"] = [
        {
            "category": "capture_gap",
            "severity": "warning",
            "status": "active",
            "event_ids": [1],
            "confidence": "high",
        }
    ]
    review.atomic_json(tmp_path / "checkpoint.json", legacy)
    before = (tmp_path / "checkpoint.json").read_bytes()
    append("closing", "receipt", intent_event_id="opening")
    calls = []

    def observed(after, limit):
        calls.append((after, limit))
        return fetch(after, limit)

    scan = review.stage(tmp_path, "next", observed)
    assert (tmp_path / "checkpoint.json").read_bytes() == before
    assert calls[0] == (0, 1)  # Revalidate the committed anchor.
    assert [item["id"] for item in scan["events"]] == [2]
    assert scan["candidate"]["unresolved"] == []
    assert scan["format_version"] == 1
    review.complete(tmp_path, envelope(scan))
    current = review.read_json(tmp_path / "checkpoint.json")
    assert current["format_version"] == 1
    assert current["last_id"] == 2
    assert current["anomaly_counts"] == {"capture_gap": 7}
    assert current["findings"] == legacy["findings"]
    assert current["event_hash"] == fetch(1, 1)["events"][0]["event_hash"]


def test_legacy_pending_review_can_complete_with_unchanged_evidence_digest(tmp_path, source):
    append, fetch = source
    append("item")
    scan = review.stage(tmp_path, "slot", fetch)
    pending = review.read_json(tmp_path / "pending.json")
    pending.pop("format_version", None)
    pending["candidate"].pop("format_version", None)
    review.atomic_json(tmp_path / "pending.json", pending)
    before = (tmp_path / "pending.json").read_bytes()
    assert review.complete(tmp_path, envelope(scan))["last_id"] == 1
    current = review.read_json(tmp_path / "checkpoint.json")
    assert current["format_version"] == 1
    assert current["events_digest"] == scan["events_digest"]
    assert (tmp_path / "pending.json").read_bytes() == before
    assert review.complete(tmp_path, envelope(scan))["state"] == "already_completed"


@pytest.mark.parametrize("name", ["checkpoint.json", "pending.json"])
@pytest.mark.parametrize("version", [2, -1, True, "1", None])
def test_unsupported_review_state_preserves_files_before_fetch_or_completion(
    tmp_path, source, name, version
):
    append, fetch = source
    append("item")
    scan = review.stage(tmp_path, "initial", fetch)
    review.complete(tmp_path, envelope(scan))
    altered = review.read_json(tmp_path / name)
    altered["format_version"] = version
    review.atomic_json(tmp_path / name, altered)
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    calls = []

    def should_not_fetch(*args):
        calls.append(args)
        return fetch(*args)

    with pytest.raises(review.ReviewError) as error:
        review.stage(tmp_path, "next", should_not_fetch)
    assert name in str(error.value)
    assert "scan" in str(error.value)
    assert not calls
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == before


def test_future_pending_version_cannot_be_acknowledged(tmp_path, source):
    append, fetch = source
    append("item")
    scan = review.stage(tmp_path, "slot", fetch)
    pending = review.read_json(tmp_path / "pending.json")
    pending["format_version"] = 9
    review.atomic_json(tmp_path / "pending.json", pending)
    before = (tmp_path / "pending.json").read_bytes()
    with pytest.raises(review.ReviewError):
        review.complete(tmp_path, envelope(scan))
    assert not (tmp_path / "checkpoint.json").exists()
    assert (tmp_path / "pending.json").read_bytes() == before


@pytest.mark.parametrize("bad", [[], {"unrelated": "field"}, {"last_id": True}])
def test_malformed_legacy_checkpoint_is_not_reset_to_empty(tmp_path, source, bad):
    _, fetch = source
    review.atomic_json(tmp_path / "checkpoint.json", copy.deepcopy(bad))
    before = (tmp_path / "checkpoint.json").read_bytes()
    with pytest.raises(review.ReviewError):
        review.stage(tmp_path, "slot", fetch)
    assert (tmp_path / "checkpoint.json").read_bytes() == before
    assert not (tmp_path / "pending.json").exists()


def test_unsupported_state_next_command_starts_separate_review_without_losing_evidence(
    tmp_path, source, monkeypatch, capsys
):
    append, fetch = source
    append("item")
    root = tmp_path / "review with 'quoted' path"
    root.mkdir(mode=0o700)
    review.atomic_json(root / "checkpoint.json", {"format_version": 99})
    review.atomic_json(
        root / "target.json",
        {
            "origin": "fixture",
            "host": "fixture.invalid",
            "user": "fixture",
            "executable": "/fixture/bin/dots-brain",
            "data_dir": "/fixture/store",
        },
    )
    before = (root / "checkpoint.json").read_bytes()
    with pytest.raises(review.ReviewError) as error:
        review.stage(root, "slot", fetch, origin="fixture")
    command = shlex.split(error.value.next_command)
    assert command[:2] == ["python", "scripts/audit_review.py"]
    monkeypatch.setattr(sys, "argv", command[1:])
    monkeypatch.setattr(review, "fetch_page", lambda after, limit, target: fetch(after, limit))
    review.main()
    scan = json.loads(capsys.readouterr().out)
    assert scan["state"] == "needs_analysis"
    assert scan["candidate"]["last_id"] == 1
    assert (root / "checkpoint.json").read_bytes() == before
    assert not (root / "pending.json").exists()


def test_state_recovery_command_preserves_custom_target_config(
    tmp_path, source, monkeypatch, capsys
):
    append, fetch = source
    append("item")
    root = tmp_path / "review with 'quotes'"
    root.mkdir(mode=0o700)
    review.atomic_json(root / "checkpoint.json", {"format_version": 99})
    target_path = tmp_path / "separate 'target' config.json"
    review.atomic_json(
        target_path,
        {
            "origin": "fixture",
            "host": "fixture.invalid",
            "user": "fixture",
            "executable": "/fixture/bin/dots-brain",
            "data_dir": "/fixture/store",
        },
    )
    before = (root / "checkpoint.json").read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_review.py", "--state-dir", str(root), "scan", "--target-config", str(target_path)],
    )
    calls = []

    def observed_fetch(after, limit, target):
        calls.append(target)
        return fetch(after, limit)

    monkeypatch.setattr(review, "fetch_page", observed_fetch)
    with pytest.raises(review.ReviewError) as error:
        review.main()
    assert not calls
    command = shlex.split(error.value.next_command)
    assert command[command.index("--target-config") + 1] == str(target_path)
    monkeypatch.setattr(sys, "argv", command[1:])
    review.main()
    scan = json.loads(capsys.readouterr().out)
    assert scan["state"] == "needs_analysis"
    assert scan["candidate"]["last_id"] == 1
    assert calls and all(target.origin == "fixture" for target in calls)
    assert (root / "checkpoint.json").read_bytes() == before
    assert not (root / "pending.json").exists()
    assert not (root / "target.json").exists()


def projection_fetch(audit):
    # The old public page is the baseline oracle, so the regression demonstrates
    # the old permanent oversize failure instead of only a missing new method.
    method = getattr(audit, "review_page", audit.page)
    return lambda after, limit: method(Policy(), after_id=after, limit=limit)


def test_accepted_http_event_progresses_then_remains_a_valid_review_anchor(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    credential = tmp_path / "credential.json"
    issue_client(
        store,
        name="fixture",
        scopes=["audit:write"],
        projects=["fixture"],
        days=1,
        output=credential,
        url="http://127.0.0.1:8765/mcp",
    )
    token = json.loads(credential.read_text())["token"]
    server = create_server(MemoryService(store), http=True)
    app = BearerAuth(server.streamable_http_app(), store)
    # JSON control escaping is generated from its shape, with no borrowed corpus.
    unit = "\x01" * 1348 + " " * 642
    arguments = {
        "project": "fixture",
        "kind": "intent",
        "client_event_id": "cycle",
        **{name: [unit] * 40 for name in ("action", "target", "details")},
    }
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "audit_record", "arguments": arguments},
    }
    while len(httpx.Request("POST", "http://127.0.0.1/mcp", json=message).content) + 5 < (
        1048576 - 32
    ):
        arguments["action"][0] = arguments["action"][0].replace(" ", "\x01", 1)
    observed_sizes = []

    async def observe(request):
        if request.method == "POST" and b'"audit_record"' in request.content:
            observed_sizes.append(len(request.content))

    async def exercise():
        async with server.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + token},
                event_hooks={"request": [observe]},
            ) as client:
                async with streamable_http_client(
                    "http://127.0.0.1:8765/mcp", http_client=client
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("audit_record", arguments)
                        assert not result.isError, result

    asyncio.run(exercise())
    assert len(observed_sizes) == 1 and observed_sizes[0] < 1048576
    audit = AuditLog(store)
    original = audit.events(Policy())
    large = original[-1]
    raw = audit.page(Policy(), after_id=large["id"] - 1, limit=1)
    assert len(review.canonical(raw).encode()) > review.MAX_PAGE_BYTES
    scan = review.stage(tmp_path, "first", projection_fetch(audit))
    omitted = scan["events"][-1]
    assert not {"action", "target", "details"}.intersection(omitted)
    assert omitted["review_omission"]["content_reviewed"] is False
    assert omitted["review_omission"]["integrity"] == "verified_by_owner_cli"
    assert scan["scan_findings"][-1]["category"] == "oversized_event"
    assert len(review.canonical(scan).encode()) < review.MAX_PAGE_BYTES
    assert review.complete(tmp_path, envelope(scan))["last_id"] == large["id"]
    audit.record(
        Policy(principal=large["principal"]),
        project="fixture",
        kind="receipt",
        client_event_id="finished",
        intent_event_id="cycle",
    )
    next_scan = review.stage(tmp_path, "second", projection_fetch(audit))
    assert next_scan["candidate"]["unresolved"] == []
    assert [item["id"] for item in next_scan["events"]] == [large["id"] + 1]
    assert review.complete(tmp_path, envelope(next_scan))["last_id"] == large["id"] + 1
    assert audit.events(Policy())[: len(original)] == original
    # Exercise the actual public CLI serialization, not a hand-made envelope.
    command = [
        sys.executable,
        "-m",
        "dots_brain.cli",
        "--data-dir",
        str(store.directory),
        "audit",
        "events",
        "--review-projection",
        "--after-id",
        str(large["id"] - 1),
        "--limit",
        "1",
    ]
    result = subprocess.run(command, capture_output=True, check=True, env=os.environ)
    assert len(result.stdout) <= review.MAX_PAGE_BYTES
    assert json.loads(result.stdout)["events"][0]["review_omission"]["content_reviewed"] is False


def test_corrupt_large_original_is_rejected_before_projection_or_checkpoint(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    audit = AuditLog(store)
    payload = ["\x02" * 1800] * 40
    audit.record(
        Policy(),
        project="fixture",
        kind="action",
        client_event_id="large",
        action=payload,
        target=payload,
        details=payload,
    )
    with store.connection(write=True) as db:
        trigger = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='audit_events_no_update'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER audit_events_no_update")
        db.execute("UPDATE audit_events SET details='{}' WHERE id=1")
        db.execute(trigger)
    with pytest.raises(ConflictError):
        review.stage(tmp_path, "slot", projection_fetch(audit))
    assert not (tmp_path / "pending.json").exists()
    assert not (tmp_path / "checkpoint.json").exists()


@pytest.mark.parametrize("change", ["version", "missing-version", "claim", "checksum", "chain"])
def test_unsupported_or_changed_projection_cannot_advance_checkpoint(tmp_path, change):
    store = Store(tmp_path / "memory")
    store.initialize()
    audit = AuditLog(store)
    audit.record(
        Policy(),
        project="fixture",
        kind="action",
        client_event_id="large",
        details=["\x03" * 1990] * 40,
    )
    fetch = projection_fetch(audit)

    def changed(after, limit):
        page = copy.deepcopy(fetch(after, limit))
        if change == "version":
            page["review_format"] = 99
        elif change == "missing-version":
            page.pop("review_format", None)
        else:
            row = page["events"][0]
            if change == "claim":
                row["review_omission"]["integrity"] = "unverified"
            elif change == "checksum":
                row["projection_hash"] = "0" * 64
            else:
                row["prev_hash"] = "wrong"
        return page

    with pytest.raises(review.ReviewError):
        review.stage(tmp_path, "slot", changed)
    assert not (tmp_path / "pending.json").exists()
    assert not (tmp_path / "checkpoint.json").exists()


def test_many_large_rows_produce_bounded_review_pages_and_aggregated_findings(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    audit = AuditLog(store)
    for index in range(36):
        audit.record(
            Policy(),
            project="fixture",
            kind="action",
            client_event_id=f"batch-{index}",
            details=["\x04" * 1700] * 40,
        )
    fetch = projection_fetch(audit)
    sizes = []

    def observed(after, limit):
        page = fetch(after, min(limit, 3))
        sizes.append(len(json.dumps(page, ensure_ascii=False).encode()))
        return page

    scan = review.stage(tmp_path, "slot", observed)
    assert scan["count"] == 36
    assert max(sizes) < review.MAX_PAGE_BYTES
    assert len(scan["candidate"]["anomalies"]) == 1
    assert scan["candidate"]["anomalies"][0]["count"] == 36
    review.complete(tmp_path, envelope(scan))
    checkpoint = review.read_json(tmp_path / "checkpoint.json")
    assert checkpoint["anomaly_counts"] == {"oversized_event": 36}
    assert len((tmp_path / "checkpoint.json").read_bytes()) < 5000


def test_review_projection_requires_unfiltered_owner_scope_and_cli_mode(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    with pytest.raises(InputError):
        AuditLog(store).review_page(Policy(projects=("fixture",)))
    for args in (["report"], ["events", "--project", "fixture"]):
        with pytest.raises(InputError, match="audit events --review-projection"):
            run(
                parser().parse_args(
                    ["--data-dir", str(store.directory), "audit", *args, "--review-projection"]
                )
            )


def test_fixed_ssh_fetch_requires_supported_projection_without_raw_fallback(monkeypatch):
    target = review.AuditTarget(
        origin="fixture",
        host="fixture.invalid",
        user="fixture",
        executable="/fixture/bin/dots-brain",
        data_dir="/fixture/store",
    )
    calls = []

    def run_command(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            stdout=json.dumps({"events": [], "next_after_id": 0, "has_more": False}).encode()
        )

    monkeypatch.setattr(review.subprocess, "run", run_command)
    with pytest.raises(review.ReviewError, match="audit events --help"):
        review.fetch_page(0, 1, target)
    assert "--review-projection" in shlex.split(calls[0][-1])


def test_projected_legacy_hash_can_anchor_a_following_current_event(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    audit = AuditLog(store)
    audit.record(
        Policy(),
        project="fixture",
        kind="action",
        client_event_id="legacy",
        details=["\x05" * 1700] * 40,
    )
    row = audit.events(Policy())[0]
    old_payload_hash = review.digest(AuditLog._payload(row))
    old_event_hash = review.hashlib.sha256(old_payload_hash.encode()).hexdigest()
    with store.connection(write=True) as db:
        trigger = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='audit_events_no_update'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER audit_events_no_update")
        db.execute(
            "UPDATE audit_events SET payload_hash=?,event_hash=? WHERE id=1",
            (old_payload_hash, old_event_hash),
        )
        db.execute(trigger)
    scan = review.stage(tmp_path, "legacy", projection_fetch(audit))
    assert scan["events"][0]["review_omission"]["content_reviewed"] is False
    review.complete(tmp_path, envelope(scan))
    created = audit.record(Policy(), project="fixture", kind="action", client_event_id="current")
    assert created["event_hash"].startswith("v3:")
    second = review.stage(tmp_path, "current", projection_fetch(audit))
    assert [event["id"] for event in second["events"]] == [2]
    assert review.complete(tmp_path, envelope(second))["last_id"] == 2


@pytest.mark.parametrize("value", ["pottery schedule", "1", "null", "true", '{"shape":"ring"}'])
@pytest.mark.parametrize("field", ["action", "target"])
def test_accepted_scalar_metadata_is_decoded_exactly_once(tmp_path, source, value, field):
    append, _ = source
    append("scalar", **{field: value})
    audit = AuditLog(Store(tmp_path / "store"))
    scan = review.stage(tmp_path, "slot", projection_fetch(audit))
    assert scan["events"][0][field] == value
    assert review.complete(tmp_path, envelope(scan))["last_id"] == 1
    assert audit.verify() == {"events": 1}


@pytest.mark.parametrize(
    "anomalies",
    [
        [{"code": "unknown_category", "event_ids": [1]}],
        [{"code": "capture_gap", "event_ids": [1], "count": True}],
        [{"code": "capture_gap", "event_ids": [0]}],
        [{"code": "capture_gap", "event_ids": [999]}],
        [{"code": "capture_gap", "event_ids": list(range(1, 40))}],
        [{"code": "capture_gap", "event_ids": [1], "extra": "field"}],
        [{"code": "capture_gap", "event_ids": [1]}] * 2,
    ],
)
def test_malformed_pending_anomalies_never_advance_or_poison_checkpoint(
    tmp_path, source, anomalies
):
    append, fetch = source
    append("item")
    scan = review.stage(tmp_path, "slot", fetch)
    pending = review.read_json(tmp_path / "pending.json")
    pending["candidate"]["anomalies"] = anomalies
    review.atomic_json(tmp_path / "pending.json", pending)
    before = (tmp_path / "pending.json").read_bytes()
    with pytest.raises(review.ReviewError):
        review.complete(tmp_path, envelope(scan))
    assert not (tmp_path / "checkpoint.json").exists()
    assert (tmp_path / "pending.json").read_bytes() == before
