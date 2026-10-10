"""Upgrade installation state without trusting filenames or unrelated processes."""

import os
import subprocess
import sys

import pytest

from dots_brain.auth import authenticate, issue_client, read_connection
from dots_brain.errors import InputError, StateError
from dots_brain.installation_state import resume_uninstalled
from dots_brain.local import read_json, write_json
from dots_brain.operations import migrate
from dots_brain.runtime import credential, down, owns_process, up
from dots_brain.store import Store


def test_historical_probe_is_revoked_and_replaced_with_read_only_access(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    path = store.directory / "probe.connection.json"
    original = issue_client(
        store,
        name="installation-probe",
        scopes=["memory:read", "memory:write", "memory:forget"],
        projects=["__dots_brain_probe__"],
        days=1,
        output=path,
        url="http://127.0.0.1:1234/mcp",
    )
    old_token = read_connection(path)["token"]
    arguments = dict(
        name="installation-probe",
        path=path,
        url="http://127.0.0.1:1234/mcp",
        projects=["__dots_brain_probe__"],
        write=False,
        replace_invalid=True,
    )
    replacement = credential(store, **arguments)
    assert replacement != original["client_id"]
    assert authenticate(store, old_token) is None
    assert authenticate(store, read_connection(path)["token"]).scopes == {"memory:read"}
    assert credential(store, **arguments) == replacement


def test_ordinary_credentials_cannot_take_the_probe_rotation_path(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    path = store.directory / "ordinary.json"
    issue_client(
        store,
        name="user",
        scopes=["memory:read", "memory:write", "memory:forget"],
        projects=["__dots_brain_probe__"],
        days=1,
        output=path,
        url="http://127.0.0.1:1234/mcp",
    )
    before = path.read_bytes()
    with pytest.raises(StateError, match="permissions differ"):
        credential(
            store,
            name="installation-probe",
            path=path,
            write=False,
            projects=["__dots_brain_probe__"],
            replace_invalid=True,
            url="http://127.0.0.1:1234/mcp",
        )
    assert path.read_bytes() == before
    assert authenticate(store, read_connection(path)["token"]) is not None


@pytest.mark.parametrize(
    "marker",
    [
        {"version": 1, "disabled": True},
        {"version": 1, "disabled": True, "uninstalled_at": "synthetic"},
        {"version": 1, "reason": "uninstalled", "created_at": "synthetic"},
    ],
)
def test_failed_resume_restores_the_original_marker(tmp_path, marker, monkeypatch):
    store = Store(tmp_path)
    store.initialize()
    write_json(store.directory / "disabled.json", marker)
    monkeypatch.setattr("dots_brain.runtime.sys.platform", "linux")
    monkeypatch.setattr(store, "initialize", lambda: (_ for _ in ()).throw(InputError("failure")))
    with pytest.raises(InputError, match="failure"):
        up(store, resume=True)
    assert read_json(store.directory / "disabled.json") == marker
    with resume_uninstalled(store, requested=True):
        assert not (store.directory / "disabled.json").exists()
    assert not (store.directory / "disabled.json").exists()


@pytest.mark.parametrize(
    "marker",
    [
        {"version": 2, "reason": "uninstalled"},
        {"version": 1, "disabled": True, "recovery_id": "unknown"},
        {"version": 1, "reason": "restored_requires_review"},
    ],
)
def test_resume_never_removes_a_recovery_or_unknown_marker(tmp_path, marker):
    store = Store(tmp_path)
    write_json(store.directory / "disabled.json", marker)
    with pytest.raises(StateError):
        with resume_uninstalled(store, requested=True):
            pytest.fail("not resumable")
    assert read_json(store.directory / "disabled.json") == marker


@pytest.mark.skipif(
    sys.platform != "linux", reason="requires real Linux process identity and pidfd"
)
def test_real_historical_daemon_stop_migrate_restart_and_probe_upgrade(tmp_path):
    old_source = os.environ.get("BRAIN_TEST_OLD_V2_SRC")
    if not old_source:
        pytest.skip("Historical source supplied by Linux CI")
    store = Store(tmp_path / "installation")
    old_command = [sys.executable, "-m", "dots_brain.cli", "--data-dir", str(store.directory)]
    environment = {**os.environ, "PYTHONPATH": old_source}
    old_start = subprocess.run(
        old_command + ["up", "--port", "0"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert old_start.returncode == 0, old_start.stderr
    try:
        state = read_json(store.directory / "service.json")
        assert "executable" not in state and owns_process(store, state)
        assert not owns_process(Store(tmp_path / "foreign"), state)
        assert not owns_process(store, {**state, "process_start": "incorrect"})
        probe = store.directory / "probe.connection.json"
        old_token = read_connection(probe)["token"]
        down(store)
        assert not owns_process(store, state)
        migrate(store, apply=True, writers_stopped=True, backup=tmp_path / "before.sqlite3")
        current = up(store)
        assert current["read"] is True and current["pid"] != state["pid"]
        assert authenticate(store, old_token) is None
        assert authenticate(store, read_connection(probe)["token"]).scopes == {"memory:read"}
        assert owns_process(store, read_json(store.directory / "service.json"))
        down(store)
    finally:
        # Both commands operate only on the disposable installation created above.
        subprocess.run(old_command + ["down"], env=environment, capture_output=True, timeout=20)


def test_existing_supervised_writer_is_reported_without_signalling_it(tmp_path):
    from dots_brain.local import lock_status, locked
    from dots_brain.operator_cli import doctor
    from dots_brain.removal import uninstall

    store = Store(tmp_path / "supervised")
    store.initialize()
    with locked(store.directory / "writers.lock", shared=True):
        status = lock_status(store.directory / "writers.lock")
        assert status["state"] == "held"
        if sys.platform == "linux":
            assert os.getpid() in status["owners"]
        assert down(store)["state"] == "external_writers_remain"
        assert doctor(store)["checks"]["managed_service"]["writers"]["state"] == "held"
        removed = uninstall(store)
        assert removed["state"] == "partial"
        assert removed["managed_process_stopped"] is False
        assert doctor(store)["healthy"] is False
    assert down(store)["state"] == "stopped"


def test_lock_diagnosis_does_not_create_missing_files(tmp_path):
    from dots_brain.local import lock_status

    path = tmp_path / "absent" / "writers.lock"
    assert lock_status(path) == {"state": "absent", "owners": []}
    assert not path.parent.exists()


def test_listener_sets_tcp_nodelay_and_keeps_port_reserved():
    import socket

    from dots_brain.runtime import reserve_listener

    with reserve_listener(0, {}) as listener:
        assert listener.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
        address = listener.getsockname()
        with socket.socket() as competitor:
            with pytest.raises(OSError):
                competitor.bind(address)


@pytest.mark.skipif(sys.platform != "linux", reason="uses a real running Linux process")
def test_deleted_interpreter_link_preserves_matching_process_identity(tmp_path, monkeypatch):
    from dots_brain.runtime import boot_identity, process_identity

    store = Store(tmp_path)
    pid = os.getpid()
    executable = os.readlink(f"/proc/{pid}/exe")
    state = {
        "pid": pid,
        "process_start": process_identity(pid),
        "port": 1234,
        "semantic": False,
        "data_dir": str(store.directory),
        "boot_id": boot_identity(),
        "executable": executable,
    }
    from pathlib import Path

    original_read = Path.read_bytes
    args = [
        executable,
        "-m",
        "dots_brain.cli",
        "--data-dir",
        str(store.directory),
        "serve",
        "--transport",
        "http",
        "--port",
        "1234",
    ]
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: (
            ("\0".join(args) + "\0").encode()
            if str(path) == f"/proc/{pid}/cmdline"
            else original_read(path)
        ),
    )
    original_link = os.readlink
    monkeypatch.setattr(
        os,
        "readlink",
        lambda path: (
            executable + " (deleted)" if path == f"/proc/{pid}/exe" else original_link(path)
        ),
    )
    assert owns_process(store, state)
    assert not owns_process(store, {**state, "executable": "/different/python"})
