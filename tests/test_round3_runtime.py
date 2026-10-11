"""Neutral runtime regressions and synchronized leases on disposable installations."""

import contextlib
import fcntl
import http.client
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dots_brain import local, runtime
from dots_brain.auth import issue_client
from dots_brain.errors import BusyError, InputError, StateError, StoreDisabledError
from dots_brain.local import locked, write_json
from dots_brain.store import Store

linux = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux /proc process evidence")


def test_inspection_cannot_reject_a_shared_writer_during_descriptor_close(tmp_path, monkeypatch):
    """Schedule the legitimate writer at the old inspector's exclusive-lock window."""
    path = tmp_path / "writers.lock"
    path.touch()
    original_close = os.close
    checked = False

    def concurrent_writer(descriptor):
        nonlocal checked
        if not checked:
            checked = True
            # This separate open file description is a real competing lease.
            with locked(path, timeout=0, shared=True):
                pass
        original_close(descriptor)

    monkeypatch.setattr(local.os, "close", concurrent_writer)
    status = local.lock_status(path)
    assert checked
    assert status["state"] in {"free", "unknown"}


def test_unavailable_kernel_inspection_is_unknown_and_never_takes_a_lock(tmp_path, monkeypatch):
    path = tmp_path / "writers.lock"
    path.touch()
    read_text = Path.read_text

    def unavailable(value, *args, **kwargs):
        if str(value) == "/proc/locks":
            raise PermissionError("synthetic unavailable kernel view")
        return read_text(value, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unavailable)
    monkeypatch.setattr(fcntl, "flock", lambda *args: pytest.fail("inspection acquired a lease"))
    assert local.lock_status(path) == {"state": "unknown", "owners": []}


def test_unknown_writer_state_does_not_claim_external_writers(tmp_path, monkeypatch):
    store = Store(tmp_path)
    (store.directory / "writers.lock").touch()
    monkeypatch.setattr(runtime, "lock_status", lambda path: {"state": "unknown", "owners": []})
    monkeypatch.setattr(runtime, "unmanaged_writers", lambda store: [])
    result = runtime.down(store)
    assert result["state"] == "writer_state_unknown"
    assert result["next_command"].startswith("lsof ")
    assert "unknown" in result["evidence"]


@pytest.mark.parametrize("has_process", [False, True])
def test_down_absent_directory_checks_writers_without_creating_state(
    tmp_path, monkeypatch, has_process
):
    store = Store(tmp_path / "removed-installation")
    observed = (
        [
            {
                "pid": 12345,
                "process_start": "start",
                "boot_id": "boot",
                "executable": "/synthetic/python3.11",
                "transport": "stdio",
                "stop_command": "kill -TERM 12345",
            }
        ]
        if has_process
        else []
    )
    scanned = []
    monkeypatch.setattr(
        runtime,
        "unmanaged_writers",
        lambda value: scanned.append(value) or observed,
        raising=False,
    )
    monkeypatch.setattr(
        runtime, "locked", lambda *args: pytest.fail("absent store acquired a lease")
    )
    monkeypatch.setattr(
        runtime, "stop_process", lambda *args: pytest.fail("untracked process signalled")
    )
    result = runtime.down(store)
    assert scanned == [store]
    assert result["state"] == ("external_writers_remain" if has_process else "writer_state_unknown")
    if has_process:
        assert result["processes"] == observed
        assert result["next_command"] == "kill -TERM 12345"
    else:
        assert "no process or lease proves stopped" in result["evidence"]
        assert result["next_command"].endswith(" doctor")
    assert not store.directory.exists()


@linux
@pytest.mark.parametrize("shared", [False, True])
def test_kernel_inspection_reports_shared_or_exclusive_owner_then_free(tmp_path, shared):
    path = tmp_path / "writers.lock"
    with locked(path, timeout=0, shared=shared):
        assert local.lock_status(path) == {
            "state": "held",
            "owners": [os.getpid()],
            "mode": "shared" if shared else "exclusive",
        }
    assert local.lock_status(path) == {"state": "free", "owners": []}


@pytest.mark.parametrize(
    "change", [None, "pid_reused", "boot_changed", "wrong_executable", "foreign_directory"]
)
@pytest.mark.parametrize("invocation", ["module", "console"])
def test_unmanaged_scan_requires_stable_identity_executable_and_directory(
    tmp_path, monkeypatch, change, invocation
):
    store = Store(tmp_path / "installation")
    proc = tmp_path / "proc"
    process = proc / "12345"
    process.mkdir(parents=True)
    interpreter = tmp_path / "python3.11"
    interpreter.touch()
    (process / "exe").symlink_to(interpreter)
    entrypoint = tmp_path / "dots-brain"
    entrypoint.write_text(
        'import sys\nfrom dots_brain.cli import main\nif __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    )
    command = (
        [str(interpreter)]
        + (["-m", "dots_brain.cli"] if invocation == "module" else [str(entrypoint)])
        + [
            "--data-dir",
            str(store.directory if change != "foreign_directory" else tmp_path / "foreign"),
            "serve",
        ]
    )
    if change == "wrong_executable":
        command[0] = str(tmp_path / "different-python")
    (process / "cmdline").write_bytes(("\0".join(command) + "\0").encode())
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime, "Path", lambda value: proc if value == "/proc" else Path(value))
    starts = iter(["start", "reused" if change == "pid_reused" else "start"])
    boots = iter(["boot", "new-boot" if change == "boot_changed" else "boot"])
    monkeypatch.setattr(runtime, "process_identity", lambda pid: next(starts))
    monkeypatch.setattr(runtime, "boot_identity", lambda: next(boots))
    result = runtime.unmanaged_writers(store)
    if change:
        assert result == []
    else:
        assert result == [
            {
                "pid": 12345,
                "process_start": "start",
                "boot_id": "boot",
                "executable": str(interpreter),
                "transport": "stdio",
                "stop_command": "kill -TERM 12345",
            }
        ]


@pytest.mark.parametrize(
    "variant",
    [
        "path_python",
        "equals_data",
        "equals_transport",
        "path_equals",
        "console_equals",
        "path_foreign_executable",
        "path_foreign_argv0",
        "path_changed_executable",
    ],
)
def test_parser_valid_serve_spellings_are_detected(tmp_path, monkeypatch, variant):
    store = Store(tmp_path / "installation")
    proc = tmp_path / "proc"
    process = proc / "12345"
    process.mkdir(parents=True)
    interpreter = tmp_path / "python3.11"
    interpreter.touch()
    (process / "exe").symlink_to(interpreter)
    if variant == "path_foreign_executable":
        (process / "exe").unlink()
        (process / "exe").symlink_to(tmp_path / "unrelated-executable")
    entrypoint = tmp_path / "dots-brain"
    entrypoint.write_text(
        "import sys\nfrom dots_brain.cli import main\n"
        'if __name__ == "__main__":\n    sys.exit(main())\n'
    )
    command = ["python3.11" if variant.startswith("path_") else str(interpreter)]
    if variant == "path_foreign_argv0":
        command[0] = "unrelated-interpreter"
    command += [str(entrypoint)] if variant == "console_equals" else ["-m", "dots_brain.cli"]
    command += (
        ["--data-dir=" + str(store.directory)]
        if variant in {"equals_data", "path_equals", "console_equals"}
        else ["--data-dir", str(store.directory)]
    )
    command += ["serve"]
    command += (
        ["--transport=stdio"]
        if variant in {"equals_transport", "path_equals", "console_equals"}
        else ["--transport", "stdio"]
    )
    (process / "cmdline").write_bytes(("\0".join(command) + "\0").encode())
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime, "Path", lambda value: proc if value == "/proc" else Path(value))
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "start")
    monkeypatch.setattr(runtime, "boot_identity", lambda: "boot")
    if variant == "path_changed_executable":
        links = iter([str(interpreter), str(tmp_path / "different-python3.11")])
        original_link = os.readlink
        monkeypatch.setattr(
            runtime.os,
            "readlink",
            lambda path: next(links) if path == process / "exe" else original_link(path),
        )
    observed = runtime.unmanaged_writers(store)
    if variant in {"path_foreign_executable", "path_foreign_argv0", "path_changed_executable"}:
        assert observed == []
        return
    assert [row["pid"] for row in observed] == [12345]
    assert observed[0]["transport"] == "stdio"
    with pytest.raises(BusyError, match="12345"):
        runtime.require_no_unmanaged_writers(store)


@pytest.mark.parametrize(
    "change", [None, "uv_cleanup", "foreign_import", "foreign_script", "too_large"]
)
def test_console_entrypoint_checks_bounded_current_script_evidence(tmp_path, change):
    path = tmp_path / "dots-brain"
    source = (
        'import sys\nfrom dots_brain.cli import main\nif __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    )
    if change == "uv_cleanup":
        source = source.replace(
            "    sys.exit(main())",
            '    if sys.argv[0].endswith(".exe"):\n'
            "        sys.argv[0] = sys.argv[0][:-4]\n    sys.exit(main())",
        )
    elif change == "foreign_import":
        source = source.replace("from dots_brain.cli", "from unrelated_module")
    elif change == "foreign_script":
        source = "import time\ntime.sleep(30)\n"
    elif change == "too_large":
        source += "#" * 8193
    path.write_text(source)
    assert runtime._console_entrypoint(path) == (change in {None, "uv_cleanup"})


@pytest.mark.parametrize("version", ["0.3.0a1", "0.3.0a2", "0.4.0a1", "0.4.0a2", "0.4.0a3"])
def test_supported_legacy_managed_versions_keep_exact_identity_validation(
    tmp_path, monkeypatch, version
):
    store = Store(tmp_path)
    state = {
        "pid": 12345,
        "process_start": "synthetic-start",
        "port": 1234,
        "semantic": False,
        "version": version,
    }
    command = [
        sys.executable,
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
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "synthetic-start")
    monkeypatch.setattr(Path, "read_bytes", lambda path: ("\0".join(command) + "\0").encode())
    monkeypatch.setattr(os, "readlink", lambda path: sys.executable)
    assert runtime.owns_process(store, state)
    assert not runtime.owns_process(store, {**state, "process_start": "reused"})
    assert not runtime.owns_process(Store(tmp_path / "foreign"), state)
    assert not runtime.owns_process(store, {**state, "version": "0.2.0-alpha.1"})
    assert not runtime.owns_process(store, {**state, "executable": sys.executable})


def test_disabled_runtime_names_the_real_resume_command(tmp_path):
    store = Store(tmp_path)
    write_json(store.directory / "disabled.json", {"version": 1, "reason": "uninstalled"})
    with pytest.raises(StoreDisabledError, match="up --resume"):
        runtime.ensure_enabled(store)


def test_permission_difference_names_revoke_and_file_preservation_commands(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    path = store.directory / "client.json"
    issued = issue_client(
        store,
        name="fixture",
        scopes=["memory:read"],
        projects=None,
        days=1,
        output=path,
        url="http://127.0.0.1:1234/mcp",
    )
    with pytest.raises(StateError) as error:
        runtime.credential(store, name="fixture", path=path, url="http://127.0.0.1:1234/mcp")
    assert "client revoke " + issued["client_id"] in str(error.value)
    assert "mv " in str(error.value) and ".before-replacement" in str(error.value)
    assert path.exists()


def test_startup_failure_surfaces_lease_evidence_without_reflecting_logs(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.initialize()
    (store.directory / "service.log").write_text("fixture-secret-must-not-be-returned")
    monkeypatch.setattr(runtime, "credential", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "owns_process", lambda *args: False)
    monkeypatch.setattr(
        runtime,
        "lock_status",
        lambda path: {"state": "held", "owners": [54321], "mode": "exclusive"},
    )

    async def unavailable(*args, **kwargs):
        raise ConnectionError("fixture-secret-must-not-be-returned")

    monkeypatch.setattr(runtime, "verify_connection", unavailable)
    with pytest.raises(BusyError) as error:
        runtime.verify_daemon(store, {"pid": 12345, "url": "http://127.0.0.1:1234/mcp"})
    message = str(error.value)
    assert "http.lock" in message and "54321" in message and "kill -TERM 54321" in message
    assert "fixture-secret" not in message


def test_readiness_failure_without_lease_evidence_names_safe_local_diagnostics(
    tmp_path, monkeypatch
):
    store = Store(tmp_path)
    store.initialize()
    monkeypatch.setattr(runtime, "credential", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "owns_process", lambda *args: False)
    monkeypatch.setattr(runtime, "lock_status", lambda path: {"state": "unknown", "owners": []})

    async def unavailable(*args, **kwargs):
        raise ConnectionError("fixture-secret-must-not-be-returned")

    monkeypatch.setattr(runtime, "verify_connection", unavailable)
    with pytest.raises(InputError) as error:
        runtime.verify_daemon(store, {"pid": 12345, "url": "http://127.0.0.1:1234/mcp"})
    message = str(error.value)
    assert "doctor" in message and "tail -n 40" in message and "service.log" in message
    assert "fixture-secret" not in message


@contextlib.contextmanager
def running_serve(store, *, transport="stdio", source=None, invocation="module"):
    """Run the actual CLI; a protocol response explicitly synchronizes readiness."""
    environment = dict(os.environ)
    if source:
        environment["PYTHONPATH"] = source
    command = (
        [sys.executable, "-m", "dots_brain.cli"]
        if invocation in {"module", "module_path_equals"}
        else [str(Path(sys.executable).with_name("dots-brain"))]
    ) + [
        "--data-dir",
        str(store.directory),
        "serve",
        "--transport",
        transport,
    ]
    if invocation == "module_path_equals":
        command[0] = Path(sys.executable).name
        environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.defpath
        command[3:5] = ["--data-dir=" + str(store.directory)]
        command[-2:] = ["--transport=" + transport]
    with contextlib.ExitStack() as cleanup:
        options = {}
        if transport == "http":
            listener = cleanup.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            command += ["--port", str(port), "--listen-fd", str(listener.fileno())]
            options["pass_fds"] = (listener.fileno(),)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            **options,
        )
        try:
            if transport == "stdio":
                process.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "neutral-lease-fixture", "version": "1"},
                            },
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                assert select.select([process.stdout], [], [], 15)[0], (
                    "serve readiness response missing"
                )
                assert "result" in json.loads(process.stdout.readline())
            else:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, process.stderr.read()
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                    try:
                        connection.request("GET", "/mcp")
                        response = connection.getresponse()
                        assert response.status == 401
                        response.read()
                        break
                    except (OSError, http.client.HTTPException):
                        if time.monotonic() >= deadline:
                            raise
                    finally:
                        connection.close()
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)


@pytest.mark.parametrize(
    "transport",
    [
        "stdio",
        pytest.param(
            "http",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="inherited HTTP listener validation requires Linux"
            ),
        ),
    ],
)
def test_actual_serve_holds_all_required_leases_for_its_lifetime(tmp_path, transport):
    store = Store(tmp_path)
    store.initialize()
    with running_serve(store, transport=transport):
        with pytest.raises(BusyError):
            with locked(store.directory / "writers.lock", timeout=0):
                pytest.fail("serve did not retain its writer lease")
        if transport == "http":
            with pytest.raises(BusyError):
                with locked(store.directory / "http.lock", timeout=0):
                    pytest.fail("HTTP serve did not retain its endpoint lease")


@linux
def test_untracked_managed_daemon_without_state_or_lock_is_visible_and_never_signalled(tmp_path):
    store = Store(tmp_path)
    state = runtime.up(store, port=0)
    original = runtime.load_state(store)
    try:
        (store.directory / "service.json").unlink()
        (store.directory / "writers.lock").unlink()
        result = runtime.down(store)
        assert result["state"] == "external_writers_remain"
        assert runtime.managed_status(store)["state"] == "unmanaged_writers"
        observed = runtime.unmanaged_writers(store)
        assert [row["pid"] for row in observed] == [state["pid"]]
        assert observed[0]["process_start"] == original["process_start"]
        assert observed[0]["boot_id"] == original["boot_id"]
        assert result["processes"] == observed
        assert runtime.active(original)
        with pytest.raises(BusyError, match=str(state["pid"])):
            runtime.up(store, port=0)
    finally:
        runtime.stop_process(store, original)


@linux
@pytest.mark.parametrize("invocation", ["module", "console", "module_path_equals"])
def test_historical_supervised_serve_without_state_or_lock_is_detected(tmp_path, invocation):
    source = os.environ.get("BRAIN_TEST_OLD_V2_SRC")
    if not source:
        pytest.skip("Historical source supplied by Linux CI")
    store = Store(tmp_path)
    initialized = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from dots_brain.store import Store; Store(sys.argv[1]).initialize()",
            str(store.directory),
        ],
        env={**os.environ, "PYTHONPATH": source},
        capture_output=True,
        timeout=15,
    )
    assert initialized.returncode == 0, initialized.stderr.decode()
    with running_serve(store, source=source, invocation=invocation) as process:
        assert not (store.directory / "service.json").exists()
        assert not (store.directory / "writers.lock").exists()
        assert runtime.down(store)["state"] == "external_writers_remain"
        assert runtime.managed_status(store)["state"] == "unmanaged_writers"
        observed = runtime.unmanaged_writers(store)
        assert [row["pid"] for row in observed] == [process.pid]
        assert observed[0]["transport"] == "stdio"
        assert process.poll() is None
        with pytest.raises(BusyError, match=str(process.pid)):
            runtime.up(store)
        if invocation == "module_path_equals":
            from dots_brain.operations import migrate

            before = store.path.read_bytes()
            with pytest.raises(BusyError, match=str(process.pid)):
                migrate(store, apply=True, writers_stopped=True, backup=None)
            assert store.path.read_bytes() == before
            assert process.poll() is None
            shutil.rmtree(store.directory)
            removed = runtime.down(store)
            assert removed["state"] == "external_writers_remain"
            assert [row["pid"] for row in removed["processes"]] == [process.pid]
            assert not store.directory.exists()
            assert process.poll() is None


@linux
def test_unrelated_process_in_same_directory_is_not_claimed_or_signalled(tmp_path):
    store = Store(tmp_path)
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.readline()"],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
    ) as process:
        try:
            assert runtime.unmanaged_writers(store) == []
            assert runtime.down(store)["state"] == "stopped"
            assert process.poll() is None
        finally:
            process.communicate(input=b"done\n", timeout=5)
