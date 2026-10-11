"""VM-local OAuth persistence and owner authorization for the official MCP SDK."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
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

from .auth import MEMORY_SCOPES, SCOPES, Policy
from .errors import InputError
from .local import read_json, sync_file_and_parent, write_json, writer_lock
from .store import Store, validate_text

MAX_CLIENTS = 256
MAX_PENDING = 128
MAX_REFRESH_ROTATIONS = 4096
RETIRED = "_dots_brain_retired"

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_requests (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE,
    params TEXT NOT NULL, status TEXT NOT NULL, projects TEXT, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    hash TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE,
    params TEXT NOT NULL, projects TEXT, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_grants (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE,
    scopes TEXT NOT NULL, projects TEXT, resource TEXT NOT NULL, expires REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    hash TEXT PRIMARY KEY, kind TEXT NOT NULL,
    grant_id TEXT NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
    scopes TEXT NOT NULL, expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_tokens_grant ON oauth_tokens(grant_id);
CREATE TABLE IF NOT EXISTS oauth_request_provenance (
    request_id TEXT PRIMARY KEY REFERENCES oauth_requests(id) ON DELETE CASCADE,
    request_created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_code_provenance (
    code_hash TEXT PRIMARY KEY REFERENCES oauth_codes(hash) ON DELETE CASCADE,
    request_id TEXT NOT NULL, request_created_at REAL
);
CREATE TABLE IF NOT EXISTS oauth_grant_provenance (
    grant_id TEXT PRIMARY KEY REFERENCES oauth_grants(id) ON DELETE CASCADE,
    request_id TEXT, request_created_at REAL, created_at REAL NOT NULL
);
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issuer_url(value: str) -> str:
    try:
        return _issuer_url(value)
    except (TypeError, AttributeError, ValueError):
        raise InputError("OAuth issuer has an invalid host or port.") from None


def _issuer_url(value: str) -> str:
    parts = urlsplit(value)
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise InputError("OAuth issuer must be an HTTPS origin.")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise InputError("OAuth issuer has an invalid port.")
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
    return str(AnyHttpUrl(value)).rstrip("/")


def configuration(store: Store) -> dict | None:
    path = store.directory / "oauth.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        config = read_json(path)
    except (OSError, ValueError):
        raise InputError("Cannot read valid OAuth configuration; inspect oauth status.") from None
    if type(config.get("version")) is not int or config["version"] != 1:
        raise InputError("Unsupported OAuth configuration.")
    return {"version": 1, "issuer": issuer_url(config.get("issuer"))}


def record_auth_change(store, db, action, *, target=None, details=None, event_id=None):
    from .activity import AuditLog

    return AuditLog(store).observed_in_connection(
        db,
        Policy(frozenset({"audit:write"}), ("system:auth",), "server:oauth"),
        project="system:auth",
        kind="action",
        client_event_id=event_id or uuid.uuid4().hex,
        action=action,
        target=target,
        details=details,
    )


def revoke_all(store, db, *, reason, event_id=None) -> dict:
    counts = {"clients_invalidated": 0, "grants_revoked": 0}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='oauth_clients'").fetchone():
        counts["clients_invalidated"] = retire_clients(
            db, db.execute("SELECT id,metadata FROM oauth_clients").fetchall()
        )
        counts["grants_revoked"] = db.execute(
            "UPDATE oauth_grants SET revoked=1 WHERE revoked=0"
        ).rowcount
        for table in ("oauth_tokens", "oauth_codes", "oauth_requests"):
            db.execute(f"DELETE FROM {table}")
        record_auth_change(
            store,
            db,
            "oauth_access_invalidated",
            details={"reason": reason, **counts},
            event_id=event_id,
        )
    return counts


def retire_clients(db, rows) -> int:
    count = 0
    for row in rows:
        metadata = json.loads(row["metadata"])
        count += RETIRED not in metadata
        metadata[RETIRED] = True
        metadata.pop("client_secret", None)
        db.execute(
            "UPDATE oauth_clients SET metadata=? WHERE id=?", (json.dumps(metadata), row["id"])
        )
    return count


def configure(store: Store, issuer: str, *, replace_issuer=False) -> dict:
    issuer = issuer_url(issuer)
    with writer_lock(store.directory, exclusive=True):
        return _configure_offline(store, issuer, replace_issuer=replace_issuer)


def _configure_offline(store: Store, issuer: str, *, replace_issuer) -> dict:
    config = {"version": 1, "issuer": issuer_url(issuer)}
    try:
        previous = configuration(store)
    except InputError:
        if not replace_issuer:
            raise InputError(
                "Invalid OAuth configuration; inspect it and use --replace-issuer explicitly."
            ) from None
        previous = {"invalid": True}
    if previous and previous != config and not replace_issuer:
        raise InputError(
            "Replacing the issuer revokes all OAuth access; use --replace-issuer explicitly."
        )
    invalidated = {"clients_invalidated": 0, "grants_revoked": 0}
    with store.connection(write=True) as db:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        if previous != config:
            invalidated = revoke_all(store, db, reason="issuer_changed")
    try:
        write_json(store.directory / "oauth.json", config)
    except (OSError, InputError):
        raise InputError(
            "OAuth database update committed; old access stays invalidated if the issuer changed. "
            "Configuration publication is uncertain; inspect oauth status, "
            "retry configuration and pair again."
        ) from None
    return {
        "state": "configured",
        "issuer": config["issuer"],
        "mcp_url": config["issuer"] + "/mcp",
        "public_ingress": "not_verified",
        "restart_required": True,
        **invalidated,
    }


def disable(store: Store) -> dict:
    with writer_lock(store.directory, exclusive=True):
        with store.connection(write=True) as db:
            counts = revoke_all(store, db, reason="oauth_disabled")
        try:
            (store.directory / "oauth.json").unlink(missing_ok=True)
            sync_file_and_parent(store.path)
        except OSError:
            raise InputError(
                "OAuth access was invalidated; configuration removal is uncertain. "
                "Inspect oauth status and retry disable."
            ) from None
    return {
        "state": "oauth_disabled",
        "grants_revoked": True,
        "invalidated": counts,
        "restart_required": True,
        "service": None,
    }


class OAuthStore:
    def __init__(self, store: Store):
        self.store = store
        config = configuration(store)
        if config is None:
            raise InputError("Configure OAuth on the existing memory host first.")
        self.issuer = config["issuer"]
        self.resource = self.issuer + "/mcp"
        # Upgrade only through explicit configuration. Companion tables preserve
        # the old INSERT layouts so reverting the binary does not break auth.
        with store.connection() as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        if (
            not {"oauth_request_provenance", "oauth_code_provenance", "oauth_grant_provenance"}
            <= tables
        ):
            raise InputError(
                "OAuth provenance upgrade required. Stop the service, back up, and run "
                "oauth configure with the existing issuer and --no-start before restarting."
            )

    def enabled(self) -> bool:
        try:
            return self._enabled()
        except InputError:
            return False

    def _enabled(self) -> bool:
        marker = self.store.directory / "disabled.json"
        return not (marker.exists() or marker.is_symlink()) and configuration(self.store) == {
            "version": 1,
            "issuer": self.issuer,
        }

    def require_enabled(self):
        if not self.enabled():
            raise InputError("OAuth is disabled or its issuer changed.")

    def get_client(self, client_id):
        if not self.enabled():
            return None
        with self.store.connection() as db:
            row = db.execute(
                "SELECT metadata FROM oauth_clients WHERE id=?", (client_id,)
            ).fetchone()
        if row is None or RETIRED in json.loads(row[0]):
            return None
        return OAuthClientInformationFull.model_validate_json(row[0])

    def register_client(self, client):
        self.require_enabled()
        if RETIRED in client.model_dump():
            raise RegistrationError("invalid_client_metadata", "Reserved client metadata.")
        if len(client.model_dump_json()) > 8192 or len(client.redirect_uris) > 8:
            raise RegistrationError("invalid_client_metadata", "Client metadata exceeds limits.")
        if len(client.client_name or "") > 200:
            raise RegistrationError("invalid_client_metadata", "Client name exceeds limits.")
        for uri in client.redirect_uris:
            parts = urlsplit(str(uri))
            if (
                parts.username
                or parts.password
                or parts.fragment
                or not parts.hostname
                or (
                    parts.scheme != "https"
                    and not (
                        parts.scheme == "http"
                        and parts.hostname in {"localhost", "127.0.0.1", "::1"}
                    )
                )
            ):
                raise RegistrationError("invalid_redirect_uri", "Use HTTPS or a loopback callback.")
        with self.store.connection(write=True) as db:
            db.execute("DELETE FROM oauth_requests WHERE expires<=?", (time.time(),))
            db.execute("DELETE FROM oauth_codes WHERE expires<=?", (time.time(),))
            db.execute(
                "DELETE FROM oauth_tokens WHERE grant_id IN "
                "(SELECT id FROM oauth_grants WHERE revoked=1 OR expires<=?)",
                (time.time(),),
            )
            retire_clients(
                db,
                db.execute(
                    "SELECT id,metadata FROM oauth_clients WHERE created<? AND id NOT IN "
                    "(SELECT client_id FROM oauth_grants WHERE revoked=0 AND expires>? "
                    "UNION SELECT client_id FROM oauth_requests "
                    "UNION SELECT client_id FROM oauth_codes)",
                    (time.time() - 86400, time.time()),
                ).fetchall(),
            )
            full = (
                db.execute(
                    "SELECT COUNT(*) FROM oauth_clients WHERE json_type(metadata,?) IS NULL",
                    ("$." + RETIRED,),
                ).fetchone()[0]
                >= MAX_CLIENTS
            )
            if not full:
                db.execute(
                    "INSERT INTO oauth_clients VALUES (?,?,?)",
                    (client.client_id, client.model_dump_json(), time.time()),
                )
        # SDK error dataclasses are frozen: raise outside generator context managers.
        if full:
            raise RegistrationError("invalid_client_metadata", "Registration capacity reached.")

    def authorize(self, client, params):
        self.require_enabled()
        if self.get_client(client.client_id) is None:
            raise AuthorizeError("unauthorized_client", "Client registration is unavailable.")
        if len(params.model_dump_json()) > 8192:
            raise AuthorizeError("invalid_request", "Authorization parameters exceed limits.")
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "The resource must be this server's MCP URL.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "A valid S256 PKCE challenge is required.")
        if not params.scopes or not set(params.scopes) <= SCOPES:
            raise AuthorizeError("invalid_scope", "Request explicit supported memory scopes.")
        with self.store.connection(write=True) as db:
            db.execute("DELETE FROM oauth_requests WHERE expires<=?", (time.time(),))
            full = db.execute("SELECT COUNT(*) FROM oauth_requests").fetchone()[0] >= MAX_PENDING
            if not full:
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
        if full:
            raise AuthorizeError(
                "temporarily_unavailable", "Pending authorization capacity reached."
            )
        return self.issuer + "/oauth/pair/" + request_id

    def pending(self) -> dict:
        self.require_enabled()
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT r.*,c.metadata FROM oauth_requests r "
                "JOIN oauth_clients c ON c.id=r.client_id "
                "WHERE r.status='pending' AND r.expires>? ORDER BY r.expires",
                (time.time(),),
            ).fetchall()
        return {
            "requests": [
                {
                    "request_id": row["id"],
                    "client_id": row["client_id"],
                    "client_name": json.loads(row["metadata"]).get("client_name"),
                    "redirect_uri": json.loads(row["params"])["redirect_uri"],
                    "scopes": json.loads(row["params"])["scopes"],
                    "expires_at": row["expires"],
                }
                for row in rows
            ],
            "metadata_trust": "untrusted_client_supplied",
        }

    def decide(self, request_id, *, projects=None, deny=False, allow_forget=False, scopes=None):
        self.require_enabled()
        if projects is not None:
            if not projects:
                raise InputError("Choose at least one project or explicitly allow all projects.")
            for project in projects:
                validate_text(project, "project", 200)
            projects = sorted(set(projects))
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM oauth_requests WHERE id=? AND expires>?", (request_id, time.time())
            ).fetchone()
            if row is None:
                raise InputError("This authorization request is unavailable or expired.")
            params = json.loads(row["params"])
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
                raise InputError("Approve a nonempty subset of the requested memory scopes.")
            if not deny and "memory:forget" in scopes and not allow_forget:
                raise InputError(
                    "Deletion access requires the owner's explicit --allow-forget choice."
                )
            encoded = None if projects is None else json.dumps(projects)
            status = "denied" if deny else "approved"
            if row["status"] != "pending" and (
                row["status"] != status
                or (not deny and (row["projects"] != encoded or scopes != requested))
            ):
                raise InputError("This request already has a different final decision.")
            params["scopes"] = scopes
            db.execute(
                "UPDATE oauth_requests SET status=?,projects=?,params=? WHERE id=?",
                ("denied" if deny else "approved", encoded, json.dumps(params), request_id),
            )
        return {
            "state": "denied" if deny else "approved",
            "request_id": request_id,
            "projects": projects,
            "scopes": scopes,
        }

    def claim(self, request_id):
        self.require_enabled()
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT r.*,p.request_created_at FROM oauth_requests r "
                "LEFT JOIN oauth_request_provenance p ON p.request_id=r.id "
                "WHERE r.id=? AND r.expires>?",
                (request_id, time.time()),
            ).fetchone()
            if row is None:
                return {"state": "expired"}
            params = AuthorizationParams.model_validate_json(row["params"])
            if row["status"] == "pending":
                client = self.get_client(row["client_id"])
                if client is None:
                    return {"state": "expired"}
                return {
                    "state": "pending",
                    "request_id": request_id,
                    "client_name": client.client_name or "Unnamed client",
                    "redirect_uri": str(params.redirect_uri),
                    "scopes": params.scopes,
                    "expires_at": row["expires"],
                }
            db.execute("DELETE FROM oauth_requests WHERE id=?", (request_id,))
            if row["status"] == "denied":
                return {
                    "state": "redirect",
                    "url": construct_redirect_uri(
                        str(params.redirect_uri), error="access_denied", state=params.state
                    ),
                }
            code = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO oauth_codes VALUES (?,?,?,?,?)",
                (digest(code), row["client_id"], row["params"], row["projects"], time.time() + 60),
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
        if not self.enabled() or len(code) > 1024 or self.get_client(client.client_id) is None:
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

    def mint(self, db, grant_id, scopes, expires):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        lifetime = min(3600, max(0, int(expires - time.time())))
        db.executemany(
            "INSERT INTO oauth_tokens VALUES (?,?,?,?,?)",
            [
                (digest(access), "access", grant_id, json.dumps(scopes), time.time() + lifetime),
                (digest(refresh), "refresh", grant_id, json.dumps(scopes), expires),
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
        if self.get_client(client.client_id) is None:
            raise TokenError("invalid_client", "Client registration is unavailable.")
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
                return self.mint(db, grant_id, params.scopes, expires)
        raise TokenError("invalid_grant", "Authorization code is expired or already used.")

    def token_row(self, db, token, kind):
        return db.execute(
            "SELECT t.*,g.client_id,g.projects,g.resource FROM oauth_tokens t JOIN oauth_grants g "
            "ON g.id=t.grant_id WHERE t.hash=? AND t.kind=? AND t.expires>? AND g.expires>? "
            "AND g.revoked=0 AND g.resource=? AND g.client_id IN "
            "(SELECT id FROM oauth_clients WHERE json_type(metadata,?) IS NULL)",
            (digest(token), kind, time.time(), time.time(), self.resource, "$." + RETIRED),
        ).fetchone()

    def load_token(self, token, kind, client_id=None):
        if not self.enabled() or len(token) > 1024:
            return None
        with self.store.connection(write=kind == "refresh") as db:
            replayed = kind == "refresh" and self.revoke_refresh_replay(db, token, client_id)
            row = None if replayed else self.token_row(db, token, kind)
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
        self._revoke_grant(db, row["grant_id"], "refresh_replay")
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
                and set(scopes) <= set(json.loads(row["scopes"]))
            ):
                rotations = db.execute(
                    "SELECT COUNT(*) FROM oauth_tokens WHERE grant_id=? AND kind='spent_refresh'",
                    (row["grant_id"],),
                ).fetchone()[0]
                if rotations >= MAX_REFRESH_ROTATIONS:
                    self._revoke_grant(db, row["grant_id"], "refresh_rotation_limit")
                else:
                    db.execute(
                        "UPDATE oauth_tokens SET kind='spent_refresh' WHERE hash=?",
                        (digest(token.token),),
                    )
                    db.execute(
                        "DELETE FROM oauth_tokens WHERE grant_id=? AND kind='access'",
                        (row["grant_id"],),
                    )
                    return self.mint(db, row["grant_id"], scopes, row["expires"])
        raise TokenError("invalid_grant", "Refresh token is expired, invalid, or already used.")

    def revoke_token(self, token):
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT grant_id FROM oauth_tokens WHERE hash=?", (digest(token.token),)
            ).fetchone()
            if row is not None:
                self._revoke_grant(db, row["grant_id"], "token_revoked")

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
                "p.request_id,p.request_created_at,p.created_at FROM oauth_grants g "
                "LEFT JOIN oauth_grant_provenance p ON p.grant_id=g.id"
            ).fetchall()
        return {
            "grants": [
                {
                    **dict(row),
                    "scopes": json.loads(row["scopes"]),
                    "projects": None if row["projects"] is None else json.loads(row["projects"]),
                }
                for row in rows
            ]
        }

    def revoke_grant(self, grant_id):
        with self.store.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM oauth_grants WHERE id=?", (grant_id,)).fetchone():
                raise InputError("This OAuth grant does not exist.")
            self._revoke_grant(db, grant_id, "owner_revoked")
        return {"grant_id": grant_id, "revoked": True}

    def _revoke_grant(self, db, grant_id, reason):
        changed = db.execute(
            "UPDATE oauth_grants SET revoked=1 WHERE id=? AND revoked=0", (grant_id,)
        ).rowcount
        db.execute("DELETE FROM oauth_tokens WHERE grant_id=?", (grant_id,))
        if changed:
            record_auth_change(
                self.store, db, "oauth_grant_revoked", target=grant_id, details={"reason": reason}
            )


class OAuthProvider:
    """SDK interface; blocking SQLite work runs outside the HTTP event loop."""

    def __init__(self, state: OAuthStore):
        self.state = state
        self.limiter = anyio.CapacityLimiter(4)

    async def run(self, function, *args):
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
