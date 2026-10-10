"""Idempotent local startup without inventing VM persistence or ingress."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import __version__
from .auth import authenticate, issue_client, read_connection, revoke_client
from .bridge import verify_connection
from .errors import InputError, StoreDisabledError
from .installation_state import resume_uninstalled
from .local import locked, read_json, write_json
from .store import Store


def inspect_network_policy(path: Path | None) -> dict:
    """Inspect an operator-supplied policy without assuming a host vendor layout."""
    if path is None:
        return {
            "state": "not_supplied",
            "managed": False,
            "tcp_destinations_configured": "unknown",
        }
    if not path.is_file():
        return {
            "state": "unavailable",
            "managed": "unknown",
            "tcp_destinations_configured": "unknown",
        }
    try:
        policy = read_json(path)
        version = policy.get("version")
        tcp = policy.get("tcp_network_access", {})
        if type(version) is not int or version != 1 or not isinstance(tcp, dict):
            raise ValueError
        domains = tcp.get("domains", [])
        ranges = tcp.get("ip_ranges", [])
        if not isinstance(domains, list) or not isinstance(ranges, list):
            raise ValueError
    except (OSError, ValueError, InputError):
        return {
            "state": "invalid",
            "managed": "unknown",
            "tcp_destinations_configured": "unknown",
        }
    return {
        "state": "checked",
        "managed": True,
        "tcp_destinations_configured": bool(domains or ranges),
    }


def preflight(network_policy: Path | None = None) -> dict:
    policy = inspect_network_policy(network_policy)
    managed = policy["managed"]
    systemd = Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None
    return {
        "platform": sys.platform,
        "background_process": sys.platform == "linux",
        "systemd_detected": bool(systemd),
        "managed_network": managed,
        "network_policy": policy["state"],
        "tcp_destinations_configured": policy["tcp_destinations_configured"],
        "public_ingress": "not_configured",
        "tunnel_reason": "The managed host has no configured TCP destinations."
        if managed is True and not policy["tcp_destinations_configured"]
        else "A supported ingress must be configured and verified on this host.",
        "vm_persistence": "not_verified",
        "secret_isolation": False,
    }


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


def boot_identity() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def owns_process(store: Store, state: dict) -> bool:
    """Prove that a state file names this store's managed Python process."""
    if not active(state):
        return False
    expected_directory = str(store.directory.resolve())
    claimed_executable = state.get("executable")
    if not isinstance(claimed_executable, str) or not os.path.isabs(claimed_executable):
        return False
    try:
        # A newer virtual environment must still be able to stop a process
        # started by the previous environment.  The state value is safe only
        # after it agrees with the live process and the installation identity.
        expected_executable = str(Path(claimed_executable).resolve(strict=True))
    except OSError:
        return False
    if state.get("data_dir") != expected_directory or state.get("boot_id") != boot_identity():
        return False
    try:
        command = Path(f"/proc/{state['pid']}/cmdline").read_bytes().split(b"\0")
        executable = str(Path(f"/proc/{state['pid']}/exe").resolve())
    except OSError:
        return False
    arguments = [item.decode("utf-8", "surrogateescape") for item in command if item]
    try:
        directory_argument = arguments.index("--data-dir")
    except ValueError:
        return False
    return (
        executable == expected_executable
        and len(arguments) >= 6
        and arguments[1:3] == ["-m", "dots_brain.cli"]
        and directory_argument + 1 < len(arguments)
        and arguments[directory_argument + 1] == expected_directory
        and "serve" in arguments
    )


def load_state(store: Store) -> dict:
    path = state_path(store)
    if not path.exists():
        return {}
    try:
        value = read_json(path)
        if not isinstance(value.get("pid", 0), int) or isinstance(value.get("pid"), bool):
            raise InputError("Managed service state has an invalid pid.")
        return value
    except (OSError, ValueError, InputError):
        quarantine = path.with_name(path.name + ".corrupt-" + uuid.uuid4().hex)
        try:
            os.replace(path, quarantine)
        except OSError as exc:
            raise InputError("Cannot quarantine corrupt managed service state.") from exc
        return {"_corrupt_state": str(quarantine)}


def credential(
    store: Store,
    *,
    name: str,
    path: Path,
    url: str,
    projects=None,
    write: bool = True,
    forget=False,
    replace_invalid: bool = False,
):
    scopes = (
        ["memory:read"]
        + (["memory:write"] if write else [])
        + (["memory:forget"] if forget else [])
    )
    if path.exists():
        if path.is_symlink():
            raise InputError("The connection file must not be a symbolic link.")
        try:
            current = read_connection(path)
        except InputError:
            if not replace_invalid:
                raise
            path.unlink()
            current = None
        if current is None:
            return issue_client(
                store, name=name, scopes=scopes, projects=projects, days=365, output=path, url=url
            )["client_id"]
        policy = authenticate(store, current["token"])
        if policy is not None:
            expected_projects = None if projects is None else tuple(sorted(set(projects)))
            if policy.projects != expected_projects or policy.scopes != frozenset(scopes):
                raise InputError(
                    "Existing client permissions differ; do not silently broaden them."
                )
            if current["url"] != url:
                write_json(path, {**current, "url": url})
            return current["client_id"]
        if not replace_invalid:
            raise InputError("Existing client credential is expired or invalid; it was preserved.")
        with contextlib.suppress(InputError):
            revoke_client(store, current["client_id"])
        path.unlink()
    return issue_client(
        store, name=name, scopes=scopes, projects=projects, days=365, output=path, url=url
    )["client_id"]


def stop_process(store: Store, state: dict) -> None:
    if not active(state):
        return
    if not owns_process(store, state):
        raise InputError(
            "Managed service state does not identify this installation; it was preserved."
        )
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise InputError("Safe process stopping requires Linux pidfd support.")
    try:
        fd = os.pidfd_open(state["pid"])
    except ProcessLookupError:
        return
    try:
        if owns_process(store, state):
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            deadline = time.monotonic() + 8
            while owns_process(store, state) and time.monotonic() < deadline:
                time.sleep(0.05)
            if owns_process(store, state):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
                deadline = time.monotonic() + 3
                while owns_process(store, state) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if owns_process(store, state):
                    raise InputError("The managed service did not stop after SIGKILL.")
    finally:
        os.close(fd)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(state["pid"], os.WNOHANG)


def state_path(store: Store) -> Path:
    return store.directory / "service.json"


@contextlib.contextmanager
def serve_locks(store: Store, *, http: bool):
    """Hold service leases for the full supervised server lifetime."""
    with locked(store.directory / "writers.lock", timeout=0, shared=True):
        if http:
            with locked(store.directory / "http.lock", timeout=0):
                yield
        else:
            yield


def ensure_enabled(store: Store) -> None:
    if (store.directory / "disabled.json").exists():
        raise StoreDisabledError(
            "This installation is disabled. Inspect its recovery or removal state."
        )


def resume_installation(store: Store) -> None:
    resume_uninstalled(store)


def daemon_environment() -> dict[str, str]:
    """Pass only locale and home state that the managed interpreter needs."""
    allowed = {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["PATH"] = os.defpath
    return environment


def reap_process(process: subprocess.Popen) -> None:
    """Retain and reap a detached child without fabricating its exit status."""
    with contextlib.suppress(OSError):
        process.wait()


def schedule_reap(process: subprocess.Popen) -> None:
    """Avoid Popen finalizer warnings while preserving the actual child status."""
    threading.Thread(target=reap_process, args=(process,), daemon=True).start()


def up(
    store: Store, *, port: int | None = None, semantic: bool | None = None, resume: bool = False
) -> dict:
    from .oauth import configuration

    if sys.platform != "linux":
        raise InputError(
            "Background startup currently requires Linux; use supervised serve elsewhere."
        )
    if port is not None and not 0 <= port <= 65535:
        raise InputError("Port must be between 0 and 65535; zero selects an available port.")
    with locked(store.directory / "service.lock", create_parent=True):
        if resume:
            resume_installation(store)
        ensure_enabled(store)
        store.initialize()
        state = load_state(store)
        if state.get("_corrupt_state"):
            raise InputError(
                "Managed service state was corrupt and has been quarantined; "
                "inspect the running process before starting a replacement."
            )
        oauth = configuration(store)
        issuer = oauth["issuer"] if oauth else None
        managed = owns_process(store, state)
        if active(state) and not managed:
            raise InputError("Managed service state refers to a foreign process; it was preserved.")
        if managed and (state.get("version") != __version__ or state.get("oauth_issuer") != issuer):
            raise InputError(
                "Managed service version or OAuth issuer differs; "
                "stop it explicitly before changing it."
            )
        if managed:
            if port not in (None, 0, state["port"]) or (
                semantic is not None and semantic != state["semantic"]
            ):
                raise InputError("Stop the existing service before changing its runtime options.")
        else:
            selected_port = port if port is not None else state.get("port", 8765)
            listen_fd: int | None = None
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
                reservation.listen(128)
                listen_fd = os.dup(reservation.fileno())
                os.set_inheritable(listen_fd, True)
            try:
                semantic = state.get("semantic", False) if semantic is None else semantic
                if semantic:
                    from .semantic import verify_model_artifacts

                    # Validate without loading a second model instance.
                    verify_model_artifacts(store)
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
                    "--listen-fd",
                    str(listen_fd),
                ]
                if semantic:
                    command.append("--semantic")
                log_path = store.directory / "service.log"
                if log_path.is_symlink():
                    raise InputError("Managed service log must not be a symbolic link.")
                if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
                    os.replace(log_path, log_path.with_name("service.log.1"))
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
                        pass_fds=(listen_fd,),
                        cwd=store.directory,
                        env=daemon_environment(),
                    )
                    schedule_reap(process)
                finally:
                    os.close(log_fd)
            finally:
                if listen_fd is not None:
                    os.close(listen_fd)
            state = {
                "version": __version__,
                "pid": process.pid,
                "process_start": process_identity(process.pid),
                "port": selected_port,
                "semantic": semantic,
                "url": f"http://127.0.0.1:{selected_port}/mcp",
                "oauth_issuer": issuer,
                "data_dir": str(store.directory.resolve()),
                "executable": str(Path(sys.executable).resolve()),
                "boot_id": boot_identity(),
            }
        started = "process" in locals()
        try:
            probe = store.directory / "probe.connection.json"
            credential(
                store,
                name="installation-probe",
                path=probe,
                url=state["url"],
                projects=["__dots_brain_probe__"],
                write=False,
                replace_invalid=True,
            )
            deadline = time.monotonic() + 15
            while True:
                try:
                    result = asyncio.run(asyncio.wait_for(verify_connection(probe), timeout=3))
                    if result["read"] and owns_process(store, state):
                        break
                except Exception:
                    pass
                if not owns_process(store, state) or time.monotonic() >= deadline:
                    raise InputError("The managed service did not pass its MCP readiness check.")
                time.sleep(0.1)
            if started:
                write_json(state_path(store), state)
        except BaseException:
            if started and owns_process(store, state):
                stop_process(store, state)
            raise
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
    if not store.directory.exists():
        return {"state": "stopped", "data_preserved": True}
    with locked(store.directory / "service.lock"):
        if state_path(store).exists():
            stop_process(store, load_state(store))
        return {"state": "stopped", "data_preserved": True}
