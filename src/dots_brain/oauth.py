"""Host-local OAuth persistence and owner authorization for the official MCP SDK."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import secrets
import shlex
import time
import unicodedata
import uuid
from functools import partial
from urllib.parse import urlsplit

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

from .auth import MEMORY_SCOPES, SCOPES, Policy, validate_endpoint
from .errors import InputError, StateError
from .installation_state import marker_path
from .local import read_json, sync_directory, write_json
from .store import Store, normalize_projects

MAX_CLIENTS = 256
MAX_PENDING = 128
MAX_PENDING_PER_CLIENT = 3
MAX_REFRESH_ROTATIONS = 4096
ONBOARDING_MAX_SECONDS = 60 * 60


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def record_auth_change(store, db, action, *, target=None, details=None):
    """Commit bounded authorization metadata alongside the authorization change."""
    from .activity import AuditLog

    return AuditLog(store).observed_in_connection(
        db,
        Policy(frozenset({"audit:write"}), ("system:auth",), "server:oauth"),
        project="system:auth",
        kind="action",
        client_event_id=uuid.uuid4().hex,
        action=action,
        target=target,
        details=details,
    )


def issuer_url(value: str) -> str:
    if not isinstance(value, str):
        raise InputError("OAuth issuer must be an HTTPS origin.")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise InputError("OAuth issuer has an invalid host or port.") from None
    if (
        parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
        or not parts.hostname
        or (
            parts.scheme != "https"
            and not (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"})
        )
    ):
        raise InputError("Use an HTTPS origin without a path, credentials, query, or fragment.")
    validate_endpoint(value.rstrip("/") + "/mcp")
    try:
        return str(AnyHttpUrl(value)).rstrip("/")
    except ValueError:
        raise InputError("OAuth issuer has an invalid host or port.") from None


def _read_configuration_file(path) -> dict:
    config = read_json(path)
    if (
        type(config.get("version")) is not int
        or config["version"] != 1
        or not isinstance(config.get("issuer"), str)
    ):
        raise InputError("Unsupported OAuth configuration.")
    return {"version": 1, "issuer": issuer_url(config["issuer"])}


def _read_configuration(store: Store) -> dict | None:
    if (store.directory / "oauth-config-pending.json").exists():
        return _inspect_pending_configuration(store)[0]
    path = store.directory / "oauth.json"
    return None if not path.exists() else _read_configuration_file(path)


def _recovery_command(store: Store, *, discard: bool = False) -> str:
    base = f"dots-brain --data-dir {shlex.quote(str(store.directory))} oauth"
    return (
        base + " configure --issuer https://memory.example --discard-journal --replace-issuer"
        if discard
        else base + " recover"
    )


def configuration(store: Store) -> dict | None:
    """Read the logical configuration without repairing files or concealing corruption."""
    try:
        return _read_configuration(store)
    except (InputError, StateError, OSError, ValueError):
        command = _recovery_command(store, discard=True)
        raise StateError(
            "OAuth configuration is invalid. Preserve oauth.json and oauth-config-pending.json "
            "for inspection; explicit replacement revokes all grants. Run: " + command
        ) from None


def configuration_status(store: Store) -> dict:
    """Report safe, read-only operator status without echoing invalid file contents."""
    try:
        config = configuration(store)
    except StateError:
        return {
            "state": "invalid",
            "reason": "invalid_configuration_or_journal",
            "next_command": _recovery_command(store, discard=True),
        }
    result = {"state": "not_configured" if config is None else "configured"}
    if config is not None:
        result["issuer"] = config["issuer"]
    if (store.directory / "oauth-config-pending.json").exists():
        result.update(reason="recovery_pending", next_command=_recovery_command(store))
    return result


def onboarding_path(store: Store):
    return store.directory / "oauth-onboarding.json"


def revoke_all(db) -> dict:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='oauth_clients'").fetchone():
        clients = db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
        db.execute(
            "INSERT OR IGNORE INTO oauth_client_revocations SELECT id,? FROM oauth_clients",
            (time.time(),),
        )
        grants = db.execute("UPDATE oauth_grants SET revoked=1 WHERE revoked=0").rowcount
        for table in ("oauth_tokens", "oauth_codes", "oauth_requests"):
            db.execute(f"DELETE FROM {table}")
        # Keep author/grant provenance, but remove every usable client secret.
        for row in db.execute("SELECT id,metadata FROM oauth_clients").fetchall():
            metadata = json.loads(row["metadata"])
            metadata.pop("client_secret", None)
            metadata.pop("registration_access_token", None)
            db.execute(
                "UPDATE oauth_clients SET metadata=? WHERE id=?", (json.dumps(metadata), row["id"])
            )
        return {"clients_invalidated": clients, "grants_revoked": grants}
    return {"clients_invalidated": 0, "grants_revoked": 0}


def _inspect_pending_configuration(store: Store) -> tuple[dict | None, bool]:
    """Select previous before DB commit and next after it, without publishing files."""
    pending = read_json(store.directory / "oauth-config-pending.json")
    if (
        type(pending.get("version")) is not int
        or pending["version"] != 1
        or not re.fullmatch(r"[0-9a-f]{32}", str(pending.get("id", "")))
        or "previous" not in pending
        or "next" not in pending
    ):
        raise StateError("OAuth configuration journal is invalid.")
    values = {}
    for key in ("previous", "next"):
        value = pending[key]
        if value is None and key == "previous":
            values[key] = None
        elif (
            isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 1
        ):
            values[key] = {"version": 1, "issuer": issuer_url(value.get("issuer"))}
        else:
            raise StateError("OAuth configuration journal is invalid.")
    with store.connection() as db:
        markers = db.execute("SELECT id FROM oauth_config_commits").fetchall()
        if len(markers) > 1:
            raise StateError("OAuth configuration commit marker is ambiguous.")
        committed = bool(markers and markers[0][0] == pending["id"])
    return values["next" if committed else "previous"], committed


def recover_configuration(store: Store) -> dict:
    """Publish a validated journal selection; the caller holds the installation lock."""
    store.ensure_writable()
    journal = store.directory / "oauth-config-pending.json"
    if not journal.exists():
        return {"state": "not_pending"}
    try:
        selected, committed = _inspect_pending_configuration(store)
    except (InputError, StateError, OSError, ValueError):
        raise StateError(
            "OAuth configuration journal is invalid; preserve it for inspection. "
            "Explicit replacement revokes all grants; run: "
            + _recovery_command(store, discard=True)
        ) from None
    path = store.directory / "oauth.json"
    if selected is None:
        path.unlink(missing_ok=True)
        sync_directory(store.directory)
    else:
        write_json(path, selected)
    if committed:
        onboarding_path(store).unlink(missing_ok=True)
    journal.unlink()
    sync_directory(store.directory)
    return {"state": "recovered", "committed": committed}


def _recover_configuration(store: Store) -> None:
    recover_configuration(store)


def configure(
    store: Store, issuer: str, *, replace_issuer: bool = False, discard_journal: bool = False
) -> dict:
    config = {"version": 1, "issuer": issuer_url(issuer)}
    store.ensure_writable()
    journal = store.directory / "oauth-config-pending.json"
    if discard_journal:
        if not replace_issuer:
            raise StateError(
                "Discarding a journal revokes all OAuth grants; also pass --replace-issuer."
            )
        # The prior issuer is unknowable in a broken journal. Explicit recovery
        # always revokes, even if the selected issuer matches the published file.
        previous = None
    else:
        _recover_configuration(store)
        previous = _read_configuration(store)
    if previous is not None and previous != config and not replace_issuer:
        raise StateError(
            "Replacing the issuer revokes existing grants; use --replace-issuer explicitly."
        )
    revoked = {"clients_invalidated": 0, "grants_revoked": 0}
    transition = uuid.uuid4().hex
    write_json(journal, {"version": 1, "id": transition, "previous": previous, "next": config})
    try:
        with store.connection(write=True) as db:
            store.ensure_writable()
            if previous != config:
                revoked = revoke_all(db)
                record_auth_change(store, db, "oauth_issuer_configured", details=revoked)
            # The DB marker selects logical state across a publication crash.
            # Publish first; rollback retains the previous grants and issuer.
            write_json(store.directory / "oauth.json", config)
            db.execute("DELETE FROM oauth_config_commits")
            db.execute("INSERT INTO oauth_config_commits(id) VALUES (?)", (transition,))
    except BaseException:
        # The journal remains if rollback publication also fails. A later
        # configure recovers using the committed transaction ID, never guesses.
        try:
            _recover_configuration(store)
        except Exception:
            pass
        raise
    _recover_configuration(store)
    return {
        "state": "configured",
        "issuer": config["issuer"],
        "mcp_url": config["issuer"] + "/mcp",
        "public_ingress": "not_verified",
        **revoked,
    }


class OAuthStore:
    def __init__(self, store: Store):
        self.store = store
        config = configuration(store)
        if config is None:
            raise InputError("Configure OAuth on the existing memory host first.")
        self.issuer = config["issuer"]
        self.resource = self.issuer + "/mcp"
        with store.connection():
            pass  # Validate the schema before accepting OAuth traffic.

    def enabled(self) -> bool:
        try:
            return not marker_path(self.store).exists() and configuration(self.store) == {
                "version": 1,
                "issuer": self.issuer,
            }
        except (InputError, StateError, OSError, ValueError):
            return False

    def require_enabled(self):
        if not self.enabled():
            raise InputError("OAuth is disabled or its issuer changed.")

    def onboarding_state(self) -> dict:
        """Return the operator-controlled registration window, defaulting closed."""
        path = onboarding_path(self.store)
        try:
            value = read_json(path) if path.exists() else {}
        except (InputError, OSError, ValueError):
            return {"state": "closed", "reason": "invalid_marker"}
        expires = value.get("expires_at")
        if (
            not isinstance(expires, (int, float))
            or isinstance(expires, bool)
            or not math.isfinite(expires)
        ):
            return {"state": "closed", "reason": "not_open"}
        if value.get("version") != 1:
            return {"state": "closed", "reason": "not_open"}
        now = time.time()
        if expires <= now:
            return {"state": "closed", "reason": "expired"}
        if expires > now + ONBOARDING_MAX_SECONDS:
            return {"state": "closed", "reason": "invalid_expiry"}
        return {"state": "open", "expires_at": expires}

    def set_onboarding(self, *, open_for_seconds: int | None) -> dict:
        self.require_enabled()
        path = onboarding_path(self.store)
        if open_for_seconds is None:
            path.unlink(missing_ok=True)
            return {"state": "closed"}
        if (
            not isinstance(open_for_seconds, int)
            or isinstance(open_for_seconds, bool)
            or not 1 <= open_for_seconds <= ONBOARDING_MAX_SECONDS
        ):
            raise InputError("Onboarding duration must be between 1 and 3600 seconds.")
        expires = time.time() + open_for_seconds
        write_json(path, {"version": 1, "expires_at": expires})
        return {"state": "open", "expires_at": expires}

    def require_onboarding(self) -> None:
        if self.onboarding_state()["state"] != "open":
            raise InputError(
                "OAuth onboarding is closed; an operator must open a temporary window."
            )

    def _cleanup(self, db) -> None:
        now = time.time()
        db.execute("DELETE FROM oauth_requests WHERE expires<=?", (now,))
        db.execute("DELETE FROM oauth_codes WHERE expires<=?", (now,))
        db.execute(
            "DELETE FROM oauth_tokens WHERE expires<=? OR grant_id IN "
            "(SELECT id FROM oauth_grants WHERE revoked=1 OR expires<=?)",
            (now, now),
        )
        # Grants and their provenance are retained until explicit revocation or
        # database maintenance. Never cascade them away merely to make room for
        # anonymous registrations.
        db.execute(
            "DELETE FROM oauth_clients WHERE created<? AND id NOT IN "
            "(SELECT client_id FROM oauth_grants UNION SELECT client_id FROM oauth_requests "
            "UNION SELECT client_id FROM oauth_codes)",
            (now - 900,),
        )

    @staticmethod
    def _safe_client_name(value: str | None) -> bool:
        return value is None or (
            len(value) <= 64
            and all(
                character.isprintable() and not unicodedata.category(character).startswith("C")
                for character in value
            )
        )

    def get_client(self, client_id):
        if not self.enabled():
            return None
        with self.store.connection() as db:
            row = db.execute(
                "SELECT metadata FROM oauth_clients WHERE id=? "
                "AND id NOT IN (SELECT client_id FROM oauth_client_revocations)",
                (client_id,),
            ).fetchone()
        return None if row is None else OAuthClientInformationFull.model_validate_json(row[0])

    def register_client(self, client):
        self.require_enabled()
        if client.token_endpoint_auth_method not in {"none", "client_secret_post"}:
            raise RegistrationError(
                "invalid_client_metadata", "Use none or client_secret_post authentication."
            )
        if self.onboarding_state()["state"] != "open":
            raise RegistrationError(
                "temporarily_unavailable",
                "OAuth onboarding is closed; try again after it is opened.",
            )
        for uri in client.redirect_uris:
            try:
                redirect_origin(str(uri))
            except InputError:
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Use a valid HTTPS or loopback callback; "
                    "retry client registration with that URI.",
                ) from None
        if len(client.model_dump_json()) > 8192 or len(client.redirect_uris) > 8:
            raise RegistrationError("invalid_client_metadata", "Client metadata exceeds limits.")
        if not self._safe_client_name(client.client_name):
            raise RegistrationError(
                "invalid_client_metadata", "Client name contains unsupported characters."
            )
        with self.store.connection(write=True) as db:
            self._cleanup(db)
            ungranted = db.execute(
                "SELECT COUNT(*) FROM oauth_clients WHERE id NOT IN "
                "(SELECT client_id FROM oauth_grants "
                "UNION SELECT client_id FROM oauth_client_revocations)"
            ).fetchone()[0]
            capacity_reached = ungranted >= MAX_CLIENTS
            if not capacity_reached:
                # Registration grants no access; retain until the short TTL.
                db.execute(
                    "INSERT INTO oauth_clients VALUES (?,?,?)",
                    (client.client_id, client.model_dump_json(), time.time()),
                )
        # SDK errors are frozen dataclasses; raise after the context manager,
        # which otherwise attempts to assign their traceback during rollback.
        if capacity_reached:
            raise RegistrationError(
                "temporarily_unavailable",
                "Registration capacity reached; wait 15 minutes or ask the operator to run: "
                "dots-brain oauth purge",
            )

    def authorize(self, client, params):
        self.require_enabled()
        if self.onboarding_state()["state"] != "open":
            raise AuthorizeError(
                "temporarily_unavailable",
                "OAuth onboarding is closed; try again after it is opened.",
            )
        if len(params.model_dump_json()) > 8192:
            raise AuthorizeError("invalid_request", "Authorization parameters exceed limits.")
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "The resource must be this server's MCP URL.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "A valid S256 PKCE challenge is required.")
        if not params.scopes or not set(params.scopes) <= SCOPES:
            raise AuthorizeError("invalid_scope", "Request explicit supported memory scopes.")
        capacity_reached = False
        with self.store.connection(write=True) as db:
            self._cleanup(db)
            live_clients = {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT client_id FROM ("
                    "SELECT client_id FROM oauth_grants WHERE revoked=0 AND expires>? "
                    "UNION SELECT client_id FROM oauth_requests WHERE expires>? "
                    "UNION SELECT client_id FROM oauth_codes WHERE expires>?"
                    ")",
                    (time.time(), time.time(), time.time()),
                )
            }
            if len(live_clients) >= MAX_CLIENTS and client.client_id not in live_clients:
                capacity_reached = True
            full = capacity_reached or (
                db.execute("SELECT COUNT(*) FROM oauth_requests WHERE status='pending'").fetchone()[
                    0
                ]
                >= MAX_PENDING
            )
            client_full = capacity_reached or (
                db.execute(
                    "SELECT COUNT(*) FROM oauth_requests WHERE client_id=? AND status='pending'",
                    (client.client_id,),
                ).fetchone()[0]
                >= MAX_PENDING_PER_CLIENT
            )
            if not full and not client_full:
                request_id = "req_" + secrets.token_urlsafe(24)
                created_at = time.time()
                db.execute(
                    "INSERT INTO oauth_requests VALUES (?,?,?,'pending',NULL,?)",
                    (request_id, client.client_id, params.model_dump_json(), created_at + 300),
                )
                db.execute(
                    "INSERT INTO oauth_request_provenance VALUES (?,?)",
                    (request_id, created_at),
                )
        if capacity_reached:
            raise AuthorizeError(
                "temporarily_unavailable", "Authorization client capacity reached."
            )
        if full or client_full:
            raise AuthorizeError(
                "temporarily_unavailable", "Pending authorization capacity reached."
            )
        return self.issuer + "/oauth/pair/" + request_id

    def pending(self, *, verbose: bool = False) -> dict:
        self.require_enabled()
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT r.*,c.metadata FROM oauth_requests r "
                "JOIN oauth_clients c ON c.id=r.client_id "
                "WHERE r.status IN ('pending','approved') AND r.expires>? ORDER BY r.expires,r.id",
                (time.time(),),
            ).fetchall()
        return {
            "requests": [
                {
                    "request_id": row["id"],
                    "client_id": row["client_id"],
                    **(
                        {"untrusted_client_name": json.loads(row["metadata"]).get("client_name")}
                        if verbose
                        else {}
                    ),
                    "state": row["status"],
                    "redirect_origin": redirect_origin(json.loads(row["params"])["redirect_uri"]),
                    "scopes": json.loads(row["params"])["scopes"],
                    "created_at": row["expires"] - 300,
                    "expires_at": row["expires"],
                }
                for row in rows
            ],
            "metadata_trust": "untrusted_client_supplied",
        }

    def clients(self) -> dict:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT c.id,c.created,c.metadata,r.revoked_at FROM oauth_clients c "
                "LEFT JOIN oauth_client_revocations r ON r.client_id=c.id "
                "ORDER BY c.created,c.id"
            ).fetchall()
        return {
            "clients": [
                {
                    "client_id": row["id"],
                    "created_at": row["created"],
                    "revoked_at": row["revoked_at"],
                    "untrusted_client_name": json.loads(row["metadata"]).get("client_name"),
                }
                for row in rows
            ]
        }

    def purge(self) -> dict:
        """Remove expired exchanges and ungranted clients, keeping all grant provenance."""
        self.require_enabled()
        with self.store.connection(write=True) as db:
            self._cleanup(db)
            # Preserve valid pending authorizations and issued codes. Removing
            # their clients cascades the exchange and silently breaks pairing.
            removed = db.execute(
                "DELETE FROM oauth_clients WHERE id NOT IN (SELECT client_id FROM oauth_grants "
                "UNION SELECT client_id FROM oauth_requests WHERE expires>? "
                "UNION SELECT client_id FROM oauth_codes WHERE expires>?)",
                (time.time(), time.time()),
            ).rowcount
            record_auth_change(
                self.store, db, "oauth_unused_clients_purged", details={"removed": removed}
            )
        return {"state": "purged", "unused_clients_removed": removed}

    def decide(
        self,
        request_id,
        *,
        projects=None,
        deny=False,
        allow_forget=False,
        scopes=None,
        redirect_host=None,
    ):
        self.require_enabled()
        if not deny and (
            not isinstance(redirect_host, str)
            or not redirect_host
            or redirect_host != redirect_host.strip()
        ):
            raise InputError(
                "Approval requires the expected redirect host; run dots-brain oauth approve "
                "REQUEST_ID --redirect-host HOST --project PROJECT."
            )
        expected_host = None
        if not deny:
            try:
                value = redirect_host
                if value.startswith("[") and value.endswith("]"):
                    value = str(ipaddress.IPv6Address(value[1:-1]))
                expected_host = _normalized_host(value)
            except (ValueError, UnicodeError):
                raise InputError(
                    "Approval requires a valid redirect host; run dots-brain oauth approve "
                    "REQUEST_ID --redirect-host HOST --project PROJECT."
                ) from None
        projects = normalize_projects(projects)
        if projects is not None:
            if not projects:
                raise InputError("Choose at least one project or explicitly allow all projects.")
            projects = list(projects)
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT r.*,a.scopes AS approved_scopes FROM oauth_requests r "
                "LEFT JOIN oauth_request_approvals a ON a.request_id=r.id "
                "WHERE r.id=? AND r.expires>?",
                (request_id, time.time()),
            ).fetchone()
            if row is None or row["status"] not in {"pending", "approved"}:
                raise InputError("This authorization request is unavailable or expired.")
            params = json.loads(row["params"])
            if (
                not deny
                and redirect_host is not None
                and urlsplit(redirect_origin(params["redirect_uri"])).hostname != expected_host
            ):
                raise InputError(
                    "The expected redirect host does not match this request; inspect it with "
                    "dots-brain oauth pending and retry dots-brain oauth approve REQUEST_ID "
                    "--redirect-host HOST --project PROJECT."
                )
            requested = params["scopes"]
            scopes = sorted(
                set(
                    scopes
                    if scopes is not None
                    else [
                        scope
                        for scope in requested
                        if scope in MEMORY_SCOPES and (scope != "memory:forget" or allow_forget)
                    ]
                )
            )
            if not deny and (not scopes or not set(scopes) <= set(requested)):
                raise InputError("Approve a nonempty subset of the requested scopes.")
            if not deny and "memory:forget" in scopes and not allow_forget:
                raise InputError(
                    "Deletion access requires the owner's explicit --allow-forget choice."
                )
            encoded = None if projects is None else json.dumps(projects)
            previous_scopes = (
                None if row["approved_scopes"] is None else json.loads(row["approved_scopes"])
            )
            if not deny and row["status"] == "approved":
                if row["projects"] != encoded or scopes != previous_scopes:
                    raise InputError("This request already has a different approval.")
            if deny:
                db.execute("DELETE FROM oauth_request_approvals WHERE request_id=?", (request_id,))
                db.execute(
                    "UPDATE oauth_requests SET status='denied',projects=NULL WHERE id=?",
                    (request_id,),
                )
            else:
                db.execute(
                    "INSERT INTO oauth_request_approvals VALUES (?,?) "
                    "ON CONFLICT(request_id) DO UPDATE SET scopes=excluded.scopes",
                    (request_id, json.dumps(scopes)),
                )
                db.execute(
                    "UPDATE oauth_requests SET status='approved',projects=? WHERE id=?",
                    (encoded, request_id),
                )
            record_auth_change(
                self.store,
                db,
                "oauth_denied" if deny else "oauth_approved",
                target=request_id,
                details={"scopes": [] if deny else scopes, "projects": projects},
            )
        return {
            "state": "denied" if deny else "approved",
            "request_id": request_id,
            "projects": projects,
            "scopes": [] if deny else scopes,
            "redirect_origin": redirect_origin(params["redirect_uri"]),
        }

    def pairing(self, request_id, *, bind_browser: bool = False, browser_nonce: str | None = None):
        """Inspect a pairing request without consuming it and bind its browser cookie."""
        self.require_enabled()
        self.require_onboarding()
        with self.store.connection() as db:
            existing = db.execute(
                "SELECT params FROM oauth_requests WHERE id=? AND expires>?",
                (request_id, time.time()),
            ).fetchone()
        if existing is None:
            return {"state": "expired"}
        needs_binding = bind_browser and not json.loads(existing[0]).get("pairing_nonce_hash")
        with self.store.connection(write=needs_binding) as db:
            row = db.execute(
                "SELECT r.*,c.metadata,a.scopes AS approved_scopes FROM oauth_requests r "
                "JOIN oauth_clients c ON c.id=r.client_id "
                "LEFT JOIN oauth_request_approvals a ON a.request_id=r.id "
                "WHERE r.id=? AND r.expires>?",
                (request_id, time.time()),
            ).fetchone()
            if row is None:
                return {"state": "expired"}
            if row["status"] not in {"pending", "approved", "denied"}:
                return {"state": "expired"}
            params = json.loads(row["params"])
            issued_nonce = None
            if bind_browser and not params.get("pairing_nonce_hash"):
                issued_nonce = secrets.token_urlsafe(32)
                params["pairing_nonce_hash"] = digest(issued_nonce)
                db.execute(
                    "UPDATE oauth_requests SET params=? WHERE id=?",
                    (json.dumps(params), request_id),
                )
            return {
                "state": row["status"],
                "request_id": request_id,
                "browser_nonce": issued_nonce,
                "browser_bound": isinstance(browser_nonce, str)
                and digest(browser_nonce) == params.get("pairing_nonce_hash"),
                "untrusted_client_name": json.loads(row["metadata"]).get("client_name")
                or "Unnamed client",
                "redirect_origin": redirect_origin(params["redirect_uri"]),
                "requested_scopes": params["scopes"],
                "approved_scopes": None
                if row["approved_scopes"] is None
                else json.loads(row["approved_scopes"]),
                "expires_at": row["expires"],
            }

    def claim(self, request_id, browser_nonce: str | None = None, *, require_browser: bool = False):
        self.require_enabled()
        self.require_onboarding()
        with self.store.connection() as db:
            check = db.execute(
                "SELECT status,params FROM oauth_requests WHERE id=? AND expires>?",
                (request_id, time.time()),
            ).fetchone()
        if check is None or check["status"] not in {"pending", "approved", "denied"}:
            return {"state": "expired"}
        if require_browser and (
            not isinstance(browser_nonce, str)
            or digest(browser_nonce) != json.loads(check["params"]).get("pairing_nonce_hash")
        ):
            return {"state": "browser_mismatch", "request_id": request_id}
        if check["status"] == "pending":
            return {"state": "pending", "request_id": request_id}
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT r.*,p.request_created_at,a.scopes AS approved_scopes FROM oauth_requests r "
                "LEFT JOIN oauth_request_provenance p ON p.request_id=r.id "
                "LEFT JOIN oauth_request_approvals a ON a.request_id=r.id "
                "WHERE r.id=? AND r.expires>?",
                (request_id, time.time()),
            ).fetchone()
            if row is None:
                return {"state": "expired"}
            if row["status"] not in {"pending", "approved", "denied"}:
                return {"state": "expired"}
            raw_params = json.loads(row["params"])
            if require_browser and (
                not isinstance(browser_nonce, str)
                or digest(browser_nonce) != raw_params.get("pairing_nonce_hash")
            ):
                return {"state": "browser_mismatch", "request_id": request_id}
            params = AuthorizationParams.model_validate(raw_params)
            if row["status"] == "pending":
                return {"state": "pending", "request_id": request_id}
            db.execute("DELETE FROM oauth_requests WHERE id=?", (request_id,))
            if row["status"] == "denied":
                return {
                    "state": "redirect",
                    "url": construct_redirect_uri(
                        str(params.redirect_uri), error="access_denied", state=params.state
                    ),
                }
            if row["approved_scopes"] is None:
                return {"state": "expired"}
            approved_params = params.model_copy(
                update={"scopes": json.loads(row["approved_scopes"])}
            )
            code = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO oauth_codes VALUES (?,?,?,?,?)",
                (
                    digest(code),
                    row["client_id"],
                    approved_params.model_dump_json(),
                    row["projects"],
                    time.time() + 60,
                ),
            )
            db.execute(
                "INSERT INTO oauth_code_provenance VALUES (?,?,?)",
                (digest(code), request_id, row["request_created_at"]),
            )
        return {
            "state": "redirect",
            "url": construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
        }

    def load_code(self, client, code):
        if not self.enabled() or len(code) > 1024:
            return None
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM oauth_codes WHERE hash=? AND client_id=? AND expires>?",
                (digest(code), client.client_id, time.time()),
            ).fetchone()
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row["params"])
        return AuthorizationCode(
            code=code,
            expires_at=row["expires"],
            client_id=client.client_id,
            subject="owner",
            **params.model_dump(exclude={"state"}),
        )

    def mint(self, db, grant_id, scopes, expires, *, refresh_scopes=None):
        refresh_scopes = scopes if refresh_scopes is None else refresh_scopes
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        lifetime = min(3600, max(0, int(expires - time.time())))
        db.executemany(
            "INSERT INTO oauth_tokens VALUES (?,?,?,?,?)",
            [
                (digest(access), "access", grant_id, json.dumps(scopes), time.time() + lifetime),
                (digest(refresh), "refresh", grant_id, json.dumps(refresh_scopes), expires),
            ],
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=lifetime,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    def exchange_code(self, client, code):
        self.require_enabled()
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT c.*,p.request_id,p.request_created_at FROM oauth_codes c "
                "LEFT JOIN oauth_code_provenance p ON p.code_hash=c.hash "
                "WHERE c.hash=? AND c.client_id=? AND c.expires>?",
                (digest(code.code), client.client_id, time.time()),
            ).fetchone()
            if row is not None:
                # Copy provenance before cascading code deletion. BEGIN IMMEDIATE
                # keeps code consumption, grant and token creation atomic.
                db.execute("DELETE FROM oauth_codes WHERE hash=?", (row["hash"],))
                params = AuthorizationParams.model_validate_json(row["params"])
                grant_id, expires = "grant_" + secrets.token_urlsafe(24), time.time() + 30 * 86400
                db.execute(
                    "INSERT INTO oauth_grants VALUES (?,?,?,?,?,?,0)",
                    (
                        grant_id,
                        client.client_id,
                        json.dumps(params.scopes),
                        row["projects"],
                        self.resource,
                        expires,
                    ),
                )
                db.execute(
                    "INSERT INTO oauth_grant_provenance VALUES (?,?,?,?)",
                    (grant_id, row["request_id"], row["request_created_at"], time.time()),
                )
                record_auth_change(
                    self.store,
                    db,
                    "oauth_grant_issued",
                    target=grant_id,
                    details={"client_id": client.client_id},
                )
                return self.mint(db, grant_id, params.scopes, expires)
        raise TokenError("invalid_grant", "Authorization code is expired or already used.")

    def token_row(self, db, token, kind):
        return db.execute(
            "SELECT t.*,g.client_id,g.projects,g.resource,g.scopes AS grant_scopes "
            "FROM oauth_tokens t JOIN oauth_grants g "
            "ON g.id=t.grant_id WHERE t.hash=? AND t.kind=? AND t.expires>? AND g.expires>? "
            "AND g.revoked=0 AND g.resource=?",
            (digest(token), kind, time.time(), time.time(), self.resource),
        ).fetchone()

    def load_token(self, token, kind, client_id=None):
        if not self.enabled() or len(token) > 1024:
            return None
        with self.store.connection() as db:
            spent = self.token_row(db, token, "spent_refresh") if kind == "refresh" else None
            row = self.token_row(db, token, kind)
        if spent is not None and spent["client_id"] == client_id:
            with self.store.connection(write=True) as db:
                self.revoke_refresh_replay(db, token, client_id)
            return None
        if row is None or (client_id is not None and client_id != row["client_id"]):
            return None
        model = AccessToken if kind == "access" else RefreshToken
        return model(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=int(row["expires"]),
            resource=row["resource"],
            subject="owner",
        )

    def revoke_refresh_replay(self, db, token, client_id):
        """Retain spent hashes until grant expiry and revoke their family on reuse."""
        row = self.token_row(db, token, "spent_refresh")
        if row is None or row["client_id"] != client_id:
            return False
        db.execute("UPDATE oauth_grants SET revoked=1 WHERE id=?", (row["grant_id"],))
        record_auth_change(
            self.store,
            db,
            "oauth_grant_revoked",
            target=row["grant_id"],
            details={"reason": "refresh_reuse"},
        )
        return True

    def exchange_refresh(self, client, token, scopes):
        self.require_enabled()
        with self.store.connection(write=True) as db:
            # Recheck inside the write transaction: two requests can load the
            # same still-valid token before either reaches this exchange.
            replayed = self.revoke_refresh_replay(db, token.token, client.client_id)
            row = None if replayed else self.token_row(db, token.token, "refresh")
            if (
                row is not None
                and row["client_id"] == client.client_id
                and set(scopes) <= set(json.loads(row["grant_scopes"]))
            ):
                rotations = db.execute(
                    "SELECT COUNT(*) FROM oauth_tokens WHERE grant_id=? AND kind='spent_refresh'",
                    (row["grant_id"],),
                ).fetchone()[0]
                if rotations >= MAX_REFRESH_ROTATIONS:
                    db.execute("UPDATE oauth_grants SET revoked=1 WHERE id=?", (row["grant_id"],))
                    record_auth_change(
                        self.store,
                        db,
                        "oauth_grant_revoked",
                        target=row["grant_id"],
                        details={"reason": "rotation_limit"},
                    )
                else:
                    db.execute(
                        "UPDATE oauth_tokens SET kind='spent_refresh' WHERE hash=?",
                        (digest(token.token),),
                    )
                    db.execute(
                        "DELETE FROM oauth_tokens WHERE grant_id=? AND kind='access'",
                        (row["grant_id"],),
                    )
                    return self.mint(
                        db,
                        row["grant_id"],
                        scopes,
                        row["expires"],
                        refresh_scopes=json.loads(row["grant_scopes"]),
                    )
        raise TokenError("invalid_grant", "Refresh token is expired, invalid, or already used.")

    def revoke_token(self, token):
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT grant_id FROM oauth_tokens WHERE hash=?", (digest(token.token),)
            ).fetchone()
            db.execute(
                "UPDATE oauth_grants SET revoked=1 WHERE id IN "
                "(SELECT grant_id FROM oauth_tokens WHERE hash=?)",
                (digest(token.token),),
            )
            if row:
                record_auth_change(
                    self.store,
                    db,
                    "oauth_grant_revoked",
                    target=row["grant_id"],
                    details={"reason": "client_request"},
                )

    def policy(self, token):
        if not self.enabled() or len(token) > 1024:
            return None
        with self.store.connection() as db:
            row = self.token_row(db, token, "access")
        if row is None:
            return None
        return Policy(
            frozenset(json.loads(row["scopes"])),
            None if row["projects"] is None else tuple(json.loads(row["projects"])),
            f"oauth-grant:{row['grant_id']}",
        )

    def grants(self):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT g.id,g.client_id,g.scopes,g.projects,g.expires,g.revoked,"
                "p.request_id,p.request_created_at,p.created_at,c.metadata FROM oauth_grants g "
                "LEFT JOIN oauth_grant_provenance p ON p.grant_id=g.id "
                "JOIN oauth_clients c ON c.id=g.client_id ORDER BY p.created_at,g.id"
            ).fetchall()
        return {
            "grants": [
                {
                    **{key: row[key] for key in row.keys() if key != "metadata"},
                    "untrusted_client_name": json.loads(row["metadata"]).get("client_name"),
                    "scopes": json.loads(row["scopes"]),
                    "projects": None if row["projects"] is None else json.loads(row["projects"]),
                }
                for row in rows
            ]
        }

    def revoke_grant(self, grant_id):
        with self.store.connection(write=True) as db:
            changed = db.execute(
                "UPDATE oauth_grants SET revoked=1 WHERE id=?", (grant_id,)
            ).rowcount
            if not changed:
                raise InputError("This OAuth grant does not exist.")
            record_auth_change(
                self.store,
                db,
                "oauth_grant_revoked",
                target=grant_id,
                details={"reason": "owner_request"},
            )
        return {"grant_id": grant_id, "revoked": True}


def _normalized_host(host: str) -> str:
    if ":" in host:
        if "%" in host:
            raise ValueError("IPv6 zone identifiers are unsupported.")
        return str(ipaddress.IPv6Address(host))
    host = host.encode("idna").decode("ascii").lower()
    if len(host) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.rstrip(".").split(".")
    ):
        raise ValueError("Invalid hostname.")
    return host


def redirect_origin(uri: str) -> str:
    """Validate a callback and serialize a safe origin, including bracketed IPv6."""
    recovery = "Retry client registration with a valid HTTPS or loopback callback URI."
    if not isinstance(uri, str) or any(
        char.isspace() or unicodedata.category(char).startswith("C") for char in uri
    ):
        raise InputError(recovery)
    try:
        parts = urlsplit(uri)
        host, port = parts.hostname, parts.port
        if (
            not host
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or "\\" in uri
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
        host = _normalized_host(host)
        if parts.scheme != "https" and not (
            parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError
        rendered = f"[{host}]" if ":" in host else host
        suffix = (
            "" if port is None or port == (443 if parts.scheme == "https" else 80) else f":{port}"
        )
        return f"{parts.scheme}://{rendered}{suffix}"
    except (ValueError, UnicodeError):
        raise InputError(recovery) from None


class OAuthProvider:
    """SDK interface; blocking SQLite work runs outside the HTTP event loop."""

    def __init__(self, state: OAuthStore):
        self.state = state
        self.limiter = anyio.CapacityLimiter(4)

    async def run(self, function, *args, **kwargs):
        if kwargs:
            function = partial(function, *args, **kwargs)
            args = ()
        return await anyio.to_thread.run_sync(function, *args, limiter=self.limiter)

    async def get_client(self, client_id):
        return await self.run(self.state.get_client, client_id)

    async def register_client(self, client_info):
        return await self.run(self.state.register_client, client_info)

    async def authorize(self, client, params):
        return await self.run(self.state.authorize, client, params)

    async def load_authorization_code(self, client, authorization_code):
        return await self.run(self.state.load_code, client, authorization_code)

    async def exchange_authorization_code(self, client, authorization_code):
        return await self.run(self.state.exchange_code, client, authorization_code)

    async def load_refresh_token(self, client, refresh_token):
        return await self.run(self.state.load_token, refresh_token, "refresh", client.client_id)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return await self.run(self.state.exchange_refresh, client, refresh_token, scopes)

    async def load_access_token(self, token):
        return await self.run(self.state.load_token, token, "access")

    async def revoke_token(self, token):
        return await self.run(self.state.revoke_token, token)
