"""Operator boundaries use disposable paths and mocked installation commands only."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from dots_brain import cli

PRIVATE = "private-rejected-value"


@pytest.mark.parametrize(
    "arguments",
    [
        [PRIVATE],
        ["connect", PRIVATE],
        ["disconnect", PRIVATE],
        ["up", "--port", PRIVATE],
        ["setup", "--" + PRIVATE],
        ["model", PRIVATE],
        ["oauth", "approve", "request", "--project", "test", "--scope", PRIVATE],
    ],
)
def test_parser_rejects_arguments_without_echo_or_side_effects(
    tmp_path, monkeypatch, capsys, arguments
):
    directory = tmp_path / "memory"
    monkeypatch.setattr(sys, "argv", ["dots-brain", "--data-dir", str(directory), *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    output = capsys.readouterr()
    document = json.loads(output.err)
    assert document["code"] == "invalid_arguments"
    assert "--help" in document["hint"]
    assert not output.out and PRIVATE not in output.err
    assert not directory.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["connect", "mcp-json"],
        ["disconnect", "mcp-json"],
        [
            "connect",
            "cursor",
            "--mcp-url",
            "http://127.0.0.1:8765/mcp",
            "--credential-file",
            PRIVATE,
        ],
        ["connect", "cursor", "--mcp-url", f"https://{PRIVATE}.invalid/mcp"],
        ["up", "--port", "-1"],
        ["up", "--port", "65536"],
        ["serve", "--port", "0"],
        ["serve", "--port", "65536"],
        ["serve", "--public-port", "8787"],
        ["serve", "--public-gateway"],
        ["serve", "--transport", "http", "--public-port", "0"],
        ["serve", "--transport", "http", "--public-port", "65536"],
        ["serve", "--transport", "http", "--public-port", "8765"],
        ["serve", "--transport", "http", "--public-port", "8787", "--public-gateway"],
    ],
)
def test_invalid_options_fail_before_store_or_locks(tmp_path, monkeypatch, capsys, arguments):
    directory = tmp_path / "memory"

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid options reached a store or lock.")

    for name in ("Store", "writer_lock", "locked"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(sys, "argv", ["dots-brain", "--data-dir", str(directory), *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.err)["code"] == "invalid_input"
    assert not output.out and PRIVATE not in output.err
    assert not directory.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["up", "--port", "0"],
        ["up", "--port", "65535"],
        ["serve", "--transport", "http", "--port", "1", "--public-port", "65535"],
        ["serve", "--transport", "http", "--port", "65535", "--public-gateway"],
        ["connect", "mcp-json", "--config", "explicit.json"],
        ["connect", "cursor", "--config", "explicit.json"],
        ["connect", "codex", "--config", "explicit.toml"],
        ["connect", "claude-code", "--config", "explicit.json"],
    ],
)
def test_valid_port_boundaries_and_explicit_provider_configs_reach_dispatch(
    tmp_path, monkeypatch, arguments
):
    directory = tmp_path / "memory"
    monkeypatch.setattr(cli, "_run", lambda args: {"state": "dispatched"})
    monkeypatch.setattr(cli, "writer_lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(cli, "locked", lambda *a, **k: nullcontext())
    args = cli.parser().parse_args(["--data-dir", str(directory), *arguments])
    assert cli.run(args) == {"state": "dispatched"}
    assert not directory.exists()


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (TimeoutError(PRIVATE), "timeout"),
        (ConnectionError(PRIVATE), "connection_failed"),
        (PermissionError(PRIVATE), "permission_denied"),
        (FileNotFoundError(PRIVATE), "not_found"),
        (BlockingIOError(PRIVATE), "busy"),
        (sqlite3.OperationalError(PRIVATE), "database_error"),
        (ValueError(PRIVATE), "operation_failed"),
        (ExceptionGroup(PRIVATE, [OSError(PRIVATE)]), "operation_failed"),
    ],
)
def test_boundary_errors_have_safe_classification_and_hint(monkeypatch, capsys, exception, code):
    def fail(args):
        raise exception

    monkeypatch.setattr(cli, "run", fail)
    monkeypatch.setattr(sys, "argv", ["dots-brain", "setup"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    output = capsys.readouterr()
    result = json.loads(output.err)
    assert result["state"] == "error" and result["code"] == code
    assert result["hint"]
    assert not output.out and PRIVATE not in output.err


@pytest.fixture
def bootstrap(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/bootstrap.py"
    spec = importlib.util.spec_from_file_location("operator_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--data-dir", str(tmp_path / "memory")])
    monkeypatch.setattr(
        module,
        "sys",
        SimpleNamespace(
            platform="linux",
            version_info=(3, 11),
            argv=sys.argv,
        ),
    )
    monkeypatch.setattr(module.shutil, "which", lambda name: "/synthetic/uv")

    def forbidden(*args, **kwargs):
        pytest.fail("An installation command was not mocked.")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    return module


@pytest.mark.parametrize(
    "arguments",
    [
        ["--connect", PRIVATE],
        ["--config", PRIVATE],
        ["--connect", "mcp-json"],
        ["--" + PRIVATE],
    ],
)
def test_bootstrap_argument_errors_are_private_and_precede_installation(
    bootstrap, capsys, arguments
):
    bootstrap.sys.argv += arguments
    with pytest.raises(SystemExit) as stopped:
        bootstrap.main()
    assert stopped.value.code == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["phase"] == "arguments"
    assert not output.out and PRIVATE not in output.err
    assert not Path(bootstrap.sys.argv[2]).exists()


@pytest.mark.parametrize(
    ("platform", "version", "uv"),
    [
        ("darwin", (3, 11), "/synthetic/uv"),
        ("win32", (3, 11), "/synthetic/uv"),
        ("linux", (3, 10), "/synthetic/uv"),
        ("linux", (3, 11), None),
    ],
)
def test_bootstrap_prerequisites_block_before_installation(
    bootstrap, monkeypatch, capsys, platform, version, uv
):
    bootstrap.sys.platform = platform
    bootstrap.sys.version_info = version
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: uv)
    assert bootstrap.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["phase"] == "prerequisites"
    assert not output.err and not Path(bootstrap.sys.argv[2]).exists()


def child_result(command):
    if command[1] == "sync" or command[-2:] == ["model", "prepare"]:
        return subprocess.CompletedProcess(command, 0, stdout=PRIVATE, stderr=PRIVATE)
    state = (
        "local_ready"
        if command[-1] == "setup"
        else "verified_local_service"
        if "up" in command
        else "configured_verified_bridge"
    )
    return subprocess.CompletedProcess(
        command, 0, stdout=json.dumps({"state": state}), stderr=PRIVATE
    )


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("provider", [None, "claude-code", "cursor", "codex", "mcp-json"])
def test_bootstrap_keeps_lock_freshness_and_supported_provider_combinations(
    bootstrap, monkeypatch, capsys, semantic, provider
):
    commands = []

    def run(command, **kwargs):
        assert kwargs["check"] and kwargs["capture_output"] and kwargs["text"]
        commands.append(command)
        return child_result(command)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    if semantic:
        bootstrap.sys.argv += ["--semantic"]
    if provider:
        bootstrap.sys.argv += ["--connect", provider, "--config", "/synthetic/config"]
    assert bootstrap.main() == 0
    assert commands[0] == ["/synthetic/uv", "sync", "--locked", "--no-dev"] + (
        ["--extra", "semantic"] if semantic else []
    )
    assert len(commands) == 3 + int(semantic) + int(provider is not None)
    assert any(command[-2:] == ["model", "prepare"] for command in commands) is semantic
    start = next(command for command in commands if "up" in command)
    assert "--resume" in start and ("--semantic" in start) is semantic
    if provider:
        assert commands[-1][-4:] == ["connect", provider, "--config", "/synthetic/config"]
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["state"] == (
        "configured_verified_bridge" if provider else "verified_local_service"
    )
    assert result["remote_connection"] == "not_verified"
    assert PRIVATE not in output.out + output.err and not output.err
    assert not Path(bootstrap.sys.argv[2]).exists()


@pytest.mark.parametrize("phase", ["dependencies", "setup", "model", "service", "client"])
@pytest.mark.parametrize("failure", ["process", "os"])
def test_bootstrap_failed_step_reports_only_its_phase(
    bootstrap, monkeypatch, capsys, phase, failure
):
    phases, calls = ["dependencies", "setup", "model", "service", "client"], []
    bootstrap.sys.argv += ["--semantic", "--connect", "mcp-json", "--config", "/synthetic/config"]

    def run(command, **kwargs):
        current = phases[len(calls)]
        calls.append(current)
        if current == phase:
            if failure == "process":
                raise subprocess.CalledProcessError(1, command, output=PRIVATE, stderr=PRIVATE)
            raise PermissionError(PRIVATE)
        return child_result(command)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    with pytest.raises(SystemExit) as stopped:
        bootstrap.main()
    assert stopped.value.code == 1
    assert calls == phases[: phases.index(phase) + 1]
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "state": "blocked",
        "phase": phase,
        "reason": "Installation step failed.",
    }
    assert PRIVATE not in output.out + output.err and not output.err


@pytest.mark.parametrize("phase", ["setup", "service", "client"])
@pytest.mark.parametrize("response", [PRIVATE, "[]", '{"state":"blocked"}', "{}", '{"state":[]}'])
def test_bootstrap_invalid_child_json_blocks_at_its_phase(
    bootstrap, monkeypatch, capsys, phase, response
):
    phases, calls = ["dependencies", "setup", "service", "client"], []
    bootstrap.sys.argv += ["--connect", "mcp-json", "--config", "/synthetic/config"]

    def run(command, **kwargs):
        current = phases[len(calls)]
        calls.append(current)
        result = child_result(command)
        if current == phase:
            result.stdout = response
        return result

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    with pytest.raises(SystemExit) as stopped:
        bootstrap.main()
    assert stopped.value.code == 1
    assert calls == phases[: phases.index(phase) + 1]
    output = capsys.readouterr()
    assert json.loads(output.out)["phase"] == phase
    assert PRIVATE not in output.out + output.err and not output.err
