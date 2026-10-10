import importlib.util
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "arguments",
    [
        ["--config", "/tmp/client"],
        ["--connect", "unknown"],
        ["--connect", "mcp-json"],
        ["--port", "65536"],
    ],
)
def test_bootstrap_invalid_options_have_no_side_effects(monkeypatch, tmp_path, arguments):
    bootstrap = script("bootstrap")

    def forbidden(*args, **kwargs):
        pytest.fail("An installation command ran before argument validation")

    monkeypatch.setattr(bootstrap.subprocess, "run", forbidden)
    with pytest.raises(SystemExit) as error:
        bootstrap.main(["--data-dir", str(tmp_path / "data"), *arguments])
    assert error.value.code == 2
    assert not (tmp_path / "data").exists()


def test_bootstrap_failure_reports_phase_and_child_json(monkeypatch, tmp_path, capsys):
    bootstrap = script("bootstrap")
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/synthetic/uv")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=json.dumps(
                {"code": "store_disabled", "message": "Resume requires explicit operator action."}
            ),
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    assert bootstrap.main(["--data-dir", str(tmp_path / "data")]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["step"] == "setup"
    assert result["result"]["code"] == "store_disabled"
    assert len(commands) == 2


def test_audit_target_remote_arguments_are_shell_quoted(tmp_path, monkeypatch):
    audit = script("audit_review")
    path = tmp_path / "target.json"
    target = dict(
        origin="synthetic",
        host="memory-test",
        user="memory",
        executable="/opt/memory app/bin/brain",
        data_dir="/private/space $(must-not-execute)",
        timezone="UTC",
    )
    audit.atomic_json(path, target)
    configured = audit.AuditTarget.load(path)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=b'{"events":[],"next_after_id":0,"has_more":false}')

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.fetch_page(0, 100, configured)["events"] == []
    remote = commands[0][-1]
    parsed = shlex.split(remote)
    assert target["data_dir"] in parsed and target["executable"] in parsed
    assert commands[0][-2] == target["host"]
    target["host"] = "-oProxyCommand=invalid"
    audit.atomic_json(path, target)
    with pytest.raises(ValueError):
        audit.AuditTarget.load(path)


@pytest.mark.parametrize("name", ["stress", "stress_semantic"])
def test_stress_acceptance_cannot_be_optimized_away(name, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    source = (ROOT / "scripts" / f"{name}.py").read_text()
    code = compile(source, name, "exec", optimize=2)
    namespace = {"__name__": "synthetic_stress"}
    exec(code, namespace)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        namespace["require"](False, "synthetic failure")
