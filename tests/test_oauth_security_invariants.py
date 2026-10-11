"""Independent OAuth boundary regressions using disposable local stores only."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from dots_brain.errors import InputError
from dots_brain.oauth import OAuthStore, configure
from dots_brain.oauth_http import OnboardingLimiter
from dots_brain.server import create_http_app, create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store

CALLBACK = "https://callback.example.test/complete"
VERIFIER = "independent_fixture_verifier_" + "v" * 32
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)


@pytest.fixture
def oauth_boundary(tmp_path):
    def create(issuer="http://127.0.0.1:8765"):
        store = Store(tmp_path / ("tls" if issuer.startswith("https:") else "plain"))
        store.initialize()
        configure(store, issuer)
        state = OAuthStore(store)
        state.set_onboarding(open_for_seconds=600)
        service = MemoryService(store)
        app = create_http_app(create_server(service, http=True), service)
        return store, state, app

    return create


def _metadata(**changes):
    return {
        "client_name": "Independent fixture client",
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "memory:read memory:write",
        **changes,
    }


def _register(http, **changes):
    response = http.post("/register", json=_metadata(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def _authorize(http, state, client):
    return http.get(
        "/authorize",
        params={
            "client_id": client["client_id"],
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "scope": "memory:read memory:write",
            "resource": state.resource,
            "state": "fixture-correlation",
        },
        follow_redirects=False,
    )


def _pending(http, state, client):
    response = _authorize(http, state, client)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(state.issuer + "/oauth/pair/"), location
    request_id = urlsplit(location).path.rsplit("/", 1)[-1]
    with state.store.connection() as db:
        row = db.execute("SELECT * FROM oauth_requests WHERE id=?", (request_id,)).fetchone()
        assert row is not None and row["client_id"] == client["client_id"]
        assert row["status"] == "pending"
    return request_id, location, response


def _approve(state, request_id):
    return state.decide(
        request_id, projects=["fixture-project"], redirect_host="callback.example.test"
    )


def _issued_form(http, state, client, request_id, pairing):
    _approve(state, request_id)
    claimed = http.post(pairing, follow_redirects=False)
    assert claimed.status_code == 303, claimed.text
    query = parse_qs(urlsplit(claimed.headers["location"]).query)
    assert query["state"] == ["fixture-correlation"]
    form = {
        "grant_type": "authorization_code",
        "client_id": client["client_id"],
        "code": query["code"][0],
        "redirect_uri": CALLBACK,
        "code_verifier": VERIFIER,
        "resource": state.resource,
    }
    return form


def _exchange(http, state, client, request_id, pairing):
    form = _issued_form(http, state, client, request_id, pairing)
    response = http.post("/token", data=form)
    assert response.status_code == 200, response.text
    assert state.policy(response.json()["access_token"]) is not None
    return response.json(), form


@pytest.mark.parametrize("issuer", ["http://127.0.0.1:8765", "https://memory.example.test"])
def test_pairing_cookie_has_browser_and_request_bound_attributes(oauth_boundary, issuer):
    _, state, app = oauth_boundary(issuer)
    with TestClient(app, base_url=issuer) as http:
        client = _register(http)
        request_id, pairing, response = _pending(http, state, client)
        cookies = SimpleCookie()
        cookies.load(response.headers["set-cookie"])
        assert len(cookies) == 1
        cookie = next(iter(cookies.values()))
        assert cookie["httponly"]
        assert cookie["samesite"].lower() == "lax"
        assert cookie["path"] == "/oauth/pair/" + request_id
        assert bool(cookie["secure"]) == issuer.startswith("https:")
        page = http.get(pairing)
        assert '<meta http-equiv="refresh" content="10">' in page.text
        _approve(state, request_id)
        approved = http.get(pairing)
        assert '<meta http-equiv="refresh"' not in approved.text
        assert state.pairing(request_id)["state"] == "approved"


def test_closed_onboarding_cannot_persist_authorization(oauth_boundary):
    store, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        client = _register(http)
        state.set_onboarding(open_for_seconds=None)
        response = _authorize(http, state, client)
        assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == [
            "temporarily_unavailable"
        ]
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM oauth_requests").fetchone()[0] == 0


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_closed_onboarding_cannot_display_or_claim_approved_pairing(oauth_boundary, method):
    store, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        client = _register(http)
        request_id, pairing, _ = _pending(http, state, client)
        _approve(state, request_id)
        state.set_onboarding(open_for_seconds=None)
        response = http.request(method, pairing, follow_redirects=False)
        assert response.status_code == 503
        with store.connection() as db:
            assert (
                db.execute(
                    "SELECT status FROM oauth_requests WHERE id=?", (request_id,)
                ).fetchone()[0]
                == "approved"
            )
            assert db.execute("SELECT COUNT(*) FROM oauth_codes").fetchone()[0] == 0


def test_oauth_changes_commit_audit_receipts_without_bearer_material(oauth_boundary):
    store, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        client = _register(http)
        request_id, pairing, _ = _pending(http, state, client)
        tokens, form = _exchange(http, state, client, request_id, pairing)
        grant_id = state.grants()["grants"][0]["id"]
        state.revoke_grant(grant_id)
        denied_id, _, _ = _pending(http, state, client)
        state.decide(denied_id, deny=True)
    with store.connection() as db:
        events = [dict(row) for row in db.execute("SELECT * FROM audit_events ORDER BY id")]
    by_action = {json.loads(row["action"]): row for row in events}
    for action, target in (
        ("oauth_approved", request_id),
        ("oauth_grant_issued", grant_id),
        ("oauth_grant_revoked", grant_id),
        ("oauth_denied", denied_id),
    ):
        assert action in by_action
        assert json.loads(by_action[action]["target"]) == target
        assert by_action[action]["project"] == "system:auth"
        assert by_action[action]["principal"] == "server:oauth"
    serialized = json.dumps(events)
    assert tokens["access_token"] not in serialized
    assert tokens["refresh_token"] not in serialized
    assert form["code"] not in serialized
    assert VERIFIER not in serialized


@pytest.mark.parametrize("method", ["client_secret_basic", "private_key_jwt"])
def test_registration_rejects_unsupported_authentication_methods(oauth_boundary, method):
    store, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        response = http.post("/register", json=_metadata(token_endpoint_auth_method=method))
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_client_metadata"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 0


@pytest.mark.parametrize("content_type", ["text/plain", "application/json"])
def test_token_rejects_non_form_content_before_any_exchange(oauth_boundary, content_type):
    _, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        client = _register(http)
        request_id, pairing, _ = _pending(http, state, client)
        form = _issued_form(http, state, client, request_id, pairing)
        response = http.post("/token", data=form, headers={"Content-Type": content_type})
        assert response.status_code == 415
        assert response.json()["error"] == "invalid_request"
        exchanged = http.post("/token", data=form)
        assert exchanged.status_code == 200
        assert state.policy(exchanged.json()["access_token"]) is not None


def test_pending_quota_is_per_client_and_does_not_block_another(oauth_boundary):
    store, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        saturated = _register(http)
        independent = _register(http)
        for _ in range(3):
            _pending(http, state, saturated)
        rejected = _authorize(http, state, saturated)
        assert parse_qs(urlsplit(rejected.headers["location"]).query)["error"] == [
            "temporarily_unavailable"
        ]
        _pending(http, state, independent)
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM oauth_requests WHERE client_id=?", (saturated["client_id"],)
            ).fetchone()[0]
            == 3
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM oauth_requests WHERE client_id=?", (independent["client_id"],)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("name", ["n" * 65, "Line\nbreak", "Invisible\u202ename"])
def test_registration_filters_oversized_and_control_client_names(oauth_boundary, name):
    _, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        response = http.post("/register", json=_metadata(client_name=name))
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_client_metadata"


def test_pairing_escapes_untrusted_client_name_and_pending_hides_it(oauth_boundary):
    _, state, app = oauth_boundary()
    name = '<img src=x onerror="alert(1)">'
    with TestClient(app, base_url=state.issuer) as http:
        client = _register(http, client_name=name)
        _, pairing, _ = _pending(http, state, client)
        page = http.get(pairing)
        assert html.escape(name) in page.text
        assert name not in page.text
    ordinary = state.pending()
    assert all("untrusted_client_name" not in row for row in ordinary["requests"])
    assert state.pending(verbose=True)["requests"][0]["untrusted_client_name"] == name


@pytest.mark.parametrize("oversize", ["redirects", "serialized"])
def test_registration_rejects_metadata_limits_without_persisting_client(oauth_boundary, oversize):
    store, state, app = oauth_boundary()
    changes = (
        {"redirect_uris": [f"https://callback.example.test/{index}" for index in range(9)]}
        if oversize == "redirects"
        else {"contacts": ["neutral" * 1500 + "@example.test"]}
    )
    with TestClient(app, base_url=state.issuer) as http:
        response = http.post("/register", json=_metadata(**changes))
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_client_metadata"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 0


def test_limiter_really_bounds_peer_table_after_repeated_refills(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("dots_brain.oauth_http.time.monotonic", lambda: clock[0])
    limiter = OnboardingLimiter()
    for index in range(700):
        # Refill the global budget so every distinct peer reaches accounting.
        clock[0] += 61
        assert limiter.allow({"path": "/register", "client": (f"peer-{index}", 1)})
    assert len(limiter.buckets) == 512
    assert ("/register", "peer-0") not in limiter.buckets
    assert ("/register", "peer-699") in limiter.buckets
    assert len(limiter.global_buckets) == 1


@pytest.mark.parametrize("header", [b"x-forwarded-for", b"cf-connecting-ip"])
def test_limiter_selects_peer_from_asgi_scope_not_forwarded_headers(monkeypatch, header):
    monkeypatch.setattr("dots_brain.oauth_http.time.monotonic", lambda: 100.0)
    limiter = OnboardingLimiter()
    admitted = [
        limiter.allow(
            {
                "path": "/register",
                "client": ("peer-A", 1),
                "headers": [(header, f"198.51.100.{i}".encode())],
            }
        )
        for i in range(12)
    ]
    assert admitted == [True] * 6 + [False] * 6
    assert limiter.allow(
        {"path": "/register", "client": ("peer-B", 2), "headers": [(header, b"198.51.100.0")]}
    )
    assert set(limiter.buckets) == {("/register", "peer-A"), ("/register", "peer-B")}


def test_limiter_global_budget_counts_only_admitted_requests(monkeypatch):
    monkeypatch.setattr("dots_brain.oauth_http.time.monotonic", lambda: 100.0)
    limiter = OnboardingLimiter()
    scope = {"path": "/register", "client": ("saturated", 1)}
    assert all(limiter.allow(scope) for _ in range(6))
    assert all(not limiter.allow(scope) for _ in range(100))
    assert (
        sum(limiter.allow({"path": "/register", "client": (f"fresh-{i}", 1)}) for i in range(55))
        == 54
    )
    assert not limiter.allow({"path": "/register", "client": ("last", 1)})


def test_http_onboarding_applies_limiter_before_handler(oauth_boundary, monkeypatch):
    monkeypatch.setattr("dots_brain.oauth_http.time.monotonic", lambda: 100.0)
    _, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        responses = [http.post("/register", json=_metadata()) for _ in range(7)]
        assert [response.status_code for response in responses] == [201] * 6 + [429]
        assert responses[-1].json()["error"] == "rate_limited"


def test_revoked_history_cleanup_allows_a_new_persisted_request(oauth_boundary, monkeypatch):
    store, state, app = oauth_boundary()
    monkeypatch.setattr("dots_brain.oauth.MAX_CLIENTS", 1)
    with TestClient(app, base_url=state.issuer) as http:
        old = _register(http)
        old_id, pairing, _ = _pending(http, state, old)
        tokens, _ = _exchange(http, state, old, old_id, pairing)
        grant_id = state.grants()["grants"][0]["id"]
        state.revoke_grant(grant_id)
        with store.connection(write=True) as db:
            db.execute("UPDATE oauth_clients SET created=0")
        new = _register(http)
        new_id, new_pairing, _ = _pending(http, state, new)
        assert http.get(new_pairing).status_code == 200
        assert state.policy(tokens["access_token"]) is None
        with store.connection() as db:
            assert (
                db.execute("SELECT revoked FROM oauth_grants WHERE id=?", (grant_id,)).fetchone()[0]
                == 1
            )
            assert (
                db.execute("SELECT client_id FROM oauth_requests WHERE id=?", (new_id,)).fetchone()[
                    0
                ]
                == new["client_id"]
            )


def test_registration_cap_preserves_existing_clients(oauth_boundary, monkeypatch):
    store, state, app = oauth_boundary()
    monkeypatch.setattr("dots_brain.oauth.MAX_CLIENTS", 2)
    with TestClient(app, base_url=state.issuer) as http:
        clients = [_register(http), _register(http)]
        rejected = http.post("/register", json=_metadata())
        assert rejected.status_code == 400
        assert rejected.json()["error"] == "invalid_client_metadata"
        assert all(state.get_client(client["client_id"]) is not None for client in clients)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 2


def test_purge_preserves_valid_pending_client_through_code_exchange(oauth_boundary):
    _, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        unused = _register(http)
        pending = _register(http)
        request_id, pairing, _ = _pending(http, state, pending)
        assert state.purge()["unused_clients_removed"] == 1
        assert state.get_client(unused["client_id"]) is None
        assert state.get_client(pending["client_id"]) is not None
        assert state.pairing(request_id)["state"] == "pending"
        _exchange(http, state, pending, request_id, pairing)


def test_discovery_advertises_only_supported_client_authentication(oauth_boundary):
    _, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        metadata = http.get("/.well-known/oauth-authorization-server").json()
        assert metadata["token_endpoint_auth_methods_supported"] == ["none", "client_secret_post"]


def test_approval_requires_matching_explicit_callback_host(oauth_boundary):
    _, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        client = _register(http)
        request_id, _, _ = _pending(http, state, client)
        with pytest.raises(InputError):
            state.decide(request_id, projects=["fixture-project"])
        with pytest.raises(InputError):
            state.decide(
                request_id, projects=["fixture-project"], redirect_host="other.example.test"
            )
        assert state.pairing(request_id)["state"] == "pending"
        assert _approve(state, request_id)["state"] == "approved"


def test_pairing_csp_lists_only_validated_callback_origin(oauth_boundary):
    _, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        client = _register(http)
        request_id, pairing, _ = _pending(http, state, client)
        for approved in (False, True):
            if approved:
                _approve(state, request_id)
            page = http.get(pairing)
            policy = page.headers["content-security-policy"]
            directives = {}
            for raw in policy.split(";"):
                fields = raw.strip().split()
                if fields:
                    assert fields[0] not in directives
                    directives[fields[0]] = fields[1:]
            assert directives.pop("default-src") == ["'none'"]
            assert directives.pop("base-uri") == ["'none'"]
            assert directives.pop("frame-ancestors") == ["'none'"]
            assert directives.pop("form-action") == ["'self'", "https://callback.example.test"]
            if approved:
                nonces = re.findall(r'<script nonce="([A-Za-z0-9_-]+)">', page.text)
                assert len(nonces) == 1
                assert directives.pop("script-src") == ["'nonce-" + nonces[0] + "'"]
            else:
                assert "<script" not in page.text
            assert directives == {}


def test_global_pending_cap_does_not_create_an_error_redirect_request(oauth_boundary, monkeypatch):
    store, state, app = oauth_boundary()
    monkeypatch.setattr("dots_brain.oauth.MAX_PENDING", 2)
    with TestClient(app, base_url=state.issuer) as http:
        first = _register(http)
        second = _register(http)
        _pending(http, state, first)
        _pending(http, state, second)
        rejected = _authorize(http, state, second)
        assert parse_qs(urlsplit(rejected.headers["location"]).query)["error"] == [
            "temporarily_unavailable"
        ]
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_requests").fetchone()[0] == 2


def test_wrong_pairing_nonce_cannot_consume_approved_request(oauth_boundary):
    store, state, app = oauth_boundary()
    with TestClient(app, base_url=state.issuer) as http:
        client = _register(http)
        request_id, pairing, _ = _pending(http, state, client)
        _approve(state, request_id)
        rejected = http.post(
            pairing,
            headers={"Cookie": "dots_brain_pair_" + request_id[-32:] + "=different-browser"},
            follow_redirects=False,
        )
        assert rejected.status_code == 403
        with store.connection() as db:
            assert (
                db.execute(
                    "SELECT status FROM oauth_requests WHERE id=?", (request_id,)
                ).fetchone()[0]
                == "approved"
            )
            assert db.execute("SELECT COUNT(*) FROM oauth_codes").fetchone()[0] == 0
        assert http.post(pairing, follow_redirects=False).status_code == 303
