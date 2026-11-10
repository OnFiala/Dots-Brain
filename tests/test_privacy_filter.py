"""Synthetic regression corpus for the content guard and audit minimization."""

import json
from urllib.parse import parse_qsl, urlsplit

import pytest

from dots_brain.activity import AuditLog
from dots_brain.auth import Policy
from dots_brain.errors import InputError
from dots_brain.privacy import check_content, sanitize
from dots_brain.store import Store

CANARY = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"


@pytest.mark.parametrize(
    "content",
    [
        "ssh://git@github.com/acme/repo.git",
        "https://deploy@git.example.com/repo",
        "postgres://app@db.internal:5432/main",
        "https://example.com/guide?token=overview",
        "api_key=YOUR_KEY_HERE",
        "Authorization: Bearer <token>",
        "sk-learn-compatible-estimators",
        "Authorization: Bearer YOUR_TOKEN_HERE",
        "api_key=${DEPLOY_API_KEY}",
        "password: minimum 12 characters",
    ],
)
def test_technical_notes_preserve_content_identity_and_replay(tmp_path, content):
    store = Store(tmp_path / "memory")
    store.initialize()
    fields = dict(content=content, source="test", account="owner", event_id="note")
    first = store.remember(**fields)
    replay = store.remember(**fields)
    assert store.get(first["id"])["content"] == content
    assert replay["id"] == first["id"] and replay["changed"] is False
    assert replay.get("warnings") == first.get("warnings")
    assert store.status()["revisions"] == 1


@pytest.mark.parametrize(
    "text",
    [
        CANARY,
        "sk-abcdefghijklmnopqrstuvwx",
        "sk-learn-" + CANARY,
        "sk-learn-ABCDefgh1234567890",
        "api_key=YOUR_KEY_HERE_" + CANARY,
        "Authorization: Bearer <token> " + CANARY,
        "Authorization: Bearer SYNTHETIC_CREDENTIAL_VALUE",
        "Authorization: Basic SYNTHETIC_CREDENTIAL_VALUE",
        "Cookie: session=SYNTHETIC_CREDENTIAL_VALUE",
        "api_key=[REDACTED_EXTRA]SYNTHETIC_CREDENTIAL_VALUE",
        "https://user:password@example.test/db",
        "https://" + CANARY + "@example.test/repo",
        "https://%67hp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890@example.test/repo",
        "https://example.test/?access_token=SYNTHETIC_CREDENTIAL_VALUE&page=2",
        "https://example.test/?token=" + CANARY,
        "https://[broken]/?api_key=SYNTHETIC_CREDENTIAL_VALUE",
        "https://example.test／evil?api_key=SYNTHETIC_CREDENTIAL_VALUE",
        "https://api_key=SYNTHETIC_CREDENTIAL_VALUE@example.test/path",
        "https://api_key%3DSYNTHETIC_CREDENTIAL_VALUE@example.test/path",
        "password=https://example.test/SYNTHETIC_CREDENTIAL_VALUE",
        "api_key=https://example.test/SYNTHETIC_CREDENTIAL_VALUE?mode=live",
        "https://example.test/?redirect=/" + CANARY + "&token=overview",
        "https://example.test/?redirect=%2F" + CANARY,
        "-----BEGIN PRIVATE KEY-----\nSYNTHETIC_CREDENTIAL_VALUE",
    ],
)
def test_credentials_and_placeholder_suffixes_still_reject_without_storage(tmp_path, text):
    store = Store(tmp_path / "memory")
    store.initialize()
    with pytest.raises(InputError, match="Fields: content.*Categories:") as failure:
        store.remember(content=text, source="test", account="owner", event_id="rejected")
    assert text not in str(failure.value)
    assert CANARY not in str(failure.value)
    assert "SYNTHETIC_CREDENTIAL_VALUE" not in str(failure.value)
    assert store.status()["memories"] == 0


@pytest.mark.parametrize("key", ["token", "access_token", "api_key", "sessionToken"])
def test_url_sanitization_preserves_other_query_pairs_and_rechecks(key):
    text = f"https://example.test/guide?{key}={CANARY}&page=2&tag=a&tag=b&empty=#section"
    result = sanitize(text)
    assert CANARY not in result.value and result.redactions > 0
    parsed = urlsplit(result.value)
    assert parse_qsl(parsed.query, keep_blank_values=True) == [
        (key, "[REDACTED_TOKEN]"),
        ("page", "2"),
        ("tag", "a"),
        ("tag", "b"),
        ("empty", ""),
    ]
    assert parsed.fragment == "section"
    repeated = sanitize(result.value)
    assert repeated.value == result.value
    assert repeated.redactions == 0 and not repeated.warnings
    assert not check_content({"content": result.value})


@pytest.mark.parametrize(
    "value",
    [
        {"apiKey": CANARY, "db_password": CANARY},
        "Authorization: Bearer " + CANARY,
        "Cookie: session=SYNTHETIC_CREDENTIAL_VALUE",
        "https://user:SYNTHETIC_CREDENTIAL_VALUE@example.test/db?access_token=" + CANARY,
    ],
)
def test_supported_redacted_values_are_stable(value):
    first = sanitize(value)
    second = sanitize(first.value)
    assert first.redactions > 0 and second.redactions == 0
    assert second.value == first.value
    mapping = first.value if isinstance(first.value, dict) else {"content": first.value}
    assert not check_content(mapping)


def test_audit_persists_redacted_url_without_losing_safe_metadata(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    log = AuditLog(store)
    log.record(
        Policy(),
        project="alpha",
        kind="action",
        client_event_id="url",
        details={"url": f"https://example.test/?token={CANARY}&page=2"},
    )
    rendered = json.dumps(log.events(Policy()))
    assert CANARY not in rendered and "page=2" in rendered
    with store.connection() as db:
        persisted = db.execute("SELECT details FROM audit_events").fetchone()[0]
    assert CANARY not in persisted and "page=2" in persisted


@pytest.mark.parametrize("suffix", ["#section", "&page=2#section", ""])
def test_last_sensitive_query_parameter_preserves_fragment(suffix):
    first = sanitize(f"https://example.test/?access_token={CANARY}{suffix}")
    assert urlsplit(first.value).fragment == urlsplit("https://example.test/" + suffix).fragment
    assert sanitize(first.value).value == first.value
    assert sanitize(first.value).redactions == 0
    assert not check_content({"content": first.value})


@pytest.mark.parametrize(
    "url",
    [
        "https://[broken]/?api_key=SYNTHETIC_CREDENTIAL_VALUE",
        "https://example.test／evil?api_key=SYNTHETIC_CREDENTIAL_VALUE",
        "https://api_key=SYNTHETIC_CREDENTIAL_VALUE@example.test/path",
        "https://api_key%3DSYNTHETIC_CREDENTIAL_VALUE@example.test/path",
    ],
)
def test_invalid_urls_and_credential_usernames_cannot_enter_audit(tmp_path, url):
    store = Store(tmp_path / "memory")
    store.initialize()
    log = AuditLog(store)
    log.record(
        Policy(), project="alpha", kind="action", client_event_id="url", details={"url": url}
    )
    with store.connection() as db:
        persisted = db.execute("SELECT details FROM audit_events").fetchone()[0]
    assert "SYNTHETIC_CREDENTIAL_VALUE" not in persisted
    cleaned = sanitize(url)
    assert "SYNTHETIC_CREDENTIAL_VALUE" not in cleaned.value
    assert sanitize(cleaned.value).value == cleaned.value
    assert sanitize(cleaned.value).redactions == 0


@pytest.mark.parametrize(
    "content",
    [
        "password=https://example.test/SYNTHETIC_CREDENTIAL_VALUE",
        "api_key=https://example.test/SYNTHETIC_CREDENTIAL_VALUE?mode=live",
        "https://example.test/?redirect=/" + CANARY + "&token=overview&page=2#section",
        "https://example.test/?redirect=%2F" + CANARY + "&page=2#section",
    ],
)
def test_url_encoding_and_assignment_boundaries_do_not_hide_audit_credentials(tmp_path, content):
    store = Store(tmp_path / "memory")
    store.initialize()
    log = AuditLog(store)
    log.record(
        Policy(), project="alpha", kind="action", client_event_id="boundary", details=content
    )
    with store.connection() as db:
        persisted = db.execute("SELECT details FROM audit_events").fetchone()[0]
    assert CANARY not in persisted and "SYNTHETIC_CREDENTIAL_VALUE" not in persisted
    clean = sanitize(content)
    assert CANARY not in clean.value and "SYNTHETIC_CREDENTIAL_VALUE" not in clean.value
    assert sanitize(clean.value).redactions == 0
    assert sanitize(clean.value).value == clean.value
    assert not check_content({"content": clean.value})
    if content.startswith("https://"):
        assert "page=2#section" in clean.value
