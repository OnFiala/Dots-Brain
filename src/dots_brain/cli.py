"""Small deterministic commands for operators and installation agents."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .auth import SCOPES
from .clients import PROVIDERS
from .errors import InputError
from .store import Store


def default_directory() -> Path:
    configured = os.environ.get("XDG_DATA_HOME", "")
    base = (
        Path(configured)
        if configured and Path(configured).is_absolute()
        else Path.home() / ".local" / "share"
    )
    return base / "dots-brain"


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse includes user-supplied values; use a stable, non-reflective error.
        print(
            json.dumps(
                {
                    "state": "error",
                    "code": "invalid_input",
                    "message": "Invalid command arguments; use --help.",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)


def port_number(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535.")
    return number


def managed_port(value):
    return 0 if value == "0" else port_number(value)


def parser() -> argparse.ArgumentParser:
    root = ArgumentParser(description="One source-aware memory for your AI tools.")
    root.add_argument(
        "--version", action="version", version=__version__, help="Print the installed version."
    )
    root.add_argument(
        "--data-dir",
        type=Path,
        help="Canonical private memory directory; defaults to the user data directory.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    _add_storage_commands(commands)
    _add_audit_commands(commands)
    _add_clients_commands(commands)
    _add_oauth_commands(commands)
    _add_service_commands(commands)
    return root


def _add_storage_commands(commands):
    commands.add_parser("setup", help="Initialize or reuse the local memory database.")
    migration = commands.add_parser("migrate", help="Plan or apply the offline database migration.")
    migration.add_argument(
        "--apply",
        action="store_true",
        help="Apply the planned migration after making its validated backup.",
    )
    migration.add_argument(
        "--writers-stopped",
        action="store_true",
        help="Confirm all clients, servers and other database writers are stopped.",
    )
    migration.add_argument("--backup", type=Path, help="Standalone SQLite backup path.")
    backup = commands.add_parser("backup", help="Write a consistent private SQLite backup.")
    backup.add_argument(
        "--output", type=Path, required=True, help="New output file; existing files are preserved."
    )
    restore = commands.add_parser("restore", help="Restore into a new disabled directory.")
    restore.add_argument(
        "--backup", type=Path, required=True, help="Standalone SQLite backup path."
    )
    restore.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Restored data directory for this recovery operation.",
    )
    activate = commands.add_parser(
        "activate-restore", help="Freeze the old store and activate a reviewed restored copy."
    )
    activate.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Restored data directory for this recovery operation.",
    )
    activate.add_argument(
        "--writers-stopped",
        action="store_true",
        help="Confirm all clients, servers and other database writers are stopped.",
    )
    abort = commands.add_parser(
        "abort-restore", help="Cancel a pending cutover; keep the restored target disabled."
    )
    abort.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Restored data directory for this recovery operation.",
    )
    abort.add_argument(
        "--writers-stopped",
        action="store_true",
        help="Confirm all clients, servers and other database writers are stopped.",
    )
    cortex = commands.add_parser("cortex", help="Configure a dedicated CORTEX MCP connection.")
    cortex.add_argument(
        "action",
        nargs="?",
        choices=["configure", "operations", "resolve"],
        default="configure",
        help="Operation to perform.",
    )
    cortex.add_argument("--endpoint", help="Dedicated CORTEX MCP URL.")
    cortex.add_argument(
        "--token-file",
        type=Path,
        help="Existing private credential file; its contents are never printed.",
    )
    cortex.add_argument(
        "--project-map",
        action="append",
        help="Local-to-CORTEX mapping LOCAL=REMOTE; repeat for each project.",
    )
    cortex.add_argument(
        "--project", help="Project boundary; only explicitly granted projects are accessible."
    )
    cortex.add_argument("--operation-id", help="Stored CORTEX operation to inspect or resolve.")
    cortex.add_argument(
        "--resolution",
        choices=["retry", "recover-sending"],
        help="Explicit operator recovery action; retry may duplicate an uncertain upstream write.",
    )
    cortex.add_argument(
        "--writers-stopped", action="store_true", help="Confirm all CORTEX writers are stopped."
    )


def _add_audit_commands(commands):
    audit = commands.add_parser("audit", help="Inspect sanitized audit events on the memory host.")
    audit.add_argument("action", choices=["report", "events"], help="Operation to perform.")
    audit.add_argument(
        "--project", help="Project boundary; only explicitly granted projects are accessible."
    )
    audit.add_argument("--since", help="Inclusive lower bound for server-recorded UTC time.")
    audit.add_argument("--until", help="Exclusive upper bound for server-recorded UTC time.")
    audit.add_argument("--after-id", type=int, help="Return events after this server audit ID.")
    audit.add_argument(
        "--limit", type=int, default=100, help="Maximum events in this page (default: 100)."
    )
    capture = commands.add_parser(
        "capture", help="Collect one bounded pass from provider JSONL snapshots."
    )
    capture.add_argument(
        "--kind", choices=["audit", "transcript"], required=True, help="Source schema to collect."
    )
    capture.add_argument(
        "--path",
        type=Path,
        action="append",
        required=True,
        help="Provider JSONL snapshot path; repeat for each source.",
    )
    capture.add_argument(
        "--cursor",
        type=Path,
        required=True,
        help="Private durable cursor file for this source set.",
    )
    capture.add_argument(
        "--credential-file", type=Path, required=True, help="Private connection JSON file."
    )
    capture.add_argument(
        "--project",
        required=True,
        help="Project boundary; only explicitly granted projects are accessible.",
    )
    capture.add_argument(
        "--account", required=True, help="Source account identifier retained as provenance."
    )
    capture.add_argument(
        "--recover-pending",
        action="store_true",
        help="Quarantine an invalid or rejected pending coverage receipt before retrying.",
    )
    preflight = commands.add_parser(
        "preflight", help="Inspect host capabilities without changing configuration."
    )
    preflight.add_argument(
        "--network-policy",
        type=Path,
        help="Optional JSON network-policy file to inspect; no provider path is assumed.",
    )


def _add_clients_commands(commands):
    start = commands.add_parser("up", help="Start or reuse and verify the local HTTP service.")
    start.add_argument(
        "--port",
        type=managed_port,
        help="Loopback TCP port; up also accepts 0 to choose a free port.",
    )
    start.add_argument(
        "--semantic",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable the prepared local embedding model.",
    )
    start.add_argument(
        "--resume", action="store_true", help="Explicitly re-enable a removed installation."
    )
    commands.add_parser("down", help="Stop the managed local service and preserve its data.")
    commands.add_parser("providers", help="List implemented client adapters and their limits.")
    connect = commands.add_parser("connect", help="Configure and verify a supported MCP client.")
    connect.add_argument("provider", choices=sorted(PROVIDERS), help="Supported client adapter.")
    connect.add_argument("--config", type=Path, help="Actual provider configuration path.")
    connect.add_argument("--credential-file", type=Path, help="Private connection JSON file.")
    connect.add_argument(
        "--project",
        action="append",
        help="Project boundary; only explicitly granted projects are accessible.",
    )
    disconnect = commands.add_parser("disconnect", help="Remove one managed client connection.")
    disconnect.add_argument("provider", choices=sorted(PROVIDERS), help="Supported client adapter.")
    disconnect.add_argument("--config", type=Path, help="Actual provider configuration path.")
    disconnect.add_argument(
        "--dry-run", action="store_true", help="Describe changes without applying them."
    )
    uninstall = commands.add_parser(
        "uninstall", help="Disable the service and detach clients; keep memories."
    )
    uninstall.add_argument(
        "--dry-run", action="store_true", help="Describe changes without applying them."
    )
    uninstall.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="PROVIDER=PATH",
        help="Include a legacy custom configuration location.",
    )
    commands.add_parser(
        "doctor", help="Report local capabilities without assuming remote readiness."
    )


def _add_oauth_commands(commands):
    oauth = commands.add_parser(
        "oauth", help="Manage host-local OAuth without printing credentials."
    )
    oauth_actions = oauth.add_subparsers(dest="oauth_action", required=True)
    oauth_setup = oauth_actions.add_parser("configure")
    oauth_setup.add_argument(
        "--issuer", required=True, help="Existing HTTPS origin; does not create ingress."
    )
    oauth_setup.add_argument(
        "--discard-journal",
        action="store_true",
        help="Discard an inspected, unrecoverable issuer-change journal.",
    )
    oauth_setup.add_argument(
        "--replace-issuer",
        action="store_true",
        help="Explicitly revoke old grants when changing the issuer.",
    )
    oauth_actions.add_parser("disable")
    for name in ("status", "pending", "grants", "clients", "purge"):
        action = oauth_actions.add_parser(name)
        if name == "pending":
            action.add_argument(
                "--verbose", action="store_true", help="Include untrusted client-supplied names."
            )
    onboarding = oauth_actions.add_parser(
        "onboarding", help="Bound registration and pairing to a short operator-opened window."
    )
    onboarding_actions = onboarding.add_subparsers(dest="onboarding_action", required=True)
    window = onboarding_actions.add_parser("open")
    window.add_argument(
        "--minutes", type=int, default=10, help="Pairing window length, 1–60 minutes (default: 10)."
    )
    onboarding_actions.add_parser("close")
    onboarding_actions.add_parser("status")
    approve = oauth_actions.add_parser("approve")
    approve.add_argument("request_id", help="Exact pending OAuth request approved by the owner.")
    approve.add_argument(
        "--redirect-host",
        required=True,
        help="Expected callback hostname from the client you deliberately connected.",
    )
    projects = approve.add_mutually_exclusive_group(required=True)
    projects.add_argument(
        "--project",
        action="append",
        help="Project boundary; only explicitly granted projects are accessible.",
    )
    projects.add_argument(
        "--all-projects",
        action="store_true",
        help="Grant access to every project; use only when intended.",
    )
    approve.add_argument(
        "--allow-forget", action="store_true", help="Explicitly permit permanent memory deletion."
    )
    approve.add_argument(
        "--scope",
        action="append",
        choices=sorted(SCOPES),
        help="Allowed capability; repeat for each scope.",
    )
    deny = oauth_actions.add_parser("deny")
    deny.add_argument("request_id", help="Exact pending OAuth request approved by the owner.")
    revoke_oauth = oauth_actions.add_parser("revoke")
    revoke_oauth.add_argument("grant_id", help="OAuth grant to revoke.")


def _add_service_commands(commands):
    serve = commands.add_parser("serve", help="Run on the memory host; HTTP binds loopback only.")
    serve.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    serve.add_argument(
        "--port",
        type=port_number,
        default=8765,
        help="Loopback TCP port; up also accepts 0 to choose a free port.",
    )
    serve.add_argument("--listen-fd", type=int, help=argparse.SUPPRESS)
    serve.add_argument(
        "--public-gateway",
        action="store_true",
        help="Accept OAuth grants only on this HTTP listener.",
    )
    serve.add_argument(
        "--semantic", action="store_true", help="Enable the prepared local embedding model."
    )
    model = commands.add_parser("model", help="Manage the pinned local embedding model.")
    model.add_argument("action", choices=["prepare"], help="Operation to perform.")
    index = commands.add_parser("index", help="Index pending memories with the local model.")
    index.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry failed embeddings for their current revision.",
    )
    export = commands.add_parser("export", help="Export memories and revisions as JSON Lines.")
    export.add_argument(
        "--output", required=True, type=Path, help="New output file; existing files are preserved."
    )
    for name in ("bridge", "verify"):
        command = commands.add_parser(name, help="Use an existing credential file; never print it.")
        command.add_argument(
            "--credential-file", required=True, type=Path, help="Private connection JSON file."
        )
        if name == "bridge":
            command.add_argument(
                "--local-data-dir",
                type=Path,
                help="Resume only the managed local instance matching this credential.",
            )
        else:
            command.add_argument(
                "--write",
                action="store_true",
                help="Write, read and remove a synthetic probe; requires read/write/forget.",
            )
            command.add_argument(
                "--project",
                default="default",
                help="Project boundary; only explicitly granted projects are accessible.",
            )
    client = commands.add_parser("client", help="Manage scoped credentials on the memory host.")
    actions = client.add_subparsers(dest="client_action", required=True)
    create = actions.add_parser("create")
    create.add_argument("--name", required=True, help="Human-readable credential label.")
    create.add_argument(
        "--scope",
        action="append",
        choices=sorted(SCOPES),
        help="Allowed capability; repeat for each scope.",
    )
    create.add_argument(
        "--project",
        action="append",
        help="Project boundary; only explicitly granted projects are accessible.",
    )
    create.add_argument(
        "--days", type=int, default=30, help="Credential lifetime in days (default: 30)."
    )
    create.add_argument(
        "--credential-file", required=True, type=Path, help="Private connection JSON file."
    )
    create.add_argument("--url", help="MCP URL; defaults to the recorded managed service endpoint.")
    revoke = actions.add_parser("revoke")
    revoke.add_argument("client_id", help="Local credential ID to revoke.")
    actions.add_parser("list")


def run(args) -> dict:
    # Host inventory and remote clients must not resolve or initialize a local store.
    if args.command == "preflight":
        from .runtime import preflight

        return preflight(args.network_policy)
    if args.command == "providers":
        from .clients import providers

        return providers()
    if args.command in ("bridge", "verify"):
        from .bridge import resume_local_connection, run_bridge, verify_connection

        if args.command == "bridge":
            if args.local_data_dir is not None:
                resume_local_connection(args.credential_file, args.local_data_dir)
            return asyncio.run(run_bridge(args.credential_file))
        return asyncio.run(
            verify_connection(args.credential_file, write=args.write, project=args.project)
        )
    if args.command == "capture":
        from .capture_delivery import collect_remote

        return asyncio.run(
            collect_remote(
                paths=args.path,
                cursor=args.cursor,
                credential=args.credential_file,
                project=args.project,
                account=args.account,
                kind=args.kind,
                recover_pending=args.recover_pending,
            )
        )
    store = Store(args.data_dir or default_directory())
    if args.command in {
        "migrate",
        "backup",
        "restore",
        "activate-restore",
        "abort-restore",
        "cortex",
    }:
        from .operator_cli import run_storage

        return run_storage(store, args)
    if args.command == "audit":
        from .activity import AuditLog
        from .auth import Policy

        audit = AuditLog(store)
        arguments = dict(
            project=args.project,
            since=args.since,
            until=args.until,
            after_id=args.after_id,
            limit=args.limit,
        )
        if args.action == "report":
            return audit.report(Policy(), **arguments)
        return audit.page(Policy(), **arguments)
    if args.command == "oauth":
        from .operator_cli import run_oauth

        return run_oauth(store, args)
    if args.command in ("disconnect", "uninstall"):
        from .removal import disconnect_client, uninstall

        return (
            disconnect_client(
                store, provider=args.provider, config=args.config, dry_run=args.dry_run
            )
            if args.command == "disconnect"
            else uninstall(store, dry_run=args.dry_run, extra_configs=args.config)
        )
    if args.command == "connect":
        from .clients import connect_client

        return connect_client(
            store,
            provider=args.provider,
            config=args.config,
            connection=args.credential_file,
            projects=args.project,
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
        return {
            "state": "local_ready",
            "version": __version__,
            "data_dir": str(store.directory),
            "read": "available",
            "write": "available",
            "semantic": "not_enabled",
            "remote_connection": "not_verified",
            "capture": "opt_in_snapshot_collector",
        }
    if args.command == "doctor":
        from .operator_cli import doctor

        return doctor(store)
    if args.command == "serve":
        from .operator_cli import run_serve

        return run_serve(store, args)
    if args.command == "model":
        from .semantic import prepare_model

        return prepare_model(store)
    if args.command == "index":
        from .semantic import SemanticIndex

        semantic = SemanticIndex(store)
        if args.retry_failed:
            semantic.retry_failed()
        indexed = 0
        while True:
            result = semantic.index()
            indexed += result["indexed_now"]
            if result["examined"] == 0:
                return {"indexed_this_run": indexed, **semantic.status()}
    if args.command == "export":
        from .operator_cli import export_store

        return export_store(store, args.output)
    if args.command == "client":
        from .auth import issue_client, list_clients, revoke_client
        from .operator_cli import client_url

        if args.client_action == "create":
            return issue_client(
                store,
                name=args.name,
                scopes=args.scope or ["memory:read"],
                projects=args.project,
                days=args.days,
                output=args.credential_file,
                url=client_url(store, args.url),
            )
        if args.client_action == "revoke":
            return revoke_client(store, args.client_id)
        return {"clients": list_clients(store)}
    raise InputError("Unknown internal command.")


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        result = run(parser().parse_args())
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
            if result.get("state") in {
                "verification_failed",
                "blocked",
                "partial",
                "capture_partial",
                "capture_recovery_required",
            }:
                raise SystemExit(1)
    except Exception as exc:
        from .protocol import safe_error

        error = safe_error(exc)
        print(json.dumps({"state": "error", **error}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
