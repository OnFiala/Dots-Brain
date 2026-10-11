"""Neutral operator recovery and legacy-client lifecycle regressions."""

import json
import shlex
import shutil
from pathlib import Path

import pytest
import tomlkit

from dots_brain.auth import authenticate, issue_client, read_connection
from dots_brain.cli import parser, run
from dots_brain.clients import bridge_entry, configure, connect_client, integration_key
from dots_brain.errors import BrainError
from dots_brain.installation_state import mark_uninstalled, resume_uninstalled, write_marker
from dots_brain.local import locked, write_json
from dots_brain.operations import migrate, restore_store
from dots_brain.operator_cli import doctor
from dots_brain.removal import uninstall
from dots_brain.store import Store


@pytest.mark.parametrize(
    ("arguments", "option"),
    [
        (["serve", "--port", "private-value"], "--port"),
        (["serve", "--transport", "private-value"], "--transport"),
        (["up", "--port", "70000"], "--port"),
        (["backup"], "--output"),
        (["restore", "--backup", "fixture.sqlite3"], "--target"),
        (["client", "create", "--name"], "--name"),
        (["client", "create", "--scope", "private-value"], "--scope"),
        (["audit", "events", "--limit", "private-value"], "--limit"),
    ],
)
def test_parser_diagnostic_names_known_option_without_echoing_value(arguments, option, capsys):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(arguments)
    assert error.value.code == 2
    result = json.loads(capsys.readouterr().err)
    assert option in result["message"]
    assert "private-value" not in result["message"]
    command = shlex.split(result["message"].split("Next command: ")[1])
    assert command[0] == "dots-brain" and command[-1] == "--help"
    with pytest.raises(SystemExit) as help_exit:
        parser().parse_args(command[1:])
    assert help_exit.value.code == 0


def test_unknown_parser_value_is_not_reflected(capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["setup", "--unknown=private-value"])
    assert "private-value" not in capsys.readouterr().err


def synthetic_legacy(store, path, provider):
    store.initialize()
    connection = store.directory / "connections" / f"{provider}.json"
    connection.parent.mkdir()
    client_id = issue_client(
        store,
        name="fixture",
        scopes=["memory:read", "memory:write"],
        projects=None,
        days=1,
        output=connection,
        url="http://127.0.0.1:8765/mcp",
    )["client_id"]
    entry = bridge_entry(connection, store.directory, provider)
    configure(path, provider=provider, entry=entry)
    return connection, entry, client_id


@pytest.mark.parametrize("provider", ["cursor", "codex", "claude-code", "mcp-json"])
def test_unregistered_legacy_client_reports_file_then_manual_cleanup_allows_connect(
    tmp_path, monkeypatch, provider
):
    store = Store(tmp_path / "data 'quoted'")
    path = tmp_path / "config 'quoted'"
    connection, entry, _ = synthetic_legacy(store, path, provider)
    original = path.read_bytes()
    credential_bytes = connection.read_bytes()
    calls = []

    def start(*args, **kwargs):
        calls.append("start")
        return {"url": "http://127.0.0.1:8765/mcp"}

    async def verified(*args, **kwargs):
        calls.append("verify")
        return {"state": "verified_read_write", "read": True, "write": True}

    monkeypatch.setattr("dots_brain.clients.up", start)
    monkeypatch.setattr("dots_brain.clients.verify_command", verified)
    with pytest.raises(BrainError) as error:
        connect_client(store, provider=provider, config=path)
    assert str(path) in str(error.value)
    assert "Manually remove only" in str(error.value)
    assert "0.2.0-alpha.1" in str(error.value)
    assert not calls
    assert path.read_bytes() == original
    assert connection.read_bytes() == credential_bytes
    assert not (store.directory / "integrations.json").exists()

    document = (
        tomlkit.parse(path.read_text()) if provider == "codex" else json.loads(path.read_text())
    )
    section = "mcp_servers" if provider == "codex" else "mcpServers"
    document[section]["unrelated"] = {"command": "fixture"}
    del document[section]["dots-brain"]
    path.write_text(tomlkit.dumps(document) if provider == "codex" else json.dumps(document))
    command = shlex.split(str(error.value).split("then run ")[1].split(". The previous")[0])
    result = run(parser().parse_args(command[1:]))
    assert result["state"] == "configured_verified_bridge"
    assert calls == ["start", "verify"]
    updated = (
        tomlkit.parse(path.read_text()) if provider == "codex" else json.loads(path.read_text())
    )
    assert updated[section]["unrelated"] == {"command": "fixture"}
    assert updated[section]["dots-brain"] != entry
    assert connection.read_bytes() == credential_bytes


def test_registered_v03_client_with_legacy_filename_is_preserved(tmp_path, monkeypatch):
    store, path = Store(tmp_path / "data"), tmp_path / "config.json"
    connection, entry, client_id = synthetic_legacy(store, path, "cursor")
    before = connection.read_bytes()
    key = integration_key("cursor", path)
    write_json(
        store.directory / "integrations.json",
        {
            "version": 1,
            "items": {
                key: {
                    "provider": "cursor",
                    "config_file": str(path),
                    "connection_file": str(connection),
                    "client_id": client_id,
                    "entry": entry,
                    "local": True,
                }
            },
        },
    )
    monkeypatch.setattr("dots_brain.clients.up", lambda *_: {"url": "http://127.0.0.1:8765/mcp"})

    async def verified(*args, **kwargs):
        return {"state": "verified_read", "read": True, "write": "not_tested"}

    monkeypatch.setattr("dots_brain.clients.verify_command", verified)
    result = connect_client(store, provider="cursor", config=path)
    assert result["client_id"] == client_id
    assert connection.read_bytes() == before
    assert authenticate(store, read_connection(connection)["token"]) is not None


@pytest.mark.parametrize("operation", ["initialize", "write"])
def test_disabled_store_names_resume_command_and_can_resume(tmp_path, operation):
    store = Store(tmp_path / "data 'quoted'")
    store.initialize()
    mark_uninstalled(store)
    with pytest.raises(BrainError) as error:
        if operation == "initialize":
            store.initialize()
        else:
            store.remember(
                content="neutral text",
                project="fixture",
                source="fixture",
                account="fixture",
                event_id="fixture",
            )
    command = shlex.split(str(error.value).split("Next command: ")[1])
    args = parser().parse_args(command[1:])
    assert args.command == "up" and args.resume and args.data_dir == store.directory
    with resume_uninstalled(store, requested=args.resume):
        store.initialize()
        result = store.remember(
            content="neutral text",
            project="fixture",
            source="fixture",
            account="fixture",
            event_id="fixture",
        )
    assert store.get(result["id"])["content"] == "neutral text"


def test_recovery_marker_names_doctor_without_suggesting_uninstall_resume(tmp_path):
    store = Store(tmp_path / "data")
    store.initialize()
    write_marker(store, reason="restored_requires_review")
    with pytest.raises(BrainError) as error:
        store.initialize()
    command = shlex.split(str(error.value).split("Next command: ")[1])
    assert "--resume" not in command
    result = run(parser().parse_args(command[1:]))
    assert result["service_disabled"] and result["healthy"] is False


@pytest.mark.parametrize("marker_kind", ["malformed", "invalid-utf8", "array", "unreadable"])
def test_invalid_disable_marker_preserves_barrier_and_names_executable_doctor(
    tmp_path, monkeypatch, marker_kind
):
    store = Store(tmp_path / "data 'quoted'")
    store.initialize()
    marker = store.directory / "disabled.json"
    payload = {
        "malformed": b'{ "private-fixture":',
        "invalid-utf8": b"\xffprivate-fixture",
        "array": b'["private-fixture"]',
        "unreadable": b'{"version":1,"reason":"uninstalled"}',
    }[marker_kind]
    marker.write_bytes(payload)
    database_before = store.path.read_bytes()
    if marker_kind == "unreadable":

        def unreadable(_):
            raise PermissionError("private-fixture")

        monkeypatch.setattr("dots_brain.installation_state.read_json", unreadable)
    with pytest.raises(BrainError) as error:
        store.initialize()
    assert error.value.code == "store_disabled"
    assert str(marker) in str(error.value) and "private-fixture" not in str(error.value)
    command = shlex.split(str(error.value).split("Next command: ")[1])
    assert command[-1] == "doctor"
    result = run(parser().parse_args(command[1:]))
    assert result["service_disabled"] and result["healthy"] is False
    assert marker.read_bytes() == payload and store.path.read_bytes() == database_before


@pytest.mark.parametrize("fixture", ["v1-263c386", "v2-c963700"])
def test_historical_backup_diagnostic_has_executable_copy_upgrade_route(tmp_path, fixture):
    store = Store(tmp_path / "surviving")
    store.initialize()
    backup = tmp_path / "old 'backup'.sqlite3"
    shutil.copyfile(
        Path(__file__).parent / "fixtures/historical" / fixture / "brain.sqlite3", backup
    )
    original = backup.read_bytes()
    target = Store(tmp_path / "restored 'copy'")
    with pytest.raises(BrainError) as error:
        restore_store(backup, target, latest_deletions=store)
    assert "Backup uses schema" in str(error.value)
    assert backup.read_bytes() == original and not target.directory.exists()
    command = shlex.split(
        str(error.value).split("Next command for the copy: ")[1].split(". Then retry")[0]
    )
    args = parser().parse_args(command[1:])
    args.data_dir.mkdir(mode=0o700)
    working_backup = args.data_dir / "brain.sqlite3"
    shutil.copyfile(backup, working_backup)
    assert run(args)["state"] == "migrated"
    result = restore_store(working_backup, target, latest_deletions=store)
    assert result["state"] == "restored_disabled"
    assert backup.read_bytes() == original


def test_doctor_does_not_call_an_unmanaged_writer_healthy(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    store.initialize()
    monkeypatch.setattr(
        "dots_brain.runtime.unmanaged_writers",
        lambda _: [{"pid": 4242, "stop_command": "kill -TERM 4242"}],
        raising=False,
    )
    result = doctor(store)
    assert result["healthy"] is False
    assert result["checks"]["managed_service"]["state"] == "unmanaged_writers"


def test_migration_refuses_detected_unmanaged_writer_without_changing_database(
    tmp_path, monkeypatch
):
    directory = tmp_path / "historical"
    shutil.copytree(Path(__file__).parent / "fixtures/historical/v1-263c386", directory)
    store = Store(directory)
    before = store.path.read_bytes()
    monkeypatch.setattr(
        "dots_brain.runtime.unmanaged_writers",
        lambda _: [{"pid": 4242, "stop_command": "kill -TERM 4242"}],
        raising=False,
    )
    with pytest.raises(BrainError) as error:
        migrate(store, apply=True, writers_stopped=True, backup=None)
    assert "4242" in str(error.value) and "kill -TERM 4242" in str(error.value)
    assert store.path.read_bytes() == before
    assert not list(directory.glob("*.v1-backup-*"))


def test_migration_takes_exclusive_writer_lease_until_migration_finishes(tmp_path):
    """Baseline control; specifically catches removal of the existing migrate lease."""
    directory = tmp_path / "historical"
    shutil.copytree(Path(__file__).parent / "fixtures/historical/v1-263c386", directory)
    store = Store(directory)
    before = store.path.read_bytes()
    with locked(directory / "writers.lock", timeout=0, shared=True):
        with pytest.raises(BrainError):
            migrate(store, apply=True, writers_stopped=True, backup=None)
        assert store.path.read_bytes() == before
    assert migrate(store, apply=True, writers_stopped=True, backup=None)["state"] == "migrated"


def test_uninstall_reports_detected_unmanaged_writer_as_remaining(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    store.initialize()
    monkeypatch.setattr(
        "dots_brain.removal.unmanaged_writers",
        lambda _: [{"pid": 4242, "stop_command": "kill -TERM 4242"}],
        raising=False,
    )
    result = uninstall(store)
    assert result["state"] == "partial"
    assert result["managed_process_stopped"] is False
    assert any(issue.get("next_command") == "kill -TERM 4242" for issue in result["issues"])


def test_uninspectable_http_lease_is_unknown_instead_of_stopped(tmp_path, monkeypatch):
    runtime = pytest.importorskip("dots_brain.http_runtime")
    store = Store(tmp_path / "data")
    store.initialize()
    monkeypatch.setattr(
        "dots_brain.http_runtime.lock_status", lambda _: {"state": "unknown", "owners": []}
    )
    with runtime.record_listeners(store, runtime.ListenerPlan(8765), None):
        result = doctor(store)
        status = result["checks"]["http_listeners"]
        assert status["state"] == "unknown"
        assert status["process_verified"] is False
        assert status["next_command"].startswith("lsof ")
        assert result["healthy"] is False
