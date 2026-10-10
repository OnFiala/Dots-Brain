"""Operator commands with no implicit daemon startup."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from . import __version__
from .errors import BrainError, InputError, StateError
from .installation_state import marker_path
from .local import locked, publish_new, read_json, sync_directory
from .protocol import safe_error


def run_storage(store, args):
    from .operations import (
        abort_restore,
        activate_restore,
        backup_store,
        configure_cortex,
        migrate,
        restore_store,
    )
    from .store import Store

    if args.command == "migrate":
        return migrate(
            store, apply=args.apply, writers_stopped=args.writers_stopped, backup=args.backup
        )
    if args.command == "backup":
        return backup_store(store, args.output)
    if args.command == "restore":
        return restore_store(args.backup, Store(args.target), latest_deletions=store)
    if args.command in {"activate-restore", "abort-restore"}:
        action = activate_restore if args.command == "activate-restore" else abort_restore
        return action(store, Store(args.target), writers_stopped=args.writers_stopped)
    if args.action == "configure":
        if not args.endpoint or not args.token_file or not args.project_map:
            raise InputError(
                "CORTEX configuration requires --endpoint, --token-file and --project-map."
            )
        return configure_cortex(
            store, endpoint=args.endpoint, token_file=args.token_file, projects=args.project_map
        )
    from .auth import Policy
    from .integration_tools import load_cortex

    if not args.project:
        raise InputError("Choose the local --project for the CORTEX operation.")
    connector = load_cortex(store)
    if connector is None:
        raise StateError("CORTEX is not configured.")
    if args.action == "operations":
        return {"operations": connector.operations(policy=Policy(), project=args.project)}
    if not args.operation_id or not args.resolution:
        raise InputError("Resolve requires --operation-id and --resolution.")
    return connector.resolve_operation(
        policy=Policy(),
        project=args.project,
        operation_id=args.operation_id,
        resolution=args.resolution,
        writers_stopped=args.writers_stopped,
    )


def run_oauth(store, args):
    from .oauth import OAuthStore, configuration, configure, record_auth_change, revoke_all

    if args.oauth_action == "status":
        config = configuration(store)
        return {
            "state": "configured" if config else "not_configured",
            "issuer": config["issuer"] if config else None,
            "service_disabled": marker_path(store).exists(),
            "public_ingress": "not_verified",
            "secret_isolation": False,
        }
    store.status()
    with locked(store.directory / "installation.lock"):
        if args.oauth_action == "configure":
            return {
                **configure(
                    store,
                    args.issuer,
                    replace_issuer=args.replace_issuer,
                    discard_journal=args.discard_journal,
                ),
                "restart_required": True,
            }
        if args.oauth_action == "disable":
            with store.connection(write=True) as db:
                revoked = revoke_all(db)
                record_auth_change(store, db, "oauth_disabled", details=revoked)
            (store.directory / "oauth.json").unlink(missing_ok=True)
            sync_directory(store.directory)
            return {"state": "oauth_disabled", **revoked, "restart_required": True}
        state = OAuthStore(store)
        if args.oauth_action == "onboarding":
            if args.onboarding_action == "status":
                return state.onboarding_state()
            seconds = args.minutes * 60 if args.onboarding_action == "open" else None
            return state.set_onboarding(open_for_seconds=seconds)
        if args.oauth_action == "pending":
            return state.pending(verbose=args.verbose)
        if args.oauth_action == "grants":
            return state.grants()
        if args.oauth_action == "clients":
            return state.clients()
        if args.oauth_action == "purge":
            return state.purge()
        if args.oauth_action == "revoke":
            return state.revoke_grant(args.grant_id)
        if args.oauth_action == "deny":
            return state.decide(args.request_id, deny=True)
        return state.decide(
            args.request_id,
            projects=args.project,
            allow_forget=args.allow_forget,
            scopes=args.scope,
            redirect_host=args.redirect_host,
        )


def doctor(store):
    """Return independent diagnoses, including an unreadable or older database."""
    from .database_checks import validate_snapshot
    from .oauth import configuration
    from .runtime import managed_status
    from .schema import SCHEMA_VERSION

    def database():
        with store.connection() as db:
            validate_snapshot(db, SCHEMA_VERSION)
        return store.status()

    checks = {}
    for name, probe in {
        "database": database,
        "oauth": lambda: {"state": "configured" if configuration(store) else "not_configured"},
        "managed_service": lambda: managed_status(store),
    }.items():
        try:
            value = probe()
            checks[name] = value
        except (BrainError, OSError, ValueError, sqlite3.Error) as exc:
            checks[name] = {"state": "error", "error": safe_error(exc)}
    blocked = any(value.get("state") == "error" for value in checks.values())
    return {
        "state": "diagnosis_completed",
        "healthy": not blocked and not marker_path(store).exists(),
        "version": __version__,
        "sqlite_version": sqlite3.sqlite_version,
        "service_disabled": marker_path(store).exists(),
        "checks": checks,
        "vm_persistence": "not_verified",
        "public_ingress": "not_verified",
        "event_automation": "not_verified",
    }


def export_store(store, output: Path):
    """Publish only a complete fsynced export, without replacing an existing file."""
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise InputError("Export output already exists.")
    fd, temporary = tempfile.mkstemp(prefix=".dots-export-", dir=output.parent)
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for record in store.export():
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        publish_new(Path(temporary), output)
        sync_directory(output.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"output": str(output), "revisions": count}


def client_url(store, supplied):
    from .auth import validate_endpoint

    if supplied:
        return validate_endpoint(supplied)
    state_path = store.directory / "service.json"
    if not state_path.exists():
        raise InputError("Provide --url, or start the managed local service first.")
    state = read_json(state_path)
    port = state.get("port")
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or state.get("url") != f"http://127.0.0.1:{port}/mcp"
    ):
        raise StateError("Managed service endpoint is invalid; provide an explicit --url.")
    return validate_endpoint(state["url"])


def run_serve(store, args):
    import socket

    from .integration_tools import load_cortex
    from .runtime import ensure_enabled, serve_locks
    from .server import create_http_app, create_server
    from .service import MemoryService

    if args.public_gateway and (args.transport != "http" or args.listen_fd is not None):
        raise InputError("Public gateway mode requires a separately supervised HTTP listener.")
    store.status()  # No lock files or new directories for an uninitialized store.
    with serve_locks(store, http=args.transport == "http"):
        ensure_enabled(store)
        semantic = None
        if args.semantic:
            from .semantic import SemanticIndex

            semantic = SemanticIndex(store)
        service = MemoryService(store, semantic)
        try:
            service.cortex = load_cortex(store)
        except BrainError:
            service.cortex_error = "invalid_configuration"
        server = create_server(service, http=args.transport == "http", port=args.port)
        if args.listen_fd is not None:
            if args.transport != "http" or args.listen_fd < 3:
                raise InputError("An inherited listener is valid only for managed HTTP startup.")
            with socket.socket(fileno=os.dup(args.listen_fd)) as listener:
                if listener.getsockname() != ("127.0.0.1", args.port) or not listener.getsockopt(
                    socket.SOL_SOCKET, socket.SO_ACCEPTCONN
                ):
                    raise InputError("The inherited listener does not match the loopback endpoint.")
        if args.transport == "http":
            import uvicorn

            binding = (
                {"fd": args.listen_fd}
                if args.listen_fd is not None
                else {"host": "127.0.0.1", "port": args.port}
            )
            uvicorn.run(
                create_http_app(server, service, public_gateway=args.public_gateway),
                **binding,
                access_log=False,
                log_level="warning",
            )
        else:
            server.run(transport="stdio")
