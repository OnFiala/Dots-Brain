"""Small deterministic commands for operators and installation agents."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .errors import BrainError
from .store import Store


def default_directory() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "dots-brain"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="One source-aware memory for your AI tools.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--data-dir", type=Path, default=default_directory())
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Initialize or reuse the local memory database.")
    commands.add_parser(
        "doctor", help="Report local capabilities without assuming remote readiness."
    )
    serve = commands.add_parser("serve", help="Run on the memory host; HTTP binds loopback only.")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--semantic", action="store_true")
    model = commands.add_parser("model", help="Manage the pinned local embedding model.")
    model.add_argument("action", choices=["prepare"])
    commands.add_parser("index", help="Index pending memories with the local model.")
    export = commands.add_parser("export", help="Export memories and revisions as JSON Lines.")
    export.add_argument("--output", required=True, type=Path)
    for name in ("bridge", "verify"):
        command = commands.add_parser(name, help="Use an existing credential file; never print it.")
        command.add_argument("--credential-file", required=True, type=Path)
    client = commands.add_parser("client", help="Manage scoped credentials on the memory host.")
    actions = client.add_subparsers(dest="client_action", required=True)
    create = actions.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument(
        "--scope", action="append", choices=["memory:read", "memory:write", "memory:forget"]
    )
    create.add_argument("--project", action="append")
    create.add_argument("--days", type=int, default=30)
    create.add_argument("--credential-file", required=True, type=Path)
    create.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    revoke = actions.add_parser("revoke")
    revoke.add_argument("client_id")
    actions.add_parser("list")
    return root


def run(args) -> dict | None:
    store = Store(args.data_dir)
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
            "capture": "not_implemented",
        }
    if args.command == "doctor":
        initialized = store.path.is_file()
        return {
            "state": "local_ready" if initialized else "setup_required",
            "version": __version__,
            "sqlite_version": sqlite3.sqlite_version,
            "memory": store.status() if initialized else None,
            "vm_persistence": "not_verified",
            "public_ingress": "not_verified",
            "secret_isolation": False,
            "remote_oauth": "not_implemented",
            "host_costs": "not_verified",
            "event_automation": "not_implemented",
        }
    if args.command in ("bridge", "verify"):
        from .bridge import run_bridge, verify_connection

        return asyncio.run(
            (run_bridge if args.command == "bridge" else verify_connection)(args.credential_file)
        )
    if args.command == "serve":
        from .server import create_http_app, create_server
        from .service import MemoryService

        store.status()  # Fail before starting if setup has not completed.
        semantic = None
        if args.semantic:
            from .semantic import SemanticIndex

            semantic = SemanticIndex(store)
        service = MemoryService(store, semantic)
        server = create_server(service, http=args.transport == "http", port=args.port)
        if args.transport == "http":
            import uvicorn

            uvicorn.run(
                create_http_app(server, service),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
                log_level="warning",
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
            indexed += result["indexed"]
            if result["examined"] == 0:
                return {"indexed_this_run": indexed, **semantic.status()}
    if args.command == "export":
        output = args.output.expanduser().absolute()
        fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        count = 0
        try:
            with os.fdopen(fd, "w") as stream:
                for record in store.export():
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += 1
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise
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
    try:
        result = run(parser().parse_args())
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
    except BrainError as exc:
        print(
            json.dumps({"state": "error", "code": exc.code, "message": str(exc)}), file=sys.stderr
        )
        raise SystemExit(1) from None
    except (OSError, ValueError, sqlite3.Error):
        # Connection and OS errors can contain secret URLs or local paths. Keep them out
        # of machine output; diagnose credentials and permissions without dumping inputs.
        print(
            json.dumps(
                {
                    "state": "error",
                    "code": "operation_failed",
                    "message": "Check local permissions, connectivity, and command arguments.",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
