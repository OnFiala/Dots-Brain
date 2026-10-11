"""Retained v2 access, provenance and transaction contracts on disposable stores."""

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from starlette.testclient import TestClient
from test_oauth import CALLBACK, ISSUER, register, request_code
from test_oauth import installation as installation

from dots_brain import oauth, operations
from dots_brain.auth import issue_client, validate_endpoint
from dots_brain.errors import InputError
from dots_brain.oauth import RETIRED, OAuthStore, configure, disable
from dots_brain.operations import activate_restore, backup_store, restore_store
from dots_brain.removal import uninstall
from dots_brain.server import create_http_app, create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store


@pytest.fixture
def issued(installation):
    store, state, _, app = installation
    with TestClient(app, base_url=ISSUER) as http:
        client = register(http, method="client_secret_post")
        tokens = http.post("/token", data=request_code(http, client, state)).json()
        yield store, state, http, client, tokens


def authorization_rows(store):
    with store.connection() as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'oauth_%'"
            )
        ]
        return {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in tables
        }


@pytest.mark.parametrize("denied", [False, True])
def test_first_authorization_decision_is_final_and_identical_retry_is_idempotent(issued, denied):
    _, state, http, client, _ = issued
    request_id, _ = request_code(http, client, state, approve=False)
    decision = {"deny": True} if denied else {"projects": ["work"], "scopes": ["memory:read"]}
    first = state.decide(request_id, **decision)
    assert state.decide(request_id, **decision) == first
    with pytest.raises(InputError):
        state.decide(request_id, deny=not denied, projects=["elsewhere"])
    if not denied:
        with pytest.raises(InputError):
            state.decide(request_id, projects=["work"], scopes=["memory:write"])


def test_registration_cleanup_preserves_a_client_with_a_live_code(issued):
    store, state, http, client, _ = issued
    code = request_code(http, client, state)
    with store.connection(write=True) as db:
        db.execute("UPDATE oauth_clients SET created=0")
        db.execute("UPDATE oauth_grants SET expires=0")
    register(http)
    assert state.get_client(client["client_id"]) is not None
    assert http.post("/token", data=code).status_code == 200


def test_expired_grant_history_survives_cleanup_without_using_registration_capacity(
    issued, monkeypatch
):
    store, state, http, client, _ = issued
    original = state.grants()["grants"][0]
    with store.connection(write=True) as db:
        db.execute("UPDATE oauth_clients SET created=0")
        db.execute("UPDATE oauth_grants SET expires=0")
    monkeypatch.setattr(oauth, "MAX_CLIENTS", 1)
    register(http)
    assert state.get_client(client["client_id"]) is None
    assert state.grants()["grants"] == [original | {"expires": 0}]
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 2


def test_grant_expires_after_30_days_without_extension_by_refresh(issued, monkeypatch):
    _, state, _, client, tokens = issued
    grant = state.grants()["grants"][0]
    assert grant["expires"] - grant["created_at"] == pytest.approx(30 * 86400, abs=0.01)
    monkeypatch.setattr(oauth.time, "time", lambda: grant["expires"] - 10)
    current = state.get_client(client["client_id"])
    refresh = state.load_token(tokens["refresh_token"], "refresh", client["client_id"])
    renewed = state.exchange_refresh(current, refresh, refresh.scopes)
    assert renewed.expires_in <= 10
    assert state.grants()["grants"] == [grant]
    monkeypatch.setattr(oauth.time, "time", lambda: grant["expires"])
    assert state.policy(renewed.access_token) is None
    assert state.load_token(renewed.refresh_token, "refresh", client["client_id"]) is None


@pytest.mark.parametrize("operation", ["issuer", "disable", "uninstall", "restore"])
def test_bulk_invalidation_retires_old_client_ids_and_preserves_grant_provenance(
    issued, tmp_path, operation
):
    store, state, _, client, tokens = issued
    before = state.grants()["grants"][0]
    target = store
    issuer = ISSUER
    if operation == "issuer":
        issuer = "https://replacement.example.invalid"
        configure(store, issuer, replace_issuer=True)
    elif operation == "disable":
        disable(store)
    elif operation == "uninstall":
        uninstall(store)
    else:
        backup = tmp_path / "snapshot.sqlite3"
        backup_store(store, backup)
        target = Store(tmp_path / "restored")
        restore_store(backup, target, latest_deletions=store)
        activate_restore(store, target, writers_stopped=True)
    assert state.policy(tokens["access_token"]) is None
    with target.connection() as db:
        metadata = json.loads(
            db.execute(
                "SELECT metadata FROM oauth_clients WHERE id=?", (client["client_id"],)
            ).fetchone()[0]
        )
        assert metadata[RETIRED] is True and "client_secret" not in metadata
        assert metadata["client_name"] == client["client_name"]
        assert db.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM oauth_grant_provenance").fetchone()[0] == 1
    (target.directory / "disabled.json").unlink(missing_ok=True)  # Explicit synthetic resume.
    configure(target, issuer)
    restarted = OAuthStore(target)
    assert restarted.get_client(client["client_id"]) is None
    assert restarted.policy(tokens["access_token"]) is None
    assert restarted.grants()["grants"] == [before | {"revoked": 1}]


@pytest.mark.parametrize("marker", [False, True])
def test_dynamic_registration_cannot_set_server_retirement_metadata(issued, marker):
    _, _, http, _, _ = issued
    response = http.post("/register", json={"redirect_uris": [CALLBACK], RETIRED: marker})
    assert response.status_code == 400
    assert RETIRED not in json.dumps(register(http))


def test_replacing_issuer_requires_explicit_choice_before_any_access_change(issued):
    store, _, _, _, _ = issued
    before = authorization_rows(store)
    config = (store.directory / "oauth.json").read_bytes()
    with pytest.raises(InputError, match="replace-issuer"):
        configure(store, "https://replacement.example.invalid")
    assert authorization_rows(store) == before
    assert (store.directory / "oauth.json").read_bytes() == config


@pytest.mark.parametrize("published", [False, True])
def test_config_publication_failure_keeps_revocation_and_reports_uncertain_file_state(
    issued, monkeypatch, published
):
    store, state, _, client, tokens = issued
    grant = state.grants()["grants"][0]
    write = oauth.write_json

    def fail(path, value):
        if published:
            write(path, value)
        raise OSError("synthetic publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(oauth, "write_json", fail)
        with pytest.raises(InputError, match="committed.*publication is uncertain"):
            configure(store, "https://replacement.example.invalid", replace_issuer=True)
    assert oauth.configuration(store)["issuer"] == (
        "https://replacement.example.invalid" if published else ISSUER
    )
    assert state.policy(tokens["access_token"]) is None
    configure(store, "https://replacement.example.invalid", replace_issuer=True)
    state = OAuthStore(store)
    assert state.get_client(client["client_id"]) is None
    assert state.grants()["grants"] == [grant | {"revoked": 1}]


@pytest.mark.parametrize("removed", [False, True])
def test_disable_file_failure_does_not_restore_access(issued, monkeypatch, removed):
    store, state, _, _, tokens = issued
    original = Path.unlink

    def fail(path, *args, **kwargs):
        if path == store.directory / "oauth.json":
            if removed:
                original(path, *args, **kwargs)
            raise OSError("synthetic removal failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        with pytest.raises(InputError, match="invalidated.*uncertain"):
            disable(store)
    assert (store.directory / "oauth.json").exists() is not removed
    assert state.policy(tokens["access_token"]) is None
    assert disable(store)["state"] == "oauth_disabled"


@pytest.mark.parametrize(
    "operation",
    ["owner", "token", "replay", "rotation", "issuer", "disable", "uninstall", "restore"],
)
def test_revocation_and_audit_failure_roll_back_in_the_same_transaction(
    issued, tmp_path, monkeypatch, operation
):
    source, state, _, client_info, tokens = issued
    store = source
    client = state.get_client(client_info["client_id"])
    grant_id = state.grants()["grants"][0]["id"]
    refresh = state.load_token(tokens["refresh_token"], "refresh", client.client_id)
    if operation == "replay":
        state.exchange_refresh(client, refresh, refresh.scopes)
    if operation == "rotation":
        monkeypatch.setattr(oauth, "MAX_REFRESH_ROTATIONS", 0)
    if operation == "restore":
        backup = tmp_path / "backup.sqlite3"
        backup_store(source, backup)
        store = Store(tmp_path / "restore")
        restore_store(backup, store, latest_deletions=source)
    with store.connection(write=True) as db:
        db.execute(
            "CREATE TRIGGER reject_auth_audit BEFORE INSERT ON audit_events "
            "WHEN NEW.project='system:auth' "
            "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"
        )
    before = authorization_rows(store)

    def perform():
        if operation == "owner":
            state.revoke_grant(grant_id)
        elif operation == "token":
            state.revoke_token(state.load_token(tokens["access_token"], "access"))
        elif operation == "replay":
            state.load_token(tokens["refresh_token"], "refresh", client.client_id)
        elif operation == "rotation":
            state.exchange_refresh(client, refresh, refresh.scopes)
        elif operation == "issuer":
            configure(store, "https://replacement.example.invalid", replace_issuer=True)
        elif operation == "disable":
            disable(store)
        elif operation == "uninstall":
            uninstall(store)
        else:
            activate_restore(source, store, writers_stopped=True)

    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        perform()
    assert authorization_rows(store) == before
    if operation in {"issuer", "disable"}:
        assert oauth.configuration(store)["issuer"] == ISSUER
    if operation == "restore":
        assert not (source.directory / "disabled.json").exists()
    with store.connection(write=True) as db:
        db.execute("DROP TRIGGER reject_auth_audit")
    if operation == "rotation":
        from mcp.server.auth.provider import TokenError

        with pytest.raises(TokenError):
            perform()
    else:
        perform()
    reasons = {
        "owner": "owner_revoked",
        "token": "token_revoked",
        "replay": "refresh_replay",
        "rotation": "refresh_rotation_limit",
        "issuer": "issuer_changed",
        "disable": "oauth_disabled",
        "uninstall": "uninstalled",
        "restore": "restore_reset",
    }
    with store.connection() as db:
        assert (
            db.execute("SELECT revoked FROM oauth_grants WHERE id=?", (grant_id,)).fetchone()[0]
            == 1
        )
        event = db.execute("SELECT details FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        assert json.loads(event[0])["value"]["reason"] == reasons[operation]


def test_restore_refuses_to_discard_grants_created_after_the_snapshot(issued, tmp_path):
    source, state, http, client, _ = issued
    backup = tmp_path / "backup.sqlite3"
    backup_store(source, backup)
    target = Store(tmp_path / "restore")
    restore_store(backup, target, latest_deletions=source)
    assert http.post("/token", data=request_code(http, client, state)).status_code == 200
    with pytest.raises(InputError, match="oauth_history"):
        activate_restore(source, target, writers_stopped=True)
    assert not (source.directory / "disabled.json").exists()
    assert len(state.grants()["grants"]) == 2


@pytest.mark.parametrize("boundary", ["pending", "sync", "committed"])
def test_restore_reset_retries_with_one_audit_event_after_interruption(
    issued, tmp_path, monkeypatch, boundary
):
    source, _, _, _, _ = issued
    backup = tmp_path / "backup.sqlite3"
    backup_store(source, backup)
    target = Store(tmp_path / "restore")
    restore_store(backup, target, latest_deletions=source)
    recovery_id = json.loads((target.directory / "disabled.json").read_text())["recovery_id"]
    write, sync = operations.write_json, operations.sync_file_and_parent

    def fail_write(path, value):
        write(path, value)
        if value.get("cutover") == boundary:
            raise OSError("synthetic crash")

    def fail_sync(path):
        sync(path)
        raise OSError("synthetic crash")

    with monkeypatch.context() as patch:
        patch.setattr(
            operations,
            "sync_file_and_parent" if boundary == "sync" else "write_json",
            fail_sync if boundary == "sync" else fail_write,
        )
        with pytest.raises(OSError, match="synthetic crash"):
            activate_restore(source, target, writers_stopped=True)
    assert activate_restore(source, target, writers_stopped=True)["state"] == "restored_ready"
    with target.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE client_event_id=?",
                ("restore_reset:" + recovery_id,),
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT revoked FROM oauth_grants").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["extra_audit", "new_credential", "corrupt_reset"])
def test_restore_retry_refuses_unrelated_audit_or_changed_credentials(
    issued, tmp_path, monkeypatch, change
):
    source, _, _, _, _ = issued
    if change == "new_credential":
        issue_client(
            source,
            name="late",
            scopes=["memory:read"],
            projects=["work"],
            days=1,
            output=tmp_path / "original.json",
            url=ISSUER + "/mcp",
        )
    backup = tmp_path / "backup.sqlite3"
    backup_store(source, backup)
    target = Store(tmp_path / "restore")
    restore_store(backup, target, latest_deletions=source)
    with monkeypatch.context() as patch:
        patch.setattr(
            operations,
            "sync_file_and_parent",
            lambda _: (_ for _ in ()).throw(OSError("synthetic")),
        )
        with pytest.raises(OSError):
            activate_restore(source, target, writers_stopped=True)
    if change == "new_credential":
        with pytest.raises(InputError, match="disabled"):
            issue_client(
                target,
                name="late",
                scopes=["memory:read"],
                projects=["work"],
                days=1,
                output=tmp_path / "late.json",
                url=ISSUER + "/mcp",
            )
        with target.connection(write=True) as db:
            db.execute("UPDATE clients SET revoked=0")  # Damaged offline recovery state.
    else:
        with target.connection(write=True) as db:
            if change == "extra_audit":
                oauth.record_auth_change(target, db, "unrelated", details={"reason": "fixture"})
            else:
                db.execute("DROP TRIGGER audit_events_no_update")
                db.execute(
                    "UPDATE audit_events SET details='{}' "
                    "WHERE id=(SELECT MAX(id) FROM audit_events)"
                )
    with pytest.raises(InputError):
        activate_restore(source, target, writers_stopped=True)
    assert (source.directory / "disabled.json").exists()
    assert (target.directory / "disabled.json").exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid:bad/mcp",
        "https://example.invalid:0/mcp",
        "http://[bad/mcp",
        "https://@example.invalid/mcp",
        "https://example.invalid/\nmcp",
    ],
)
def test_endpoint_input_errors_are_bounded_and_do_not_echo_the_address(url):
    with pytest.raises(InputError) as error:
        validate_endpoint(url)
    assert url not in str(error.value)


@pytest.mark.parametrize("stage", ["startup", "running"])
@pytest.mark.parametrize(
    "bad",
    [
        "{invalid",
        '{"version":true,"issuer":"https://example.invalid"}',
        '{"version":1,"issuer":"https://example.invalid:bad"}',
    ],
)
def test_invalid_oauth_configuration_never_falls_back_to_static_public_access(
    issued, tmp_path, stage, bad
):
    store, _, _, _, _ = issued
    credential = tmp_path / "private.json"
    issue_client(
        store,
        name="private",
        scopes=["memory:read"],
        projects=["work"],
        days=1,
        output=credential,
        url=ISSUER + "/mcp",
    )
    token = json.loads(credential.read_text())["token"]
    config = store.directory / "oauth.json"
    if stage == "startup":
        config.write_text(bad)
    service = MemoryService(store)
    app = create_http_app(create_server(service, http=True), service, public_gateway=True)
    with TestClient(app, base_url=ISSUER) as http:
        config.write_text(bad)
        assert (
            http.post(
                "/mcp", headers={"Authorization": "Bearer " + token, "Host": "localhost"}, json={}
            ).status_code
            == 401
        )
        assert http.post("/register", json={}).status_code in {401, 503}


@pytest.mark.parametrize("operation", ["issuer", "disable", "uninstall"])
def test_live_http_lifetime_lock_blocks_bulk_maintenance_without_data_change(
    installation, free_ports, http_process, operation
):
    store, _, _, _ = installation
    with http_process(store, *free_ports):
        before = authorization_rows(store)
        config = (store.directory / "oauth.json").read_bytes()
        with pytest.raises(InputError, match="lock is busy"):
            if operation == "issuer":
                configure(store, "https://replacement.example.invalid", replace_issuer=True)
            elif operation == "disable":
                disable(store)
            else:
                uninstall(store)
        assert authorization_rows(store) == before
        assert (store.directory / "oauth.json").read_bytes() == config
        assert not (store.directory / "disabled.json").exists()


def test_two_real_listeners_use_server_policy_even_with_spoofed_host(
    installation, tmp_path, free_ports, http_process
):
    store, _, _, _ = installation
    private, public = [f"http://127.0.0.1:{port}" for port in free_ports]
    configure(store, public, replace_issuer=True)
    state = OAuthStore(store)
    credentials = tmp_path / "private.json"
    issue_client(
        store,
        name="synthetic",
        scopes=["memory:read"],
        projects=["work"],
        days=1,
        output=credentials,
        url=private + "/mcp",
    )
    static = json.loads(credentials.read_text())["token"]
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "fixture", "version": "1"},
        },
    }
    with http_process(store, *free_ports), httpx.Client(base_url=public, trust_env=False) as http:
        client = register(http)
        token = http.post("/token", data=request_code(http, client, state)).json()["access_token"]
        for host in (f"localhost:{free_ports[0]}", urlsplit(public).netloc):
            headers = {
                "Authorization": "Bearer " + static,
                "Host": host,
                "Accept": "application/json, text/event-stream",
            }
            assert http.post(private + "/mcp", headers=headers, json=initialize).status_code == 200
            assert http.post(public + "/mcp", headers=headers, json=initialize).status_code == 401
            for address in (private, public):
                response = http.post(
                    address + "/mcp",
                    headers=headers | {"Authorization": "Bearer " + token},
                    json=initialize,
                )
                assert response.status_code == 200
                assert response.json()["result"]["serverInfo"]["name"] == "Dots Brain"
