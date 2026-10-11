"""Independent local checks and an explicit read-only MCP probe."""

import asyncio
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit

from .auth import authenticate, read_connection
from .bridge import verify_connection
from .database_io import open_readonly
from .errors import BrainError, InputError
from .local import read_json
from .oauth import configuration
from .runtime import bound_state, private_endpoint, state_path


def diagnose(store, *, mcp_url=None, connection=None):
    checks, memory, oauth, state, status = {}, None, None, {}, {}
    checks["sqlite"] = "ready" if sqlite3.sqlite_version_info >= (3, 42, 0) else "invalid"
    disabled = (store.directory / "disabled.json").exists() or (
        store.directory / "disabled.json"
    ).is_symlink()
    try:
        if store.path.is_file():
            with closing(open_readonly(store.path)) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version != 2:
                    raise InputError("Unsupported schema.")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise InputError("Database check failed.")
                memory = {
                    "memories": db.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                    "schema_version": version,
                }
        checks["database"] = "ready" if memory is not None else "setup_required"
    except (BrainError, OSError, ValueError, sqlite3.Error):
        checks["database"] = "invalid_or_migration_required"
    try:
        oauth = configuration(store)
        checks["oauth"] = "configured" if oauth else "not_configured"
    except (BrainError, OSError, ValueError, sqlite3.Error):
        checks["oauth"] = "invalid"
    try:
        state = read_json(state_path(store)) if state_path(store).exists() else {}
        checks["managed_state"] = (
            "bound" if bound_state(store, state) else "unbound" if state else "absent"
        )
    except (BrainError, OSError, ValueError):
        checks["managed_state"] = "invalid"
    checks["mcp"] = "not_verified"
    if mcp_url or connection or checks["managed_state"] == "bound":
        try:
            path = connection or store.directory / "probe.connection.json"
            credential = read_connection(path)
            expected = private_endpoint(mcp_url or state.get("url", credential["url"]))
            if credential["url"] != expected or authenticate(store, credential["token"]) is None:
                raise InputError("The endpoint or credential does not belong to this store.")
            result = asyncio.run(asyncio.wait_for(verify_connection(path), timeout=5))
            if not result["read"]:
                raise InputError("MCP read verification failed.")
            checks["mcp"], status = "verified_read", result.get("status") or {}
        except Exception:
            checks["mcp"] = "endpoint_or_credential_failed"
    warnings = []
    if oauth and urlsplit(oauth["issuer"]).hostname not in {"localhost", "127.0.0.1", "::1"}:
        if checks["mcp"] != "verified_read":
            warnings.append("public_oauth_listener_unverified")
        elif not any(item["policy"] == "oauth_only" for item in status.get("http_listeners", [])):
            warnings.append("public_oauth_listener_missing")
    failed = any(
        value
        in {"invalid", "unbound", "invalid_or_migration_required", "endpoint_or_credential_failed"}
        for value in checks.values()
    )
    return {
        "state": "disabled"
        if disabled
        else "partial"
        if failed
        else "local_ready"
        if memory
        else "setup_required",
        "memory": memory,
        "checks": checks,
        "warnings": warnings,
        "remote_oauth": checks["oauth"],
        "http_listeners": status.get("http_listeners", []),
        "next_action": "For an external supervisor, provide --mcp-url and an existing "
        "--credential-file; manage its lifecycle through that supervisor."
        if checks["mcp"] == "not_verified"
        else "Inspect failed checks and warnings."
        if failed or warnings
        else None,
    }
