"""Scoped local credentials. This module is not an OAuth server or a secret vault."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import anyio

from .errors import InputError
from .store import Store, validate_text

SCOPES = frozenset({"memory:read", "memory:write", "memory:forget"})


@dataclass(frozen=True)
class Policy:
    scopes: frozenset[str] = SCOPES
    projects: tuple[str, ...] | None = None

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise InputError(f"This client does not have the required {scope} scope.")


def validate_endpoint(url: str) -> str:
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise InputError("The MCP endpoint must not contain credentials, a query, or a fragment.")
    if parts.path != "/mcp" or not parts.hostname:
        raise InputError("Expected an absolute MCP endpoint ending in /mcp.")
    local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise InputError("HTTPS is required except for a loopback endpoint.")
    return url


def issue_client(
    store: Store,
    *,
    name: str,
    scopes: list[str],
    projects: list[str] | None,
    days: int,
    output: Path,
    url: str,
) -> dict:
    validate_text(name, "name", 200)
    if not scopes or not set(scopes) <= SCOPES:
        raise InputError("Choose memory:read, memory:write, or memory:forget scopes.")
    if not 1 <= days <= 365:
        raise InputError("Credential lifetime must be between 1 and 365 days.")
    if projects is not None:
        for project in projects:
            validate_text(project, "project", 200)
    validate_endpoint(url)
    client_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
    expires = time.time() + days * 86400
    payload = {"version": 1, "client_id": client_id, "url": url, "token": token}
    output = output.expanduser().absolute()
    if not output.parent.is_dir():
        raise InputError("The credential output directory must already exist.")
    # O_EXCL prevents overwriting another client's credential or following a symlink.
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        with store.connection(write=True) as db:
            db.execute(
                "INSERT INTO clients VALUES (?,?,?,?,?,?,0)",
                (
                    client_id,
                    name,
                    hashlib.sha256(token.encode()).hexdigest(),
                    json.dumps(sorted(set(scopes))),
                    None if projects is None else json.dumps(sorted(set(projects))),
                    expires,
                ),
            )
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {
        "client_id": client_id,
        "credential_file": str(output),
        "scopes": sorted(set(scopes)),
        "projects": projects,
        "expires_at": expires,
        "state": "configured_pending_verification",
        "secret_isolation": False,
    }


def authenticate(store: Store, token: str) -> Policy | None:
    if (store.directory / "disabled.json").exists():
        return None
    if len(token) > 1024:
        return None
    with store.connection() as db:
        row = db.execute(
            "SELECT scopes,projects FROM clients WHERE token_hash=? AND revoked=0 AND expires_at>?",
            (hashlib.sha256(token.encode()).hexdigest(), time.time()),
        ).fetchone()
    if row is None:
        return None
    return Policy(
        frozenset(json.loads(row["scopes"])),
        None if row["projects"] is None else tuple(json.loads(row["projects"])),
    )


def revoke_client(store: Store, client_id: str) -> dict:
    with store.connection(write=True) as db:
        db.execute("UPDATE clients SET revoked=1 WHERE id=?", (client_id,))
    return {"client_id": client_id, "revoked": True}


def list_clients(store: Store) -> list[dict]:
    with store.connection() as db:
        rows = db.execute(
            "SELECT id,name,scopes,projects,expires_at,revoked FROM clients ORDER BY id"
        )
        return [dict(r) for r in rows]


def read_connection(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        validate_endpoint(data["url"])
        if data.get("version") != 1 or not isinstance(data["token"], str) or not data["token"]:
            raise ValueError
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InputError("Cannot read a valid connection credential file.") from exc


class BearerAuth:
    """Authenticate every HTTP request without exposing token values to tools."""

    def __init__(self, app, store: Store, *, oauth=None):
        self.app, self.store = app, store
        self.oauth = oauth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        value = headers.get(b"authorization", b"").decode("latin-1")
        bearer = value[:7].lower() == "bearer "
        policy = (
            await anyio.to_thread.run_sync(authenticate, self.store, value[7:]) if bearer else None
        )
        if policy is None and self.oauth is not None and bearer:
            policy = await anyio.to_thread.run_sync(self.oauth.policy, value[7:])
        if policy is None:
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="dots-brain"'
                    + (
                        f', resource_metadata="{self.oauth.issuer}'
                        '/.well-known/oauth-protected-resource/mcp"'
                        if self.oauth is not None
                        else ""
                    )
                },
            )
            return await response(scope, receive, send)
        scope = {**scope, "brain_policy": policy}
        return await self.app(scope, receive, send)
