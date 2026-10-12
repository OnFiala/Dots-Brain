import json
from datetime import UTC, datetime

import pytest

from dots_brain.activity import AuditLog
from dots_brain.auth import Policy
from dots_brain.errors import ConflictError, InputError, NotFoundError
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
    with pytest.raises(InputError):
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
    "since,until,expected",
    [
        ("2026-01-01T00:00:00.000000Z", None, [1, 2, 3]),
        (None, "2026-01-01T00:00:00.000000Z", [1]),
        (None, "2026-01-01T00:00:00.000001Z", [1, 2]),
        ("2026-01-01T00:00:00.000001Z", None, [2, 3]),
        ("2026-01-01T01:00:00.000001+01:00", None, [2, 3]),
    ],
)
def test_audit_time_windows_preserve_legacy_and_fractional_hashes(tmp_path, since, until, expected):
    log = AuditLog(audit_store(tmp_path))
    caller = policy("audit:read", "audit:write")
    times = ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00.000001Z", "2026-01-01T00:00:00.000002Z"]
    for index, timestamp in enumerate(times):
        log.record(
            caller,
            project="alpha",
            kind="action",
            client_event_id=str(index),
            occurred_at=timestamp,
        )
    before = log.events(caller)
    assert [row["id"] for row in log.events(caller, since=since, until=until)] == expected
    assert log.events(caller) == before
    assert before[0]["occurred_at"] == "2026-01-01T00:00:00+00:00"
    assert log.record(
        caller, project="alpha", kind="action", client_event_id="0", occurred_at=times[0]
    )["replayed"]


@pytest.mark.parametrize("until,has_more", [("2026-10-12T10:00:00Z", False), (None, True)])
def test_operational_report_windows_use_recorded_time(tmp_path, monkeypatch, until, has_more):
    from dots_brain import activity

    class Clock(datetime):
        instant = "2026-10-12T10:00:00+00:00"

        @classmethod
        def now(cls, tz=UTC):
            return datetime.fromisoformat(cls.instant).astimezone(tz)

    monkeypatch.setattr(activity, "datetime", Clock)
    log = AuditLog(audit_store(tmp_path))
    caller = policy("audit:read", "audit:write")
    for index in range(2):
        log.record(
            caller,
            project="alpha",
            kind="action",
            client_event_id=str(index),
            occurred_at="2026-01-01T00:00:00Z",
        )
        Clock.instant = "2026-10-12T10:00:00.000001+00:00"
    since = "2026-10-12T10:00:00.000000Z"
    assert log.events(caller, since=since, until=until) == []
    report = log.report(caller, since=since, until=until, limit=1)
    assert report["window"]["returned"] == 1
    assert report["window"]["has_more"] is has_more
    assert report["window"]["complete"] is not has_more
    assert report["status"] == ("partial" if has_more else "observed")
