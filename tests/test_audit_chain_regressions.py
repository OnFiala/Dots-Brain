import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dots_brain.activity import AuditLog
from dots_brain.auth import Policy
from dots_brain.errors import ConflictError, InputError
from dots_brain.store import Store

spec = importlib.util.spec_from_file_location(
    "audit_review", Path(__file__).parents[1] / "scripts/audit_review.py"
)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def audit_log(tmp_path):
    store = Store(tmp_path / "brain")
    store.initialize()
    return store, AuditLog(store), Policy(frozenset({"audit:read", "audit:write"}))


def v2_rows(count, *, kinds=None, evidence=None):
    rows, previous = [], ""
    for event_id in range(1, count + 1):
        kind = kinds[event_id - 1] if kinds else "action"
        item = {
            "id": event_id,
            "project": "test",
            "principal": "bot:test",
            "client_event_id": f"event-{event_id}",
            "kind": kind,
            "evidence": (evidence or {}).get(event_id, "client_report"),
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "recorded_at": f"2026-01-01T00:00:{event_id % 60:02}+00:00",
            "action": "{}",
            "target": "{}",
            "details": "{}",
            "intent_event_id": None,
        }
        payload = AuditLog._payload(item)
        item["payload_hash"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        item["prev_hash"] = previous
        item["event_hash"] = hashlib.sha256((previous + item["payload_hash"]).encode()).hexdigest()
        previous = item["event_hash"]
        rows.append(item)
    return rows


def fetcher(rows):
    def fetch(after_id, limit):
        page = [dict(row) for row in rows if row["id"] > after_id][:limit]
        return {
            "events": page,
            "next_after_id": page[-1]["id"] if page else after_id,
            "has_more": bool(page and any(row["id"] > page[-1]["id"] for row in rows)),
        }

    return fetch


def envelope(scan):
    return {
        "review_id": scan["review_id"],
        "events_digest": scan["events_digest"],
        "model": "test",
        "findings": [],
    }


def test_v2_history_remains_verifiable_and_new_v3_event_binds_identity_and_stamp(tmp_path):
    store, log, policy = audit_log(tmp_path)
    legacy = v2_rows(1)[0]
    with store.connection(write=True) as db:
        db.execute(
            """INSERT INTO audit_events(
            id,project,principal,client_event_id,kind,evidence,occurred_at,recorded_at,
            action,target,details,intent_event_id,payload_hash,prev_hash,event_hash)
            VALUES (
            :id,:project,:principal,:client_event_id,:kind,:evidence,:occurred_at,
            :recorded_at,:action,:target,:details,:intent_event_id,:payload_hash,
            :prev_hash,:event_hash)""",
            legacy,
        )
    created = log.record(policy, project="test", kind="action", client_event_id="new")
    assert created["event_hash"].startswith("v3:")
    assert log.verify() == {"events": 2}
    with store.connection(write=True) as db:
        db.execute("DROP TRIGGER audit_events_no_update")
        db.execute("UPDATE audit_events SET recorded_at='2026-01-02T00:00:00+00:00' WHERE id=2")
    with pytest.raises(ConflictError):
        log.verify()


def test_normalized_page_rejects_boolean_limits_and_decodes_json(tmp_path):
    store, log, policy = audit_log(tmp_path)
    log.record(policy, project="test", kind="action", client_event_id="one", action={"verb": "ok"})
    with store.connection(write=True) as db:
        log.observed_in_connection(
            db,
            policy,
            project="system:auth",
            kind="action",
            client_event_id="auth-grant-1",
            action={"code": "grant_issued"},
            details={"count": 1},
        )
    with pytest.raises(InputError):
        log.events(policy, limit=True)
    page = log.page(policy, limit=1)
    assert page["events"][0]["action"] == {"verb": "ok"}
    assert page["next_after_id"] == 1 and page["has_more"] is True


def test_report_uses_recorded_window_and_receipts_outside_that_page_close_intents(tmp_path):
    _, log, policy = audit_log(tmp_path)
    since = datetime.now(UTC).isoformat()
    log.record(
        policy,
        project="test",
        kind="intent",
        client_event_id="intent",
        occurred_at="2000-01-01T00:00:00+00:00",
    )
    log.record(
        policy, project="test", kind="receipt", client_event_id="receipt", intent_event_id="intent"
    )
    report = log.report(policy, since=since, limit=1)
    assert report["window"]["returned"] == 1
    assert report["coverage"]["missing_receipts"] == 0
    assert report["status"] == "partial"  # The second recorded event is on the next page.


def test_review_completes_bounded_chunks_without_losing_exact_anchor(tmp_path):
    rows = v2_rows(2101)
    fetch = fetcher(rows)
    first = review.stage(tmp_path, "first", fetch)
    assert first["candidate"]["last_id"] == 2000
    assert first["candidate"]["has_more"] is True
    assert review.complete(tmp_path, envelope(first))["state"] == "completed_chunk"
    second = review.stage(tmp_path, "second", fetch)
    assert second["events"][0]["id"] == 2001
    assert second["candidate"]["has_more"] is False
    assert review.complete(tmp_path, envelope(second))["state"] == "completed"


def test_historical_receipt_anomalies_are_findings_and_legacy_pair_keys_are_migrated_in_memory():
    rows = v2_rows(6, kinds=["intent", "receipt", "receipt", "receipt", "intent", "receipt"])
    rows[1]["intent_event_id"] = "event-1"
    rows[2]["intent_event_id"] = "event-1"
    rows[3]["intent_event_id"] = "missing"
    rows[5]["intent_event_id"] = "event-5"
    rows[5]["evidence"] = "server_observed"
    # The test deliberately models a historical database that predates receipt checks.
    previous = ""
    for row in rows:
        payload = AuditLog._payload(row)
        row["payload_hash"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        row["prev_hash"] = previous
        row["event_hash"] = hashlib.sha256((previous + row["payload_hash"]).encode()).hexdigest()
        previous = row["event_hash"]
    _, candidate = review.collect({}, fetcher(rows))
    assert {item["code"] for item in candidate["anomalies"]} == {
        "orphan_or_duplicate_receipt",
        "receipt_evidence_mismatch",
    }
    legacy_checkpoint = {
        "origin": "synthetic",
        "last_id": 1,
        "event_hash": rows[0]["event_hash"],
        "unresolved": [{"key": json.dumps(["test", "bot:test", "event-1"]), "id": 1}],
    }
    _, migrated = review.collect(legacy_checkpoint, fetcher(rows[:2]))
    assert migrated["unresolved"] == []
