"""Local credential files and the HTTP boundary for static and OAuth grants."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import anyio

from .errors import ForbiddenError, InputError
from .installation_state import marker_path
from .local import publish_new, sync_directory
from .store import Store, normalize_projects, validate_identifier, validate_integer

MEMORY_SCOPES = frozenset({"memory:read", "memory:write", "memory:forget"})
SCOPES = MEMORY_SCOPES | {"audit:read", "audit:write", "cortex:read", "cortex:write"}


@dataclass(frozen=True)
class Policy:
    scopes: frozenset[str] = SCOPES
    projects: tuple[str, ...] | None = None
    principal: str = "local-owner:stdio"

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ForbiddenError(f"This client does not have the required {scope} scope.")


def validate_endpoint(url: str) -> str:
    if not isinstance(url, str):
        raise InputError("Expected an absolute MCP endpoint ending in /mcp.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise InputError("The MCP endpoint has an invalid host or port.") from None
    if port is not None and not 1 <= port <= 65535:
        raise InputError("The MCP endpoint has an invalid port.")
    if any(character.isspace() or ord(character) < 32 for character in url):
        raise InputError("The MCP endpoint must not contain whitespace or control characters.")
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
    store.ensure_writable()
    validate_identifier(name, "name", 200)
    if not scopes or not set(scopes) <= SCOPES:
        raise InputError("Choose explicitly supported memory, audit, or CORTEX scopes.")
    validate_integer(days, "days", maximum=365)
    projects = normalize_projects(projects)
    validate_endpoint(url)
    client_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
    expires = time.time() + days * 86400
    payload = {"version": 1, "client_id": client_id, "url": url, "token": token}
    output = output.expanduser().absolute()
    if not output.parent.is_dir():
        raise InputError("The credential output directory must already exist.")
    if output.exists() or output.is_symlink():
        raise InputError("Credential output already exists; it was preserved.")
    descriptor, temporary = tempfile.mkstemp(prefix=".dots-brain-credential-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        publish_new(Path(temporary), output)
        sync_directory(output.parent)
        with store.connection(write=True) as db:
            store.ensure_writable()
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
    except FileExistsError as exc:
        raise InputError("Credential output already exists; it was preserved.") from exc
    except BaseException:
        try:
            published = output.stat(follow_symlinks=False)
            prepared = Path(temporary).stat()
            if (published.st_dev, published.st_ino) == (prepared.st_dev, prepared.st_ino):
                output.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)
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
    if marker_path(store).exists():
        return None
    if len(token) > 1024:
        return None
    with store.connection() as db:
        row = db.execute(
            "SELECT id,scopes,projects FROM clients "
            "WHERE token_hash=? AND revoked=0 AND expires_at>?",
            (hashlib.sha256(token.encode()).hexdigest(), time.time()),
        ).fetchone()
    if row is None:
        return None
    return Policy(
        frozenset(json.loads(row["scopes"])),
        None if row["projects"] is None else tuple(json.loads(row["projects"])),
        f"local-client:{row['id']}",
    )


def revoke_client(store: Store, client_id: str) -> dict:
    with store.connection(write=True) as db:
        changed = db.execute("UPDATE clients SET revoked=1 WHERE id=?", (client_id,)).rowcount
    if not changed:
        raise InputError("This client does not exist.")
    return {"client_id": client_id, "revoked": True}


def list_clients(store: Store) -> list[dict]:
    with store.connection() as db:
        rows = db.execute(
            "SELECT id,name,scopes,projects,expires_at,revoked FROM clients ORDER BY id"
        )
        return [
            {
                **dict(row),
                "scopes": json.loads(row["scopes"]),
                "projects": None if row["projects"] is None else json.loads(row["projects"]),
            }
            for row in rows
        ]


def read_connection(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_endpoint(data["url"])
        if (
            data.get("version") != 1
            or not isinstance(data["token"], str)
            or not data["token"]
            or not isinstance(data["client_id"], str)
            or not data["client_id"]
        ):
            raise ValueError
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InputError("Cannot read a valid connection credential file.") from exc


class BearerAuth:
    """Authenticate every HTTP request without exposing token values to tools."""

    def __init__(self, app, store: Store, *, oauth=None, public_gateway: bool = False):
        self.app, self.store = app, store
        self.oauth = oauth
        # This setting is server-owned. It must be supplied by the listener or
        # supervisor, never inferred from Host or forwarded request headers.
        self.public_gateway = public_gateway

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        raw_headers = scope["headers"]
        if sum(key.lower() == b"authorization" for key, _ in raw_headers) > 1:
            from starlette.responses import JSONResponse

            return await JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)
        headers = dict(raw_headers)
        value = headers.get(b"authorization", b"").decode("latin-1")
        bearer = value[:7].lower() == "bearer "
        policy = (
            await anyio.to_thread.run_sync(authenticate, self.store, value[7:])
            if bearer and not self.public_gateway
            else None
        )
        if policy is None and self.oauth is not None and bearer:
            policy = await anyio.to_thread.run_sync(self.oauth.policy, value[7:])
        if policy is None:
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="dots-brain", error="invalid_token"'
                    + (
                        f', resource_metadata="{self.oauth.issuer}'
                        '/.well-known/oauth-protected-resource/mcp"'
                        if self.oauth is not None
                        else ""
                    ),
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                },
            )
            return await response(scope, receive, send)
        scope = {**scope, "brain_policy": policy}
        return await self.app(scope, receive, send)
