"""Small deterministic commands for operators and installation agents."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from contextlib import ExitStack
from pathlib import Path

from . import __version__
from .auth import SCOPES
from .clients import PROVIDERS
from .errors import BrainError, CapabilityError, InputError
from .local import exclusive_output, locked, writer_lock
from .store import Store


def default_directory() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "dots-brain"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2,
            json.dumps(
                {
                    "state": "error",
                    "code": "invalid_arguments",
                    "message": "Invalid command arguments.",
                    "hint": f"Run {self.prog} --help for valid options.",
                }
            )
            + "\n",
        )


def parser() -> argparse.ArgumentParser:
    root = Parser(prog="dots-brain", description="One source-aware memory for your AI tools.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--data-dir", type=Path, default=default_directory())
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Initialize or reuse the local memory database.")
    migration = commands.add_parser("migrate", help="Plan or apply the offline v1 to v2 migration.")
    migration.add_argument("--apply", action="store_true")
    migration.add_argument("--writers-stopped", action="store_true")
    migration.add_argument("--backup", type=Path)
    backup = commands.add_parser("backup", help="Write a consistent private SQLite backup.")
    backup.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore", help="Restore into a new disabled directory.")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    activate = commands.add_parser(
        "activate-restore", help="Freeze the old store and activate a reviewed restored copy."
    )
    activate.add_argument("--target", type=Path, required=True)
    activate.add_argument("--writers-stopped", action="store_true")
    cortex = commands.add_parser("cortex", help="Reserved CORTEX module command (disabled).")
    cortex.add_argument("--endpoint", required=True)
    cortex.add_argument("--token-file", type=Path, required=True)
    cortex.add_argument("--project-map", action="append", required=True)
    audit = commands.add_parser("audit", help="Inspect sanitized audit events on the memory host.")
    audit.add_argument("action", choices=["report", "events"])
    audit.add_argument("--project")
    audit.add_argument("--since")
    audit.add_argument("--until")
    audit.add_argument("--after-id", type=int)
    audit.add_argument("--limit", type=int, default=100)
    capture = commands.add_parser("capture", help="Reserved snapshot capture command (disabled).")
    capture.add_argument("--kind", choices=["audit", "transcript"], required=True)
    capture.add_argument("--path", type=Path, action="append", required=True)
    capture.add_argument("--cursor", type=Path, required=True)
    capture.add_argument("--credential-file", type=Path, required=True)
    capture.add_argument("--project", required=True)
    capture.add_argument("--account", required=True)
    start = commands.add_parser("up", help="Start or reuse and verify the local HTTP service.")
    start.add_argument("--port", type=int)
    start.add_argument("--semantic", action="store_true")
    start.add_argument(
        "--resume", action="store_true", help="Explicitly re-enable a removed installation."
    )
    commands.add_parser("down", help="Stop the managed local service and preserve its data.")
    commands.add_parser("providers", help="List implemented client adapters and their limits.")
    connect = commands.add_parser("connect", help="Configure and verify a supported MCP client.")
    connect.add_argument("provider", choices=sorted(PROVIDERS))
    connect.add_argument("--config", type=Path)
    connect.add_argument("--credential-file", type=Path)
    connect.add_argument("--project", action="append")
    connect.add_argument(
        "--mcp-url", help="Explicit loopback private endpoint of this host's supervisor."
    )
    disconnect = commands.add_parser("disconnect", help="Remove one managed client connection.")
    disconnect.add_argument("provider", choices=sorted(PROVIDERS))
    disconnect.add_argument("--config", type=Path)
    disconnect.add_argument("--dry-run", action="store_true")
    uninstall = commands.add_parser(
        "uninstall", help="Disable the service and detach clients; keep memories."
    )
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="PROVIDER=PATH",
        help="Include a legacy custom configuration location.",
    )
    doctor = commands.add_parser(
        "doctor", help="Report local capabilities without assuming remote readiness."
    )
    doctor.add_argument(
        "--mcp-url", help="Explicit private loopback MCP URL; does not start a service."
    )
    doctor.add_argument(
        "--credential-file", type=Path, help="Existing credential for read verification."
    )
    oauth = commands.add_parser("oauth", help="Manage VM-local OAuth without printing credentials.")
    oauth_actions = oauth.add_subparsers(dest="oauth_action", required=True)
    oauth_setup = oauth_actions.add_parser("configure")
    oauth_setup.add_argument(
        "--issuer", required=True, help="Existing HTTPS origin; does not create ingress."
    )
    oauth_setup.add_argument(
        "--no-start",
        action="store_true",
        help="Compatibility flag; configuration never starts a service.",
    )
    oauth_setup.add_argument(
        "--replace-issuer",
        action="store_true",
        help="Invalidate old OAuth access when replacing the issuer.",
    )
    oauth_disable = oauth_actions.add_parser("disable")
    oauth_disable.add_argument(
        "--no-start",
        action="store_true",
        help="Compatibility flag; disable never starts a service.",
    )
    for name in ("status", "pending", "grants"):
        oauth_actions.add_parser(name)
    approve = oauth_actions.add_parser("approve")
    approve.add_argument("request_id")
    projects = approve.add_mutually_exclusive_group(required=True)
    projects.add_argument("--project", action="append")
    projects.add_argument("--all-projects", action="store_true")
    approve.add_argument("--allow-forget", action="store_true")
    approve.add_argument("--scope", action="append", choices=sorted(SCOPES))
    deny = oauth_actions.add_parser("deny")
    deny.add_argument("request_id")
    revoke_oauth = oauth_actions.add_parser("revoke")
    revoke_oauth.add_argument("grant_id")
    serve = commands.add_parser("serve", help="Run on the memory host; HTTP binds loopback only.")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--public-port", type=int, help="Additional loopback OAuth-only port; --port stays private."
    )
    serve.add_argument(
        "--public-gateway", action="store_true", help="Make the single HTTP listener OAuth-only."
    )
    serve.add_argument("--semantic", action="store_true")
    model = commands.add_parser("model", help="Manage the pinned local embedding model.")
    model.add_argument("action", choices=["prepare"])
    commands.add_parser("index", help="Index pending memories with the local model.")
    export = commands.add_parser("export", help="Export memories and revisions as JSON Lines.")
    export.add_argument("--output", required=True, type=Path)
    for name in ("bridge", "verify"):
        command = commands.add_parser(name, help="Use an existing credential file; never print it.")
        command.add_argument("--credential-file", required=True, type=Path)
        if name == "bridge":
            command.add_argument("--local-data-dir", type=Path)
        else:
            command.add_argument("--write", action="store_true")
            command.add_argument("--project", default="default")
    client = commands.add_parser("client", help="Manage scoped credentials on the memory host.")
    actions = client.add_subparsers(dest="client_action", required=True)
    create = actions.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--scope", action="append", choices=sorted(SCOPES))
    create.add_argument("--project", action="append")
    create.add_argument("--days", type=int, default=30)
    create.add_argument("--credential-file", required=True, type=Path)
    create.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    revoke = actions.add_parser("revoke")
    revoke.add_argument("client_id")
    actions.add_parser("list")
    return root


def run(args) -> dict | None:
    # Reject incompatible options before creating locks or opening the store.
    if args.command in {"cortex", "capture"}:
        raise CapabilityError(
            f"The {args.command} module is disabled and excluded from the core distribution. "
            "Existing module state was preserved. Activation requires a separate reviewed plan."
        )
    if args.command in {"connect", "disconnect"}:
        if args.provider == "mcp-json" and args.config is None:
            raise InputError("mcp-json requires --config PATH.")
        if args.command == "connect" and args.mcp_url and args.credential_file:
            raise InputError("Choose --mcp-url or --credential-file.")
        if args.command == "connect" and args.mcp_url:
            from .runtime import private_endpoint

            private_endpoint(args.mcp_url)
    if args.command in {"serve", "up"}:
        minimum = 1 if args.command == "serve" else 0
        if args.port is not None and not minimum <= args.port <= 65535:
            raise InputError(f"Port must be between {minimum} and 65535.")
    if args.command == "serve":
        if args.transport != "http" and (args.public_port is not None or args.public_gateway):
            raise InputError("Public listener options require --transport http.")
        if args.public_port is not None and (
            not 1 <= args.public_port <= 65535
            or args.public_port == args.port
            or args.public_gateway
        ):
            raise InputError("Choose distinct private/public ports in 1..65535.")
    # One lease for an entire local command, including the lifetime of HTTP/stdio.
    writes = args.command in {"setup", "serve", "up", "index", "model", "client"}
    writes |= args.command == "connect" and args.credential_file is None
    writes |= args.command == "oauth" and args.oauth_action not in {"configure", "disable"}
    with ExitStack() as leases:
        if writes:
            leases.enter_context(writer_lock(Store(args.data_dir).directory))
        if args.command == "serve" and args.transport == "http":
            leases.enter_context(locked(Store(args.data_dir).directory / "http.lock", timeout=0))
        return _run(args)


def _run(args) -> dict | None:
    store = Store(args.data_dir)
    if args.command in {"migrate", "backup", "restore", "activate-restore"}:
        from .operations import (
            activate_restore,
            backup_store,
            migrate,
            restore_store,
        )

        if args.command == "migrate":
            return migrate(
                store, apply=args.apply, writers_stopped=args.writers_stopped, backup=args.backup
            )
        if args.command == "backup":
            return backup_store(store, args.output)
        if args.command == "restore":
            return restore_store(args.backup, Store(args.target), latest_deletions=store)
        if args.command == "activate-restore":
            return activate_restore(store, Store(args.target), writers_stopped=args.writers_stopped)
    if args.command == "audit":
        from .activity import AuditLog
        from .auth import Policy

        audit = AuditLog(store)
        arguments = dict(project=args.project, since=args.since, until=args.until, limit=args.limit)
        if args.action == "report":
            return audit.report(Policy(), **arguments)
        rows = audit.events(Policy(), **arguments, after_id=args.after_id)
        return {
            "events": rows,
            "next_after_id": rows[-1]["id"] if rows else args.after_id,
            "has_more": bool(
                rows
                and audit.events(Policy(), **(arguments | {"limit": 1}), after_id=rows[-1]["id"])
            ),
        }
    if args.command == "oauth":
        from .local import locked
        from .oauth import OAuthStore, configuration, configure, disable

        if args.oauth_action == "status":
            config = configuration(store)
            return {
                "state": "configured" if config else "not_configured",
                "issuer": config["issuer"] if config else None,
                "service_disabled": (store.directory / "disabled.json").exists(),
                "public_ingress": "not_verified",
                "secret_isolation": False,
            }
        store.status()  # Never initialize another memory on a client device.
        with locked(store.directory / "installation.lock"):
            if args.oauth_action == "configure":
                result = configure(store, args.issuer, replace_issuer=args.replace_issuer)
                return {
                    **result,
                    "service": None,
                }
            if args.oauth_action == "disable":
                return disable(store)
            state = OAuthStore(store)
            if args.oauth_action == "pending":
                return state.pending()
            if args.oauth_action == "grants":
                return state.grants()
            if args.oauth_action == "revoke":
                return state.revoke_grant(args.grant_id)
            if args.oauth_action == "deny":
                return state.decide(args.request_id, deny=True)
            return state.decide(
                args.request_id,
                projects=args.project,
                allow_forget=args.allow_forget,
                scopes=args.scope,
            )
    if args.command in ("disconnect", "uninstall"):
        from .removal import disconnect_client, uninstall

        return (
            disconnect_client(
                store, provider=args.provider, config=args.config, dry_run=args.dry_run
            )
            if args.command == "disconnect"
            else uninstall(store, dry_run=args.dry_run, extra_configs=args.config)
        )
    if args.command in ("connect", "providers"):
        from .clients import connect_client, providers

        return (
            providers()
            if args.command == "providers"
            else connect_client(
                store,
                provider=args.provider,
                config=args.config,
                connection=args.credential_file,
                projects=args.project,
                mcp_url=args.mcp_url,
            )
        )
    if args.command in ("up", "down"):
        from .runtime import down, up

        return (
            up(store, port=args.port, semantic=args.semantic, resume=args.resume)
            if args.command == "up"
            else down(store)
        )
    if args.command == "setup":
        store.initialize()
        disabled = (store.directory / "disabled.json").exists()
        return {
            "state": "disabled" if disabled else "local_ready",
            "version": __version__,
            "data_dir": str(store.directory),
            "read": "disabled" if disabled else "available",
            "write": "disabled" if disabled else "available",
            "semantic": "not_enabled",
            "remote_connection": "not_verified",
            "capture": "disabled",
        }
    if args.command == "doctor":
        from .diagnostics import diagnose

        return {
            **diagnose(store, mcp_url=args.mcp_url, connection=args.credential_file),
            "version": __version__,
            "sqlite_version": sqlite3.sqlite_version,
            "vm_persistence": "not_verified",
            "public_ingress": "not_verified",
            "secret_isolation": False,
            "host_costs": "not_verified",
            "event_automation": "not_implemented",
        }
    if args.command in ("bridge", "verify"):
        from .bridge import resume_local_connection, run_bridge, verify_connection

        if args.command == "bridge":
            if args.local_data_dir is not None:
                resume_local_connection(args.credential_file, args.local_data_dir)
            return asyncio.run(run_bridge(args.credential_file))
        return asyncio.run(
            verify_connection(args.credential_file, write=args.write, project=args.project)
        )
    if args.command == "serve":
        from .runtime import ensure_enabled
        from .server import create_server, run_http
        from .service import MemoryService

        ensure_enabled(store)
        store.status()  # Fail before starting if setup has not completed.
        semantic = None
        if args.semantic:
            from .semantic import SemanticIndex

            semantic = SemanticIndex(store)
        service = MemoryService(store, semantic)
        server = create_server(service, http=args.transport == "http", port=args.port)
        if args.transport == "http":
            run_http(
                server,
                service,
                port=args.port,
                public_port=args.public_port,
                public_gateway=args.public_gateway,
            )
        else:
            server.run(transport="stdio")
        return None
    if args.command == "model":
        from .semantic import prepare_model

        return prepare_model(store)
    if args.command == "index":
        from .semantic import SemanticIndex

        semantic = SemanticIndex(store)
        indexed = 0
        while True:
            result = semantic.index()
            indexed += result["indexed_now"]
            if result["examined"] == 0:
                return {"indexed_this_run": indexed, **semantic.status()}
    if args.command == "export":
        output = args.output.expanduser().absolute()
        count = 0
        with exclusive_output(output) as descriptor:
            with os.fdopen(descriptor, "w", closefd=False) as stream:
                for record in store.export():
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += 1
                stream.flush()
                os.fsync(stream.fileno())
        return {"output": str(output), "revisions": count}
    if args.command == "client":
        from .auth import issue_client, list_clients, revoke_client

        if args.client_action == "create":
            return issue_client(
                store,
                name=args.name,
                scopes=args.scope or ["memory:read"],
                projects=args.project,
                days=args.days,
                output=args.credential_file,
                url=args.url,
            )
        if args.client_action == "revoke":
            return revoke_client(store, args.client_id)
        return {"clients": list_clients(store)}
    return None


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    try:
        result = run(parser().parse_args())
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
            if result.get("state") in {"verification_failed", "blocked", "partial"}:
                raise SystemExit(1)
    except BrainError as exc:
        print(
            json.dumps({"state": "error", "code": exc.code, "message": str(exc)}), file=sys.stderr
        )
        raise SystemExit(1) from None
    except (OSError, ValueError, sqlite3.Error, ExceptionGroup) as exc:
        # Connection and OS errors can contain secret URLs or local paths. Keep them out
        # of machine output; diagnose credentials and permissions without dumping inputs.
        code, hint = "operation_failed", "Check permissions, connectivity, and command arguments."
        for kind, name, advice in (
            (TimeoutError, "timeout", "Check endpoint availability before retrying."),
            (ConnectionError, "connection_failed", "Check the endpoint and network connection."),
            (PermissionError, "permission_denied", "Check access rights on the selected path."),
            (FileNotFoundError, "not_found", "Check that the required file or executable exists."),
            (BlockingIOError, "busy", "Wait for the running operation and retry."),
            (sqlite3.Error, "database_error", "Run doctor; preserve the database and backups."),
        ):
            if isinstance(exc, kind):
                code, hint = name, advice
                break
        print(
            json.dumps(
                {
                    "state": "error",
                    "code": code,
                    "message": "The command could not complete.",
                    "hint": hint,
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
