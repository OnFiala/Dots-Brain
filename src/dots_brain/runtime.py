"""Idempotent local startup without inventing VM persistence or ingress."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .auth import authenticate, issue_client, read_connection, revoke_client, validate_endpoint
from .bridge import verify_connection
from .errors import InputError
from .local import atomic_write, locked, read_json, write_json
from .store import Store


def process_identity(pid: int) -> str | None:
    try:
        # PID plus start time prevents accidentally stopping a reused PID.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def active(state: dict) -> bool:
    pid = state.get("pid")
    return (
        isinstance(pid, int)
        and pid > 1
        and state.get("process_start") is not None
        and process_identity(pid) == state["process_start"]
    )


def credential(
    store: Store, *, name: str, path: Path, url: str, projects=None, forget=False, read_only=False
):
    scopes = ["memory:read"] if read_only else ["memory:read", "memory:write"]
    if forget:
        scopes.append("memory:forget")
    if path.exists():
        if path.is_symlink():
            raise InputError("The connection file must not be a symbolic link.")
        current = read_connection(path)
        policy = authenticate(store, current["token"])
        if policy is not None and read_only and policy.scopes != frozenset(scopes):
            revoke_client(store, current["client_id"])
            path.unlink()
            return issue_client(
                store, name=name, scopes=scopes, projects=projects, days=365, output=path, url=url
            )["client_id"]
        if policy is not None:
            expected_projects = None if projects is None else tuple(sorted(set(projects)))
            if policy.projects != expected_projects or policy.scopes != frozenset(scopes):
                raise InputError(
                    "Existing client permissions differ; do not silently broaden them."
                )
            if current["url"] != url:
                write_json(path, {**current, "url": url})
            return current["client_id"]
        revoke_client(store, current["client_id"])
        path.unlink()
    return issue_client(
        store, name=name, scopes=scopes, projects=projects, days=365, output=path, url=url
    )["client_id"]


def stop_process(state: dict) -> None:
    if not active(state):
        return
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise InputError("Safe process stopping requires Linux pidfd support.")
    try:
        fd = os.pidfd_open(state["pid"])
    except ProcessLookupError:
        return
    try:
        if active(state):
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            deadline = time.monotonic() + 8
            while active(state) and time.monotonic() < deadline:
                time.sleep(0.05)
            if active(state):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
                deadline = time.monotonic() + 5
                while active(state) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if active(state):
                    raise InputError(
                        "The managed process has not stopped; its state was preserved."
                    )
    finally:
        os.close(fd)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(state["pid"], os.WNOHANG)


def state_path(store: Store) -> Path:
    return store.directory / "service.json"


def http_running(store: Store, *, timeout: float = 0) -> bool:
    if not store.directory.is_dir():
        return False
    try:
        with locked(store.directory / "http.lock", timeout=timeout):
            return False
    except InputError:
        return True


def bound_state(store: Store, state: dict) -> bool:
    return state.get("data_dir") == str(store.directory.resolve())


def private_endpoint(url: str) -> str:
    validate_endpoint(url)
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise InputError("Use this host's explicit loopback HTTP private MCP endpoint.")
    return url


def ensure_enabled(store: Store) -> None:
    marker = store.directory / "disabled.json"
    if marker.exists() or marker.is_symlink():
        raise InputError("This installation is disabled. Inspect its recovery or removal state.")


def resume_installation(store: Store) -> None:
    marker = store.directory / "disabled.json"
    if marker.is_symlink():
        raise InputError("A symbolic-link disable barrier was preserved; inspect doctor.")
    if marker.exists():
        state = read_json(marker)
        legacy = (
            state.get("version") == 1
            and state.get("disabled") is True
            and set(state) <= {"version", "disabled", "uninstalled_at"}
        )
        if not (legacy or state.get("version") == 1 and state.get("reason") == "uninstalled"):
            raise InputError("Recovery requires activate-restore; up --resume cannot bypass it.")
    marker.unlink(missing_ok=True)


@contextlib.contextmanager
def resuming(store: Store, *, requested: bool):
    """A failed explicit startup must retain its original uninstall barrier."""
    marker = store.directory / "disabled.json"
    if requested and marker.is_symlink():
        raise InputError("A symbolic-link disable barrier was preserved; inspect doctor.")
    original = marker.read_text() if requested and marker.exists() else None
    try:
        if requested:
            resume_installation(store)
        yield
    except BaseException:
        if original is not None and not marker.exists():
            atomic_write(marker, original)
        raise


def up(
    store: Store, *, port: int | None = None, semantic: bool = False, resume: bool = False
) -> dict:
    from .oauth import configuration

    if sys.platform != "linux":
        raise InputError(
            "Background startup currently requires Linux; use supervised serve elsewhere."
        )
    if port is not None and not 0 <= port <= 65535:
        raise InputError("Port must be between 0 and 65535; zero selects an available port.")
    with (
        locked(store.directory / "service.lock"),
        resuming(store, requested=resume),
        contextlib.ExitStack() as startup,
    ):
        ensure_enabled(store)
        store.initialize()
        state = read_json(state_path(store)) if state_path(store).exists() else {}
        if state and not bound_state(store, state):
            raise InputError("Service state is not bound to this data directory; it was preserved.")
        process = None
        oauth = configuration(store)
        issuer = oauth["issuer"] if oauth else None
        if active(state) and (
            state.get("version") != __version__ or state.get("oauth_issuer") != issuer
        ):
            stop_process(state)
        if active(state):
            if port not in (None, 0, state["port"]) or (semantic and not state["semantic"]):
                raise InputError("Stop the existing service before changing its runtime options.")
        else:
            # A dead leader can leave worker threads holding the HTTP lease briefly.
            if http_running(store, timeout=1 if state else 0):
                raise InputError(
                    "HTTP is externally managed for this data directory. Use connect --mcp-url "
                    "with its explicit private endpoint; up will not start another server."
                )
            selected_port = port if port is not None else state.get("port", 8765)
            with socket.socket() as reservation:
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                deadline = time.monotonic() + (3 if state else 0)
                while True:
                    try:
                        reservation.bind(("127.0.0.1", selected_port))
                        break
                    except OSError as exc:
                        if exc.errno != errno.EADDRINUSE:
                            raise
                        # After SIGKILL the leader can be gone before its worker threads
                        # finish releasing sockets. Keep the canonical port during recovery.
                        if state and time.monotonic() < deadline:
                            time.sleep(0.05)
                            continue
                        if port is not None or state:
                            raise InputError(
                                "The configured local port is already occupied."
                            ) from None
                        reservation.bind(("127.0.0.1", 0))
                        break
                selected_port = reservation.getsockname()[1]
            semantic = semantic or state.get("semantic", False)
            if semantic:
                from .semantic import verify_model_artifacts

                verify_model_artifacts(store)  # Validate without loading a second model instance.
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
                str(selected_port),
            ]
            if semantic:
                command.append("--semantic")
            log_path = store.directory / "service.log"
            if log_path.is_symlink():
                raise InputError("The managed log must not be a symbolic link.")
            if log_path.is_file() and log_path.stat().st_size >= 5 * 1024 * 1024:
                previous_log = log_path.with_name("service.log.1")
                if previous_log.is_symlink():
                    raise InputError("The rotated log must not be a symbolic link.")
                log_path.replace(previous_log)
            log_fd = os.open(
                log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log_fd,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                os.close(log_fd)
            state = {
                "version": __version__,
                "pid": process.pid,
                "process_start": process_identity(process.pid),
                "port": selected_port,
                "semantic": semantic,
                "url": f"http://127.0.0.1:{selected_port}/mcp",
                "oauth_issuer": issuer,
                "data_dir": str(store.directory.resolve()),
            }
            startup.callback(stop_process, state)
        probe = store.directory / "probe.connection.json"
        credential(
            store,
            name="installation-probe",
            path=probe,
            url=state["url"],
            projects=["__dots_brain_probe__"],
            read_only=True,
        )
        deadline = time.monotonic() + 15
        while True:
            try:
                result = asyncio.run(asyncio.wait_for(verify_connection(probe), timeout=3))
                if result["read"]:
                    break
            except Exception:
                pass
            if not active(state) or time.monotonic() >= deadline:
                raise InputError("The managed service did not pass its MCP readiness check.")
            time.sleep(0.1)
        if process is not None:
            write_json(state_path(store), state)
        startup.pop_all()
        return {
            "state": "verified_local_service",
            "url": state["url"],
            "pid": state["pid"],
            "data_dir": str(store.directory),
            "read": True,
            "write": "not_tested",
            "semantic": state["semantic"],
            "lifecycle": "background_process",
            "restart_on_vm_boot": False,
            "public_ingress": "not_configured",
            "oauth": "configured" if issuer else "not_configured",
            "configured_mcp_url": issuer + "/mcp" if issuer else None,
            "capture": "not_implemented",
        }


def down(store: Store) -> dict:
    with locked(store.directory / "service.lock"):
        release_timeout = 0
        if state_path(store).exists():
            state = read_json(state_path(store))
            if not bound_state(store, state):
                return {"state": "not_managed", "data_preserved": True}
            stop_process(state)
            release_timeout = 1
        return {
            "state": "externally_managed"
            if http_running(store, timeout=release_timeout)
            else "stopped",
            "data_preserved": True,
        }
