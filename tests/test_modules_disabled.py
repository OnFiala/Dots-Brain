"""Core remains usable while the preserved CORTEX/capture modules are disabled."""

import asyncio
import builtins
import importlib.util
import inspect
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

import dots_brain
from dots_brain import cli, server
from dots_brain.auth import SCOPES, BearerAuth, Policy, issue_client
from dots_brain.errors import CapabilityError
from dots_brain.migrations import migrate_v1_to_v2
from dots_brain.service import MemoryService
from dots_brain.store import Store

REPOSITORY = Path(__file__).resolve().parents[1]
CORE_SOURCE = Path(dots_brain.__file__).resolve().parents[1]
OPTIONAL_IMPORTS = (
    "dots_brain.capture",
    "dots_brain.capture_delivery",
    "dots_brain.cortex_connector",
    "dots_brain_cortex",
    "dots_brain_capture",
)
CORE_TOOLS = {
    "memory_context",
    "memory_search",
    "memory_get",
    "memory_remember",
    "memory_forget",
    "memory_status",
    "audit_record",
    "audit_events",
    "audit_report",
}


def _snapshot(directory):
    files = {
        path.relative_to(directory): (
            path.read_bytes(),
            path.stat().st_mode,
            path.stat().st_mtime_ns,
        )
        for path in directory.rglob("*")
        if path.is_file()
    }
    directories = {path.relative_to(directory) for path in directory.rglob("*") if path.is_dir()}
    return files, directories


def _legacy_files(directory, *, malformed=False):
    files = {
        "cortex.token": "SYNTHETIC_MODULE_TOKEN\n",
        "capture/cursor.json": '{"offset": 17}\n',
        "capture/cursor.coverage.json": '{"pending": "synthetic"}\n',
        "capture/cursor.delivery.lock": "preserved legacy lock\n",
        "capture/provider.jsonl": '{"role": "user", "content": "Synthetic source"}\n',
        "capture/credential.json": '{"token": "SYNTHETIC_CAPTURE_TOKEN"}\n',
        "cortex.json": "invalid legacy JSON {"
        if malformed
        else json.dumps(
            {
                "endpoint": "http://127.0.0.1:9/mcp",
                "token_file": str(directory / "cortex.token"),
                "project_mapping": {"alpha": "mapped-alpha"},
            }
        ),
    }
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o600)
    return [directory / name for name in files]


@contextmanager
def _module_guard(monkeypatch, protected=()):
    """Trap only optional paths/imports and network; core files/SQLite remain available."""
    attempts = []
    paths = [os.path.abspath(os.fspath(path)) for path in protected]
    paths.append(str(REPOSITORY / "modules"))
    paths.extend(str(CORE_SOURCE / name.replace(".", "/")) + ".py" for name in OPTIONAL_IMPORTS[:3])

    def forbidden(kind):
        attempts.append(kind)
        raise AssertionError(f"Disabled module attempted {kind}")

    def optional_path(path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return False
        candidate = os.path.abspath(os.fsdecode(path))
        return any(candidate == item or candidate.startswith(item + os.sep) for item in paths)

    def wrap_file(function):
        def guarded(path, *args, **kwargs):
            if optional_path(path):
                forbidden("optional file access")
            return function(path, *args, **kwargs)

        return guarded

    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        absolute = (
            importlib.util.resolve_name("." * level + name, globals["__package__"])
            if level
            else name
        )
        requested = [absolute, *(absolute + "." + item for item in fromlist or ())]
        if any(
            request == item or request.startswith(item + ".")
            for request in requested
            for item in OPTIONAL_IMPORTS
        ):
            forbidden("optional import")
        return original_import(name, globals, locals, fromlist, level)

    original_socket_init = socket.socket.__init__

    def guarded_socket_init(
        self, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0, fileno=None
    ):
        if family in {socket.AF_INET, socket.AF_INET6}:
            forbidden("network socket")
        original_socket_init(self, family, type, proto, fileno)

    with monkeypatch.context() as patches:
        for module, name in (
            (builtins, "open"),
            (io, "open"),
            (os, "open"),
            (os, "stat"),
            (os, "lstat"),
            (os, "mkdir"),
        ):
            patches.setattr(module, name, wrap_file(getattr(module, name)))
        patches.setattr(builtins, "__import__", guarded_import)
        patches.setattr(socket.socket, "__init__", guarded_socket_init)
        for module, name in (
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket.socket, "bind"),
        ):
            patches.setattr(module, name, lambda *args, **kwargs: forbidden("network operation"))
        yield attempts
    assert attempts == [], "An optional side effect was attempted, even if its error was caught."


def _disabled_arguments(command, directory):
    if command == "cortex":
        return [
            "--data-dir",
            str(directory),
            "cortex",
            "--endpoint",
            "http://127.0.0.1:9/mcp",
            "--token-file",
            str(directory / "cortex.token"),
            "--project-map",
            "alpha=mapped-alpha",
        ]
    return [
        "--data-dir",
        str(directory),
        "capture",
        "--kind",
        "transcript",
        "--path",
        str(directory / "capture/provider.jsonl"),
        "--cursor",
        str(directory / "capture/cursor.json"),
        "--credential-file",
        str(directory / "capture/credential.json"),
        "--project",
        "alpha",
        "--account",
        "synthetic",
    ]


def _intercept_stdio(monkeypatch):
    services, runs = [], []
    original = server.create_server

    def construct(service, **kwargs):
        services.append(service)
        return original(service, **kwargs)

    monkeypatch.setattr(server, "create_server", construct)
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: runs.append(kwargs))
    return services, runs


def test_core_imports_without_optional_packages_in_an_isolated_process(tmp_path):
    # This proves the source import boundary. Installed wheel/sdist checks are separate.
    code = """
import importlib
import importlib.util
import sys
sys.path.insert(0, sys.argv[1])
for name in ("cli", "store", "service", "server", "operations", "migrations"):
    importlib.import_module("dots_brain." + name)
for name in sys.argv[2:]:
    assert importlib.util.find_spec(name) is None, name
assert not any(name.startswith(("dots_brain_cortex", "dots_brain_capture")) for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(CORE_SOURCE), *OPTIONAL_IMPORTS],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("command", ["capture", "cortex"])
@pytest.mark.parametrize("existing", [False, True], ids=["absent-state", "existing-state"])
def test_disabled_cli_rejects_before_locks_store_inputs_imports_or_network(
    tmp_path, monkeypatch, capsys, command, existing
):
    directory = tmp_path / "memory"
    if existing:
        Store(directory).initialize()
        _legacy_files(directory)
    before = _snapshot(tmp_path)
    arguments = _disabled_arguments(command, directory)
    parsed = cli.parser().parse_args(arguments)

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled CLI reached a store, writer lock, or SQLite resource.")

    with _module_guard(monkeypatch, [directory]):
        with monkeypatch.context() as patches:
            patches.setattr(cli, "Store", forbidden)
            patches.setattr(cli, "writer_lock", forbidden)
            patches.setattr(sqlite3, "connect", forbidden)
            with pytest.raises(CapabilityError) as failure:
                cli.run(parsed)
            assert failure.value.code == "capability_unavailable"
            assert "disabled" in str(failure.value).lower()
            patches.setattr(sys, "argv", ["dots-brain", *arguments])
            with pytest.raises(SystemExit) as exited:
                cli.main()
            assert exited.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["code"] == "capability_unavailable"
    assert "SYNTHETIC_" not in output.err
    assert _snapshot(tmp_path) == before
    assert directory.exists() is existing


@pytest.mark.parametrize("malformed", [False, True], ids=["previously-enabled", "malformed"])
def test_core_serve_does_not_consult_legacy_cortex_configuration(tmp_path, monkeypatch, malformed):
    store = Store(tmp_path / "memory")
    store.initialize()
    files = _legacy_files(store.directory, malformed=malformed)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    services, runs = _intercept_stdio(monkeypatch)
    arguments = cli.parser().parse_args(["--data-dir", str(store.directory), "serve"])
    with _module_guard(monkeypatch, files):
        assert cli.run(arguments) is None
        assert services[0].status(policy=Policy())["memories"] == 0
        assert not hasattr(services[0], "cortex")
    assert runs == [{"transport": "stdio"}]
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before


@pytest.mark.parametrize("injected", [False, True], ids=["core-service", "legacy-attribute"])
def test_real_core_mcp_registers_only_core_tools_even_with_legacy_cortex_attribute(
    tmp_path, monkeypatch, injected
):
    store = Store(tmp_path / "memory")
    store.initialize()
    credential = tmp_path / "core-client.json"
    issue_client(
        store,
        name="synthetic",
        scopes=sorted(SCOPES),
        projects=["alpha"],
        days=1,
        output=credential,
        url="http://127.0.0.1:8765/mcp",
    )
    token = json.loads(credential.read_text())["token"]
    assert set(inspect.signature(MemoryService).parameters) == {"store", "semantic"}
    service = MemoryService(store)
    assert not hasattr(service, "cortex")

    class LegacyConnector:
        def __bool__(self):
            pytest.fail("Core consulted the injected legacy CORTEX connector.")

        def __getattr__(self, name):
            pytest.fail("Core used the injected legacy CORTEX connector.")

    if injected:
        service.cortex = LegacyConnector()

    async def exercise():
        mcp = server.create_server(service, http=True)
        app = BearerAuth(mcp.streamable_http_app(), store)
        async with mcp.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + token},
            ) as http:
                async with streamable_http_client(
                    "http://127.0.0.1:8765/mcp", http_client=http
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        assert {
                            item.name for item in (await session.list_tools()).tools
                        } == CORE_TOOLS
                        saved = await session.call_tool(
                            "memory_remember",
                            dict(
                                content="Core-only synthetic fact",
                                source="test",
                                account="synthetic",
                                event_id="one",
                                project="alpha",
                            ),
                        )
                        assert not saved.isError
                        loaded = await session.call_tool(
                            "memory_get", {"memory_id": saved.structuredContent["id"]}
                        )
                        assert loaded.structuredContent["content"] == "Core-only synthetic fact"
                        status = await session.call_tool("memory_status", {})
                        assert not status.isError and status.structuredContent["memories"] == 1
                        for capability in ("capture", "continuous_capture", "history_import"):
                            assert status.structuredContent[capability] == "disabled"
                        assert (
                            status.structuredContent["audit"]["provider_wide_capture"] == "disabled"
                        )
                        audit = await session.call_tool("audit_events", {"project": "alpha"})
                        assert not audit.isError and len(audit.structuredContent["events"]) == 2
                        denied = await session.call_tool(
                            "cortex_context", {"project": "alpha", "query": "fact"}
                        )
                        assert denied.isError

    with _module_guard(monkeypatch):
        asyncio.run(asyncio.wait_for(exercise(), timeout=10))


@pytest.mark.parametrize("operation", ["initialize", "migrate-v1"])
def test_core_setup_and_v1_migration_do_not_install_optional_tables(
    tmp_path, monkeypatch, operation
):
    store = Store(tmp_path / "memory")
    if operation == "migrate-v1":
        store.directory.mkdir()
        shutil.copyfile(
            REPOSITORY / "tests/fixtures/historical/v1-263c386/brain.sqlite3", store.path
        )
    with _module_guard(monkeypatch):
        if operation == "initialize":
            store.initialize()
        else:
            result = migrate_v1_to_v2(
                store,
                apply=True,
                backup_path=store.directory / "before.sqlite3",
                stop_guard=lambda: None,
            )
            assert result["state"] == "migrated"
        with store.connection() as db:
            names = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not any(name.startswith(("cortex_", "capture_")) for name in names)
            assert {"memories", "revisions", "clients", "audit_events"} <= names
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_ordinary_core_use_preserves_legacy_module_files_and_all_ledger_states(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "memory")
    store.initialize()
    # Independent synthetic legacy ledger: core must leave even unknown columns intact.
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE IF EXISTS cortex_operations")
        db.execute(
            "CREATE TABLE cortex_operations "
            "(operation_id TEXT PRIMARY KEY, state TEXT, receipt_json TEXT)"
        )
        db.executemany(
            "INSERT INTO cortex_operations VALUES (?,?,?)",
            [
                (state, state, json.dumps({"synthetic": state}))
                for state in ("planned", "sending", "acknowledged", "uncertain")
            ],
        )
        db.execute("CREATE INDEX legacy_cortex_state ON cortex_operations(state)")
    files = _legacy_files(store.directory)
    before_files = {
        path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) for path in files
    }

    def ledger_snapshot():
        with sqlite3.connect(store.path) as db:
            return (
                db.execute(
                    "SELECT type,name,sql FROM sqlite_master "
                    "WHERE tbl_name='cortex_operations' ORDER BY name"
                ).fetchall(),
                db.execute("SELECT * FROM cortex_operations ORDER BY operation_id").fetchall(),
            )

    before_ledger = ledger_snapshot()
    services, runs = _intercept_stdio(monkeypatch)
    with _module_guard(monkeypatch, files):
        setup = cli.run(cli.parser().parse_args(["--data-dir", str(store.directory), "setup"]))
        assert setup["state"] == "local_ready"
        assert (
            cli.run(cli.parser().parse_args(["--data-dir", str(store.directory), "serve"])) is None
        )
        service = services[0]
        saved = service.mutate(
            store.remember,
            policy=Policy(),
            audit_project="alpha",
            action="memory_remember",
            content="Ordinary synthetic memory",
            source="test",
            account="synthetic",
            event_id="one",
            project="alpha",
        )
        assert (
            service.search("Ordinary", policy=Policy(), project="alpha")["results"][0]["id"]
            == saved["id"]
        )
        assert store.get(saved["id"])["content"] == "Ordinary synthetic memory"
        removed = service.mutate(
            store.forget,
            policy=Policy(),
            audit_project="alpha",
            action="memory_forget",
            memory_id=saved["id"],
            expected_revision=1,
        )
        assert removed["deleted"] is True
        assert service.status(policy=Policy())["memories"] == 0
    assert runs == [{"transport": "stdio"}]
    assert ledger_snapshot() == before_ledger
    assert {
        path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) for path in files
    } == before_files
