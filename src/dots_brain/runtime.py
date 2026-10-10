"""Idempotent local startup without inventing VM persistence or ingress."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .auth import authenticate, issue_client, read_connection, revoke_client
from .bridge import verify_connection
from .errors import InputError, StateError, StoreDisabledError
from .installation_state import marker_path, resume_uninstalled
from .local import lock_status, locked, read_json, write_json
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
    """Match PID start time and the exact managed command before signaling it.

    Historical state lacks boot/executable fields. Its recognized release and
    original command still bind it to this installation; up requires explicit
    down before replacing a different release.
    """
    if not active(state) or type(state.get("port")) is not int:
        return False
    directory = str(store.directory)
    try:
        arguments = Path(f"/proc/{state['pid']}/cmdline").read_bytes().split(b"\0")
        arguments = [value.decode("utf-8", "surrogateescape") for value in arguments if value]
        executable = os.readlink(f"/proc/{state['pid']}/exe").removesuffix(" (deleted)")
    except OSError:
        return False
    expected = [
        "-m",
        "dots_brain.cli",
        "--data-dir",
        directory,
        "serve",
        "--transport",
        "http",
        "--port",
        str(state["port"]),
    ]
    if arguments[1:10] != expected or type(state.get("semantic")) is not bool:
        return False
    tail = arguments[10:]
    if len(tail) >= 2 and tail[0] == "--listen-fd" and tail[1].isdigit():
        tail = tail[2:]
    if tail != (["--semantic"] if state["semantic"] else []):
        return False
    identity_fields = {"data_dir", "executable", "boot_id"}
    if not identity_fields.intersection(state):
        return state.get("version") in {"0.3.0a2", "0.4.0a1"}
    return (
        state.get("data_dir") == directory
        and state.get("boot_id") == boot_identity()
        and state.get("executable") == executable
    )


def load_state(store: Store) -> dict:
    path = state_path(store)
    if not path.exists():
        return {}
    try:
        value = read_json(path)
        if type(value.get("pid")) is not int or not isinstance(value.get("process_start"), str):
            raise ValueError
        return value
    except (OSError, ValueError, InputError):
        raise StateError(
            "Managed service state is corrupt; it was preserved. "
            "Identify the running process before repairing service.json."
        ) from None


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
            if policy.principal != f"local-client:{current['client_id']}":
                raise StateError("Credential identity does not match its authenticated client.")
            same_permissions = policy.projects == expected_projects and policy.scopes == frozenset(
                scopes
            )
            installation_probe = (
                name == "installation-probe"
                and path == store.directory / "probe.connection.json"
                and expected_projects == ("__dots_brain_probe__",)
                and scopes == ["memory:read"]
                and policy.projects == expected_projects
                and policy.scopes == frozenset({"memory:read", "memory:write", "memory:forget"})
            )
            if not same_permissions and not installation_probe:
                raise StateError(
                    "Existing client permissions differ; explicitly replace this client."
                )
            if same_permissions:
                if current["url"] != url:
                    write_json(path, {**current, "url": url})
                return current["client_id"]
            # Revocation precedes removal: interruption cannot leave a wider probe usable.
            revoke_client(store, current["client_id"])
            path.unlink()
            return issue_client(
                store, name=name, scopes=scopes, projects=projects, days=365, output=path, url=url
            )["client_id"]
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
        exited = select.poll()
        exited.register(fd, select.POLLIN)
        if not owns_process(store, state):
            if exited.poll(0):
                return
            raise InputError("Managed process identity changed; it was preserved.")
        with contextlib.suppress(ProcessLookupError):
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        # /proc command/executable metadata can disappear before leases are
        # released. The already verified pidfd tracks exit without PID reuse.
        if not exited.poll(8000):
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            if not exited.poll(3000):
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
    if marker_path(store).exists():
        raise StoreDisabledError(
            "This installation is disabled. Inspect its recovery or removal state."
        )


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


@contextlib.contextmanager
def reserve_listener(port: int | None, previous: dict):
    """Keep the bound socket open until the child inherits it; never change a saved port."""
    selected = port if port is not None else previous.get("port", 8765)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        deadline = time.monotonic() + (3 if previous else 0)
        while True:
            try:
                listener.bind(("127.0.0.1", selected))
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                # A killed process can leave worker threads briefly releasing sockets.
                if previous and time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                if port is not None or previous:
                    raise InputError("The configured local port is already occupied.") from None
                listener.bind(("127.0.0.1", 0))
                break
        listener.listen(128)
        yield listener


def start_daemon(store: Store, previous: dict, *, port, semantic, issuer) -> dict:
    """Reserve the endpoint, start one child and return its process identity."""
    semantic = previous.get("semantic", False) if semantic is None else semantic
    if semantic:
        from .semantic import verify_model_artifacts

        verify_model_artifacts(store)  # Do not load a second model in the parent.
    log_path = store.directory / "service.log"
    if log_path.is_symlink():
        raise InputError("Managed service log must not be a symbolic link.")
    if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
        os.replace(log_path, log_path.with_name("service.log.1"))
    with reserve_listener(port, previous) as listener:
        selected_port = listener.getsockname()[1]
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
            str(listener.fileno()),
        ]
        if semantic:
            command.append("--semantic")
        log_fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_fd,
                start_new_session=True,
                close_fds=True,
                pass_fds=(listener.fileno(),),
                cwd=store.directory,
                env=daemon_environment(),
            )
            schedule_reap(process)
        finally:
            os.close(log_fd)
    return {
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


def verify_daemon(store: Store, state: dict) -> None:
    """Require a real MCP read and matching process identity before recording startup."""
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
    with (
        locked(store.directory / "service.lock", create_parent=True),
        resume_uninstalled(store, requested=resume),
    ):
        ensure_enabled(store)
        store.initialize()
        state = load_state(store)
        if state and (
            type(state.get("port")) is not int
            or not 1 <= state["port"] <= 65535
            or type(state.get("semantic")) is not bool
            or state.get("url") != f"http://127.0.0.1:{state['port']}/mcp"
        ):
            raise StateError("Managed service endpoint/options are invalid; state was preserved.")
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
            state = start_daemon(store, state, port=port, semantic=semantic, issuer=issuer)
        started = not managed
        try:
            verify_daemon(store, state)
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
            "capture": "explicit_writes_and_opt_in_snapshots",
        }


def managed_status(store: Store) -> dict:
    """Report only validated process metadata and observed server leases."""
    state = load_state(store)
    status = (
        "not_managed"
        if not state
        else (
            "running"
            if owns_process(store, state)
            else "foreign_process"
            if active(state)
            else "stopped"
        )
    )
    return {"state": status, "writers": lock_status(store.directory / "writers.lock")}


def down(store: Store) -> dict:
    if not store.directory.exists():
        return {"state": "stopped", "data_preserved": True}
    with locked(store.directory / "service.lock"):
        if state_path(store).exists():
            stop_process(store, load_state(store))
        leases = lock_status(store.directory / "writers.lock")
        return {
            "state": "stopped"
            if leases["state"] in {"absent", "free"}
            else "external_writers_remain",
            "writers": leases,
            "data_preserved": True,
        }
