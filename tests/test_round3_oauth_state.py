"""Round 3 regressions using synthetic temporary installations only."""

import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import dots_brain.oauth as oauth
from dots_brain.errors import InputError, StateError
from dots_brain.store import Store

ISSUER = "http://127.0.0.1:8765"
CALLBACK = "http://127.0.0.1:9999/callback"


@pytest.fixture
def installation(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    oauth.configure(store, ISSUER)
    state = oauth.OAuthStore(store)
    state.set_onboarding(open_for_seconds=600)
    return store, state


def client(state, identifier):
    value = OAuthClientInformationFull(
        client_id=identifier,
        redirect_uris=[AnyUrl(CALLBACK)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    state.register_client(value)
    return value


def request(state, registered):
    result = state.authorize(
        registered,
        AuthorizationParams(
            redirect_uri=AnyUrl(CALLBACK),
            state="synthetic",
            redirect_uri_provided_explicitly=True,
            scopes=["memory:read"],
            code_challenge="x" * 43,
            resource=state.resource,
        ),
    )
    identifier = result.rsplit("/", 1)[-1]
    assert result == ISSUER + "/oauth/pair/" + identifier
    assert identifier.startswith("req_")
    with state.store.connection() as db:
        row = db.execute("SELECT * FROM oauth_requests WHERE id=?", (identifier,)).fetchone()
        assert row is not None and row["status"] == "pending"
        assert row["client_id"] == registered.client_id
    return identifier


def grant(state, registered):
    identifier = request(state, registered)
    state.decide(identifier, redirect_host="127.0.0.1", projects=["synthetic"])
    callback = state.claim(identifier)
    code = parse_qs(urlsplit(callback["url"]).query)["code"][0]
    return state.exchange_code(registered, state.load_code(registered, code))


@pytest.mark.parametrize(
    "invalid", ['{"version":', '{"version":1,"issuer":"PRIVATE_SECRET"}', "[]"]
)
def test_corrupt_configuration_is_invalid_and_read_only(installation, invalid):
    store, state = installation
    path = store.directory / "oauth.json"
    path.write_text(invalid)
    before = path.read_bytes()
    with pytest.raises(StateError, match="oauth configure") as error:
        oauth.configuration(store)
    assert "PRIVATE_SECRET" not in str(error.value)
    status = oauth.configuration_status(store)
    assert status["state"] == "invalid"
    assert "--discard-journal --replace-issuer" in status["next_command"]
    assert "PRIVATE_SECRET" not in json.dumps(status)
    assert path.read_bytes() == before
    assert not state.enabled()


def test_absent_configuration_is_distinct_from_invalid(installation):
    store, _ = installation
    (store.directory / "oauth.json").unlink()
    assert oauth.configuration(store) is None
    assert oauth.configuration_status(store) == {"state": "not_configured"}


@pytest.mark.parametrize("committed", [False, True])
def test_recover_publication_crash_selects_db_commit_and_preserves_prior_grant(
    installation, committed
):
    store, state = installation
    registered = client(state, "prior")
    tokens = grant(state, registered)
    previous = {"version": 1, "issuer": ISSUER}
    replacement = {"version": 1, "issuer": "https://replacement.example"}
    journal = store.directory / "oauth-config-pending.json"
    journal.write_text(
        json.dumps({"version": 1, "id": "a" * 32, "previous": previous, "next": replacement})
    )
    (store.directory / "oauth.json").write_text(json.dumps(replacement))
    if committed:
        with store.connection(write=True) as db:
            oauth.revoke_all(db)
            db.execute("DELETE FROM oauth_config_commits")
            db.execute("INSERT INTO oauth_config_commits VALUES (?)", ("a" * 32,))
    before = {p.name: p.read_bytes() for p in store.directory.glob("oauth*.json")}
    assert oauth.configuration(store) == (replacement if committed else previous)
    status = oauth.configuration_status(store)
    assert status["state"] == "configured"
    assert status["reason"] == "recovery_pending"
    assert status["next_command"].endswith("oauth recover")
    assert {p.name: p.read_bytes() for p in store.directory.glob("oauth*.json")} == before
    result = getattr(oauth, "recover_configuration", oauth._recover_configuration)(store)
    assert result == {"state": "recovered", "committed": committed}
    assert not journal.exists()
    assert json.loads((store.directory / "oauth.json").read_text()) == (
        replacement if committed else previous
    )
    assert (state.policy(tokens.access_token) is None) == committed
    with store.connection() as db:
        assert db.execute("SELECT revoked FROM oauth_grants").fetchone()[0] == int(committed)
    assert getattr(oauth, "recover_configuration", oauth._recover_configuration)(store) == {
        "state": "not_pending"
    }


@pytest.mark.parametrize(
    "pending",
    [
        {"version": 1, "id": "b" * 32, "next": {"version": 1, "issuer": ISSUER}},
        {"version": 1, "id": "c" * 32, "previous": None, "next": None},
        {
            "version": True,
            "id": "d" * 32,
            "previous": None,
            "next": {"version": 1, "issuer": ISSUER},
        },
        {
            "version": 1,
            "id": "e" * 32,
            "previous": {"version": 1, "issuer": ISSUER},
            "next": {"version": 1, "issuer": "PRIVATE_SECRET"},
        },
    ],
)
def test_malformed_journal_is_retained_and_explicit_replacement_revokes(installation, pending):
    store, state = installation
    tokens = grant(state, client(state, "prior"))
    journal = store.directory / "oauth-config-pending.json"
    journal.write_text(json.dumps(pending))
    before = journal.read_bytes()
    with pytest.raises(StateError, match="--discard-journal --replace-issuer"):
        getattr(oauth, "recover_configuration", oauth._recover_configuration)(store)
    assert journal.read_bytes() == before
    assert oauth.configuration_status(store)["state"] == "invalid"
    with pytest.raises(StateError, match="--replace-issuer"):
        oauth.configure(store, ISSUER, discard_journal=True)
    oauth.configure(store, ISSUER, discard_journal=True, replace_issuer=True)
    assert not journal.exists()
    assert state.policy(tokens.access_token) is None


def test_registration_capacity_is_bounded_and_expiry_cleanup_frees_slot(installation, monkeypatch):
    store, state = installation
    monkeypatch.setattr(oauth, "MAX_CLIENTS", 2)
    first = client(state, "first")
    client(state, "second")
    with pytest.raises(RegistrationError, match="oauth purge"):
        client(state, "overflow")
    with store.connection(write=True) as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 2
        db.execute(
            "UPDATE oauth_clients SET created=? WHERE id=?", (time.time() - 901, first.client_id)
        )
    accepted = client(state, "replacement")
    request(state, accepted)
    assert state.get_client(first.client_id) is None


@pytest.mark.parametrize("historical", ["revoked", "expired"])
def test_historical_grant_does_not_exhaust_registration_or_authorization(
    installation, monkeypatch, historical
):
    store, state = installation
    monkeypatch.setattr(oauth, "MAX_CLIENTS", 1)
    prior = client(state, "historical")
    grant(state, prior)
    with store.connection(write=True) as db:
        if historical == "revoked":
            db.execute("UPDATE oauth_grants SET revoked=1")
        else:
            db.execute("UPDATE oauth_grants SET expires=?", (time.time() - 1,))
    candidate = client(state, "new")
    identifier = request(state, candidate)
    assert state.pairing(identifier)["state"] == "pending"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM oauth_grants").fetchone()[0] == 1


def test_purge_preserves_legitimate_pending_client_and_authorization(installation):
    store, state = installation
    unused = client(state, "unused")
    pending = client(state, "pending")
    identifier = request(state, pending)
    assert state.purge()["unused_clients_removed"] == 1
    assert state.get_client(unused.client_id) is None
    assert state.get_client(pending.client_id) is not None
    assert state.pairing(identifier)["state"] == "pending"
    state.decide(identifier, redirect_host="127.0.0.1", projects=["synthetic"])
    claimed = state.claim(identifier)
    assert "code" in parse_qs(urlsplit(claimed["url"]).query)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_codes").fetchone()[0] == 1


@pytest.mark.parametrize(
    "host", [None, "", " ", "https://127.0.0.1", "attacker.example", "[[127.0.0.1]]"]
)
def test_direct_approval_requires_expected_redirect_host_without_mutation(installation, host):
    store, state = installation
    identifier = request(state, client(state, "pending"))
    with pytest.raises(InputError, match="redirect host"):
        state.decide(identifier, redirect_host=host, projects=["synthetic"])
    with store.connection() as db:
        assert (
            db.execute("SELECT status FROM oauth_requests WHERE id=?", (identifier,)).fetchone()[0]
            == "pending"
        )
        assert db.execute("SELECT COUNT(*) FROM oauth_request_approvals").fetchone()[0] == 0


@pytest.mark.parametrize(
    "uri",
    [
        "https://bad_host.example/callback",
        "https://-bad.example/callback",
        "https://good.example\n/callback",
        "https://good.example/\x00callback",
        "https://good.example:99999/callback",
        "https://user:password@good.example/callback",
        "https://[::1%25eth0]/callback",
        "http://[2001:db8::1]/callback",
    ],
)
def test_registration_rejects_unsafe_redirect_origins_without_mutation(installation, uri):
    store, state = installation
    registered = OAuthClientInformationFull(
        client_id="bad", redirect_uris=[AnyUrl(CALLBACK)], token_endpoint_auth_method="none"
    ).model_copy(update={"redirect_uris": [uri]})
    with pytest.raises(RegistrationError):
        state.register_client(registered)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 0


@pytest.mark.parametrize(
    "uri, expected",
    [
        ("https://EXAMPLE.com:443/callback", "https://example.com"),
        ("http://[::1]:9999/callback", "http://[::1]:9999"),
        ("https://[2001:db8::1]:8443/callback", "https://[2001:db8::1]:8443"),
    ],
)
def test_redirect_origin_is_safe_normalized_serialization(uri, expected):
    assert oauth.redirect_origin(uri) == expected


def test_ambiguous_commit_markers_are_retained_for_owner_replacement(installation):
    store, _ = installation
    pending = {
        "version": 1,
        "id": "f" * 32,
        "previous": None,
        "next": {"version": 1, "issuer": ISSUER},
    }
    journal = store.directory / "oauth-config-pending.json"
    journal.write_text(json.dumps(pending))
    with store.connection(write=True) as db:
        db.execute("INSERT INTO oauth_config_commits VALUES (?)", ("f" * 32,))
    before = journal.read_bytes()
    with pytest.raises(StateError, match="oauth configure"):
        getattr(oauth, "recover_configuration", oauth._recover_configuration)(store)
    assert journal.read_bytes() == before
