"""Operator error taxonomy, recovery, and a bounded OAuth route factory."""

import ast
import inspect
import json
import shlex
import socket
from pathlib import Path

import pytest

from dots_brain import runtime
from dots_brain.auth import read_connection, revoke_client
from dots_brain.cli import parser, run
from dots_brain.clients import configure, connect_client, preflight_configure, registrations
from dots_brain.errors import BrainError
from dots_brain.oauth_http import routes_app
from dots_brain.protocol import safe_error
from dots_brain.store import Store


def test_oauth_route_factory_only_assembles_handlers_and_middleware():
    """Structural acceptance only; existing transport tests prove behavior."""
    source = inspect.getsource(routes_app)
    tree = ast.parse(source)
    assert len(source.splitlines()) <= 80
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for statement in tree.body[0].body
        for node in ast.walk(statement)
    )


def test_unsupported_background_platform_is_a_capability_error_with_help(
    tmp_path, monkeypatch, capsys
):
    store = Store(tmp_path / "data 'quoted'")
    monkeypatch.setattr(runtime.sys, "platform", "unsupported-fixture")
    with pytest.raises(BrainError) as error:
        runtime.up(store)
    assert safe_error(error.value)["code"] == "capability_unavailable"
    command = shlex.split(str(error.value).split("Next command: ")[1])
    with pytest.raises(SystemExit) as help_exit:
        parser().parse_args(command[1:])
    assert help_exit.value.code == 0
    assert "--transport" in capsys.readouterr().out
    assert not store.directory.exists()


def test_invalid_port_remains_an_input_error_without_creating_state(tmp_path, monkeypatch):
    store = Store(tmp_path / "absent")
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    with pytest.raises(BrainError) as error:
        runtime.up(store, port=65536)
    assert safe_error(error.value)["code"] == "invalid_input"
    assert not store.directory.exists()


def test_occupied_port_is_busy_and_becomes_available_after_owner_closes_it():
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        port = owner.getsockname()[1]
        with pytest.raises(BrainError) as error:
            with runtime.reserve_listener(port, {}):
                pytest.fail("Occupied port was acquired")
        assert safe_error(error.value)["code"] == "busy"
        command = shlex.split(str(error.value).split("Next command: ")[1])
        assert command == ["lsof", "-nP", f"-iTCP:{port}"]
    with runtime.reserve_listener(port, {}) as listener:
        assert listener.getsockname()[1] == port


def test_revoked_existing_credential_is_state_error_and_retains_file(tmp_path):
    store = Store(tmp_path / "data 'quoted'")
    store.initialize()
    credential_file = tmp_path / "client.json"
    options = dict(name="synthetic", path=credential_file, url="http://127.0.0.1:8765/mcp")
    client_id = runtime.credential(store, **options)
    revoke_client(store, client_id)
    previous = credential_file.read_bytes()
    with pytest.raises(BrainError) as error:
        runtime.credential(store, **options)
    assert safe_error(error.value)["code"] == "invalid_state"
    assert credential_file.read_bytes() == previous
    command = shlex.split(str(error.value).split("Next command: ")[1])
    result = run(parser().parse_args(command[1:]))
    assert any(client["id"] == client_id and client["revoked"] for client in result["clients"])
    archived = credential_file.with_suffix(".archived")
    credential_file.rename(archived)
    replacement = runtime.credential(store, **options)
    assert replacement != client_id and archived.read_bytes() == previous


@pytest.mark.parametrize("provider", ["cursor", "codex"])
@pytest.mark.parametrize("failure", ["syntax", "section", "conflict", "symlink"])
@pytest.mark.parametrize("operation", ["configure", "preflight"])
def test_existing_client_config_problems_are_state_errors_and_recover_without_loss(
    tmp_path, provider, failure, operation, capsys
):
    path = tmp_path / "config 'quoted'"
    other = tmp_path / "unrelated"
    other.write_text("unrelated data")
    if failure == "symlink":
        path.symlink_to(other)
        before = None
    else:
        if provider == "cursor":
            value = {
                "syntax": '{"private-fixture":',
                "section": '{"mcpServers": false}',
                "conflict": '{"mcpServers":{"dots-brain":{"command":"existing"}}}',
            }[failure]
        else:
            value = {
                "syntax": 'private_fixture = "',
                "section": "mcp_servers = false",
                "conflict": '[mcp_servers.dots-brain]\ncommand = "existing"\n',
            }[failure]
        path.write_text(value)
        before = path.read_bytes()
    action = configure if operation == "configure" else preflight_configure
    entry = {"command": "synthetic"}
    with pytest.raises(BrainError) as error:
        action(path, provider=provider, entry=entry)
    assert safe_error(error.value)["code"] == "invalid_state"
    assert str(path) in str(error.value) and "private-fixture" not in str(error.value)
    assert other.read_text() == "unrelated data"
    if failure == "symlink":
        assert path.is_symlink()
        path.unlink()
    else:
        assert path.read_bytes() == before
    command = shlex.split(str(error.value).split("Next command: ")[1])
    with pytest.raises(SystemExit) as help_exit:
        parser().parse_args(command[1:])
    assert help_exit.value.code == 0
    capsys.readouterr()
    # Simulate the owner's explicit repair of only the selected configuration.
    path.write_text("{}" if provider == "cursor" else "")
    action(path, provider=provider, entry=entry)
    assert other.read_text() == "unrelated data"


def test_uninitialized_client_host_has_state_code_and_scoped_setup_command(tmp_path):
    store = Store(tmp_path / "data 'quoted'")
    with pytest.raises(BrainError) as error:
        connect_client(store, provider="cursor", config=tmp_path / "client.json")
    assert safe_error(error.value)["code"] == "invalid_state"
    command = shlex.split(str(error.value).split("Next command: ")[1])
    args = parser().parse_args(command[1:])
    assert args.command == "setup" and args.data_dir == store.directory
    assert not store.directory.exists()


def test_unsupported_registry_is_state_error_with_preserved_file(tmp_path):
    store = Store(tmp_path / "data 'quoted'")
    store.initialize()
    path = store.directory / "integrations.json"
    original = json.dumps({"version": 99, "items": {}}).encode()
    path.write_bytes(original)
    with pytest.raises(BrainError) as error:
        registrations(store.directory)
    assert safe_error(error.value)["code"] == "invalid_state"
    assert str(path) in str(error.value)
    command = shlex.split(str(error.value).split("Next command: ")[1])
    args = parser().parse_args(command[1:])
    assert args.command == "doctor" and args.data_dir == store.directory
    assert path.read_bytes() == original


@pytest.mark.parametrize("original", [b'{"private_fixture":', b"[]", b"\xff"])
def test_corrupt_managed_registry_has_state_error_and_backup_recovery(tmp_path, original):
    store = Store(tmp_path / "data 'quoted'")
    store.initialize()
    path = store.directory / "integrations.json"
    known_good = b'{"version": 1, "items": {}}'
    backup = store.directory / "integrations.known-good.json"
    backup.write_bytes(known_good)
    path.write_bytes(original)
    with pytest.raises(BrainError) as error:
        registrations(store.directory)
    assert safe_error(error.value)["code"] == "invalid_state"
    assert str(path) in str(error.value) and "private_fixture" not in str(error.value)
    assert path.read_bytes() == original and backup.read_bytes() == known_good
    command = shlex.split(str(error.value).split("Next command: ")[1])
    diagnostic = run(parser().parse_args(command[1:]))
    assert isinstance(diagnostic, dict)
    path.write_bytes(backup.read_bytes())
    assert registrations(store.directory) == {"version": 1, "items": {}}


def test_corrupt_managed_credential_has_state_error_and_preserving_replacement(tmp_path):
    store = Store(tmp_path / "data 'quoted'")
    store.initialize()
    path = store.directory / "client 'quoted'.json"
    original = b'{"private_fixture":'
    path.write_bytes(original)
    options = dict(name="synthetic", path=path, url="http://127.0.0.1:8765/mcp")
    with pytest.raises(BrainError) as error:
        runtime.credential(store, **options)
    assert safe_error(error.value)["code"] == "invalid_state"
    assert str(path) in str(error.value) and "private_fixture" not in str(error.value)
    assert path.read_bytes() == original
    command = shlex.split(str(error.value).split("Next command: ")[1])
    assert run(parser().parse_args(command[1:])) == {"clients": []}
    backup = Path(str(path) + ".before-replacement")
    path.rename(backup)
    client_id = runtime.credential(store, **options)
    assert read_connection(path)["client_id"] == client_id
    assert backup.read_bytes() == original


def test_explicit_invalid_external_credential_remains_input_error(tmp_path):
    path = tmp_path / "external.json"
    path.write_text("[]")
    with pytest.raises(BrainError) as error:
        read_connection(path)
    assert safe_error(error.value)["code"] == "invalid_input"
