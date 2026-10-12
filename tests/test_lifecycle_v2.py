import json
import os
import signal
import sys
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from dots_brain import runtime
from dots_brain.auth import authenticate, issue_client, list_clients, read_connection
from dots_brain.clients import configure, connect_client
from dots_brain.diagnostics import diagnose
from dots_brain.errors import InputError
from dots_brain.local import locked, write_json
from dots_brain.oauth import configure as configure_oauth
from dots_brain.store import Store


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "memory")
    result.initialize()
    return result


@pytest.fixture
def simulated_start(store, monkeypatch):
    stopped = []
    monkeypatch.setattr(
        runtime, "sys", SimpleNamespace(platform="linux", executable=sys.executable)
    )
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=7654321))
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "synthetic-start")
    monkeypatch.setattr(runtime, "stop_process", lambda state: stopped.append(state["pid"]))
    return stopped


def test_readiness_precedes_state_publication_and_probe_only_reads(
    store, simulated_start, monkeypatch
):
    async def verify(path):
        assert not runtime.state_path(store).exists()
        credential = read_connection(path)
        assert authenticate(store, credential["token"]).scopes == frozenset({"memory:read"})
        return {"read": True}

    monkeypatch.setattr(runtime, "verify_connection", verify)
    result = runtime.up(store, port=0)
    state = json.loads(runtime.state_path(store).read_text())
    assert result["read"] and result["write"] == "not_tested"
    assert state["data_dir"] == str(store.directory.resolve())
    assert simulated_start == [] and store.status()["memories"] == 0


@pytest.mark.parametrize("failure", ["readiness", "publication", "credential"])
def test_failed_start_cleans_only_started_process(store, simulated_start, monkeypatch, failure):
    async def verify(path):
        return {"read": failure != "readiness"}

    def reject(*args, **kwargs):
        raise InputError("Synthetic startup failure.")

    monkeypatch.setattr(runtime, "verify_connection", verify)
    monkeypatch.setattr(runtime, "active", lambda state: False)
    if failure == "publication":
        monkeypatch.setattr(runtime, "write_json", reject)
    if failure == "credential":
        monkeypatch.setattr(runtime, "credential", reject)
    with pytest.raises(InputError):
        runtime.up(store, port=0)
    assert simulated_start == [7654321]
    assert not runtime.state_path(store).exists()


@pytest.mark.parametrize("stays_alive", [False, True])
def test_stop_confirms_exit_after_sigkill_or_reports_failure(monkeypatch, stays_alive):
    sent, checks = [], []
    ticks = iter(range(100))

    def active(state):
        if signal.SIGKILL in sent:
            checks.append(True)
            return stays_alive or len(checks) < 3
        return True

    monkeypatch.setattr(runtime, "active", active)
    monkeypatch.setattr(
        runtime, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None)
    )
    monkeypatch.setattr(
        runtime,
        "os",
        SimpleNamespace(
            pidfd_open=lambda pid: 999,
            close=lambda fd: None,
            waitpid=lambda pid, flags: (pid, 0),
            WNOHANG=os.WNOHANG,
        ),
    )
    monkeypatch.setattr(
        runtime,
        "signal",
        SimpleNamespace(
            pidfd_send_signal=lambda fd, sig: sent.append(sig),
            SIGTERM=signal.SIGTERM,
            SIGKILL=signal.SIGKILL,
        ),
    )
    if stays_alive:
        with pytest.raises(InputError, match="has not stopped"):
            runtime.stop_process({"pid": 4321})
    else:
        runtime.stop_process({"pid": 4321})
    assert sent == [signal.SIGTERM, signal.SIGKILL] and len(checks) >= 3


@pytest.mark.parametrize("directory", [None, "/synthetic/other-store"])
def test_unbound_state_cannot_stop_or_adopt_a_process(store, monkeypatch, directory):
    state = {"pid": 4321, "process_start": "same", "data_dir": directory}
    write_json(runtime.state_path(store), state)
    monkeypatch.setattr(runtime, "active", lambda state: True)
    monkeypatch.setattr(runtime, "stop_process", lambda state: pytest.fail("Unbound process"))
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(InputError, match="not bound"):
        runtime.up(store)
    assert runtime.down(store)["state"] == "not_managed"
    assert json.loads(runtime.state_path(store).read_text()) == state


def test_external_http_is_preserved_by_up_and_down(store, http_process, free_ports, monkeypatch):
    with http_process(store, free_ports[0]) as process:
        monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="linux"))
        with pytest.raises(InputError, match="externally managed"):
            runtime.up(store)
        assert runtime.down(store)["state"] == "externally_managed"
        assert process.poll() is None and not runtime.state_path(store).exists()


@pytest.mark.parametrize("public", [False, True])
def test_external_connect_and_doctor_read_same_store_with_d6_diagnostics(
    store, tmp_path, http_process, free_ports, monkeypatch, public
):
    configure_oauth(store, "https://memory.example.invalid")
    endpoint = f"http://127.0.0.1:{free_ports[0]}/mcp"
    credential = tmp_path / "doctor.json"
    issue_client(
        store,
        name="doctor",
        scopes=["memory:read"],
        projects=None,
        days=1,
        output=credential,
        url=endpoint,
    )
    monkeypatch.setattr("dots_brain.clients.up", lambda *a, **k: pytest.fail("No managed startup"))
    with http_process(store, free_ports[0], free_ports[1] if public else None) as process:
        with locked(store.directory / "service.lock"):
            check = diagnose(store, mcp_url=endpoint, connection=credential)
            result = connect_client(
                store,
                provider="cursor",
                config=tmp_path / "cursor.json",
                projects=["synthetic"],
                mcp_url=endpoint,
            )
        assert check["state"] == "local_ready" and check["checks"]["mcp"] == "verified_read"
        assert check["warnings"] == ([] if public else ["public_oauth_listener_missing"])
        assert result["read"] and result["write"] == "not_tested"
        entry = json.loads((tmp_path / "cursor.json").read_text())["mcpServers"]["dots-brain"]
        assert "--local-data-dir" not in entry["args"]
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0] == 0
        assert process.poll() is None and not runtime.state_path(store).exists()


@pytest.mark.parametrize("problem", ["unreachable", "wrong-store", "config-conflict"])
def test_failed_external_connect_leaves_no_usable_new_credential(
    store, tmp_path, http_process, free_ports, problem
):
    target = tmp_path / "cursor.json"
    original = '{"mcpServers":{"dots-brain":{"command":"unrelated"}}}'
    if problem == "config-conflict":
        target.write_text(original)
    with ExitStack() as stack:
        if problem == "wrong-store":
            other = Store(tmp_path / "other")
            other.initialize()
            stack.enter_context(http_process(other, free_ports[0]))
        with pytest.raises(InputError):
            connect_client(
                store,
                provider="cursor",
                config=target,
                mcp_url=f"http://127.0.0.1:{free_ports[0]}/mcp",
            )
    assert all(client["revoked"] for client in list_clients(store))
    assert not list((store.directory / "connections").glob("*.json"))
    assert not (store.directory / "integrations.json").exists()
    assert target.read_text() == original if problem == "config-conflict" else not target.exists()


@pytest.mark.parametrize("failure", ["probe", "credential-publication", "malformed-credential"])
def test_failed_reconnect_preserves_existing_credential_and_registry(
    store, tmp_path, http_process, free_ports, monkeypatch, failure
):
    config = tmp_path / "cursor.json"
    endpoint = f"http://127.0.0.1:{free_ports[0]}/mcp"
    with http_process(store, free_ports[0]):
        connect_client(store, provider="cursor", config=config, mcp_url=endpoint)
        registry = store.directory / "integrations.json"
        connection = next((store.directory / "connections").glob("*.json"))
        if failure == "malformed-credential":
            connection.write_text("[]")
        before = {path: path.read_bytes() for path in (config, registry, connection)}
        if failure == "credential-publication":
            real_write = runtime.write_json

            def write_then_fail(path, value):
                real_write(path, value)
                raise OSError("Synthetic credential post-publication failure")

            monkeypatch.setattr(runtime, "write_json", write_then_fail)
        with pytest.raises(OSError if failure == "credential-publication" else InputError):
            connect_client(
                store,
                provider="cursor",
                config=config,
                mcp_url=f"http://127.0.0.1:{free_ports[1]}/mcp",
            )
        assert all(path.read_bytes() == content for path, content in before.items())
        if failure != "malformed-credential":
            assert authenticate(store, read_connection(connection)["token"]) is not None


def test_uninstall_never_signals_state_bound_to_other_directory(store, tmp_path, monkeypatch):
    from dots_brain import removal

    runtime.write_json(runtime.state_path(store), {"data_dir": str(tmp_path / "another")})

    def unexpected_signal(state):
        pytest.fail("Uninstall attempted to stop an unbound process")

    monkeypatch.setattr(removal, "stop_process", unexpected_signal)
    result = removal.uninstall(store)
    assert result["state"] == "partial"
    assert {"state": "managed_process_unbound"} in result["issues"]


@pytest.mark.parametrize("original", [None, "", '{"theme":"dark"}'])
def test_connect_publication_failure_restores_owned_changes(store, tmp_path, monkeypatch, original):
    from dots_brain import clients

    target = tmp_path / "cursor.json"
    if original is not None:
        target.write_text(original)
    real_write, failed = clients.atomic_write, []

    def write_then_fail(path, content, **kwargs):
        real_write(path, content, **kwargs)
        if path == target and not failed:
            failed.append(True)
            raise OSError("Synthetic post-publication failure")

    async def verify(*args, **kwargs):
        return {"state": "verified_read", "read": True, "write": "not_tested"}

    monkeypatch.setattr(clients, "atomic_write", write_then_fail)
    monkeypatch.setattr(clients, "verify_command", verify)
    with pytest.raises(OSError):
        connect_client(store, provider="cursor", config=target, mcp_url="http://127.0.0.1:9876/mcp")
    assert target.read_text() == original if original is not None else not target.exists()
    assert not target.with_name("cursor.json.before-dots-brain").exists()
    assert not (store.directory / "integrations.json").exists()
    assert all(client["revoked"] for client in list_clients(store))


def test_failed_configuration_preserves_concurrent_edit_and_new_backup(tmp_path):
    target = tmp_path / "client.json"
    target.write_text('{"theme":"original"}')
    changed = '{"theme":"concurrently-changed"}'
    with pytest.raises(InputError, match="cleanup is incomplete"), ExitStack() as rollback:
        configure(
            target,
            directory=tmp_path / "memory",
            provider="cursor",
            entry={"command": "synthetic"},
            rollback=rollback,
        )
        target.write_text(changed)
        raise RuntimeError("Synthetic failure")
    assert target.read_text() == changed
    assert target.with_name("client.json.before-dots-brain").read_text() == '{"theme":"original"}'


@pytest.mark.parametrize("problem", ["database", "oauth", "missing"])
def test_doctor_reports_independent_failures_without_initializing(store, tmp_path, problem):
    if problem == "database":
        with store.connection(write=True) as db:
            db.execute("PRAGMA user_version=99")
    if problem == "oauth":
        (store.directory / "oauth.json").write_text("{invalid")
    if problem == "missing":
        store = Store(tmp_path / "absent")
    result = diagnose(store)
    assert set(result["checks"]) == {"database", "oauth", "managed_state", "mcp", "sqlite"}
    if problem == "missing":
        assert result["state"] == "setup_required" and not store.directory.exists()
    else:
        assert result["state"] == "partial"
        assert result["checks"]["mcp"] == "not_verified"


@pytest.mark.parametrize(
    "endpoint",
    ["https://remote.example.invalid/mcp", "http://127.0.0.1/x", "http://127.0.0.1/mcp?q=1"],
)
def test_private_endpoint_rejects_unsafe_or_ambiguous_targets(endpoint):
    with pytest.raises(InputError):
        runtime.private_endpoint(endpoint)


@pytest.mark.parametrize("symlink", [False, True])
def test_start_log_rotation_and_symlink_boundary(
    store, tmp_path, simulated_start, monkeypatch, symlink
):
    log = store.directory / "service.log"
    other = tmp_path / "untouched"
    other.write_text("unrelated")
    if symlink:
        log.symlink_to(other)
    else:
        log.write_bytes(b"x" * (5 * 1024 * 1024))

    async def verify(path):
        return {"read": True}

    monkeypatch.setattr(runtime, "verify_connection", verify)
    if symlink:
        with pytest.raises(InputError, match="symbolic"):
            runtime.up(store, port=0)
        assert other.read_text() == "unrelated"
    else:
        runtime.up(store, port=0)
        assert log.stat().st_size == 0
        assert (store.directory / "service.log.1").stat().st_size == 5 * 1024 * 1024


@pytest.mark.skipif(sys.platform != "linux", reason="Requires actual Linux managed process control")
def test_concurrent_cold_starts_reuse_one_managed_process(store):
    from concurrent.futures import ThreadPoolExecutor

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: runtime.up(store, port=0), range(3)))
        assert len({result["pid"] for result in results}) == 1
    finally:
        runtime.down(store)
