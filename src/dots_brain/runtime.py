"""Idempotent local startup without inventing VM persistence or ingress."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import errno
import os
import re
import select
import shlex
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
from .errors import BusyError, CapabilityError, InputError, StateError
from .installation_state import ensure_enabled, resume_uninstalled
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
        return state.get("version") in {"0.3.0a1", "0.3.0a2", "0.4.0a1", "0.4.0a2", "0.4.0a3"}
    return (
        state.get("data_dir") == directory
        and state.get("boot_id") == boot_identity()
        and state.get("executable") == executable
    )


def operator_command(store: Store, command: str) -> str:
    return f"dots-brain --data-dir {shlex.quote(str(store.directory))} {command}"


def _console_entrypoint(path: Path) -> bool:
    """Inspect current disk entrypoint evidence, never grant signaling authority.

    This is not proof of the script bytes originally executed by the process.
    """
    if not path.is_absolute() or path.name != "dots-brain" or not path.is_file():
        return False
    try:
        with path.open(encoding="utf-8") as stream:
            source = stream.read(8193)
        if len(source) > 8192:
            return False
        tree = ast.parse(source)
    except (OSError, UnicodeError, SyntaxError):
        return False
    imports = {
        ast.dump(ast.parse(value).body[0])
        for value in ("import sys", "import re", "from dots_brain.cli import main")
    }
    required = {
        ast.dump(ast.parse(value).body[0])
        for value in ("import sys", "from dots_brain.cli import main")
    }
    if not tree.body or any(ast.dump(node) not in imports for node in tree.body[:-1]):
        return False
    if not required <= {ast.dump(node) for node in tree.body[:-1]}:
        return False
    guard = tree.body[-1]
    expected_test = ast.parse('if __name__ == "__main__": pass').body[0].test
    if (
        not isinstance(guard, ast.If)
        or guard.orelse
        or ast.dump(guard.test) != ast.dump(expected_test)
    ):
        return False
    # uv/pip vary in argv suffix cleanup inside the guard; the imported callable
    # and terminal entrypoint remain the same. This evidence only blocks startup.
    return bool(guard.body) and ast.dump(guard.body[-1]) == ast.dump(
        ast.parse("sys.exit(main())").body[0]
    )


def unmanaged_writers(store: Store) -> list[dict]:
    """Observe untracked Python module/console servers for this directory on Linux.

    This bounded scan recognizes supported module/console invocations, not arbitrary
    wrappers, embedded API users or writers on other hosts. It never adopts or
    signals a process. Evidence includes the identity needed for reinspection.
    """
    if sys.platform != "linux":
        return []
    state = load_state(store)
    managed_pid = state.get("pid") if owns_process(store, state) else None
    boot = boot_identity()
    if boot is None:
        return []
    try:
        processes = list(Path("/proc").iterdir())
    except OSError:
        return []
    result = []
    for process in processes:
        if not process.name.isdigit():
            continue
        pid = int(process.name)
        if pid <= 1 or pid == managed_pid:
            continue
        start = process_identity(pid)
        if start is None:
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            arguments = [
                value.decode("utf-8", "surrogateescape")
                for value in (process / "cmdline").read_bytes().split(b"\0")
                if value
            ]
            module = arguments[1:3] == ["-m", "dots_brain.cli"]
            serve_arguments = []
            for argument in arguments[3 if module else 2 :]:
                option, separator, value = argument.partition("=")
                serve_arguments.extend(
                    [option, value] if argument.startswith("--") and separator else [argument]
                )
            if (
                len(serve_arguments) < 3
                or serve_arguments[0] != "--data-dir"
                or serve_arguments[2] != "serve"
                or not Path(serve_arguments[1]).is_absolute()
                or Path(serve_arguments[1]).resolve() != store.directory
                or not module
                and not _console_entrypoint(Path(arguments[1]))
            ):
                continue
            executable = os.readlink(process / "exe").removesuffix(" (deleted)")
            if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(executable).name):
                continue
            interpreter = Path(arguments[0])
            if interpreter.is_absolute() and str(interpreter.resolve()) != executable:
                continue
            if not interpreter.is_absolute() and not re.fullmatch(
                r"python(?:\d+(?:\.\d+)*)?", arguments[0]
            ):
                continue
            options = iter(serve_arguments[3:])
            transport = "stdio"
            for option in options:
                if option in {"--semantic", "--public-gateway"}:
                    continue
                value = next(options)
                if option == "--transport" and value in {"http", "stdio"}:
                    transport = value
                elif option in {"--port", "--public-port", "--listen-fd"} and (
                    value.isdigit() and 0 <= int(value) <= 65535
                ):
                    continue
                else:
                    raise ValueError
            # Recheck after reading argv/executable: never reuse stale PID data.
            if (
                process_identity(pid) != start
                or boot_identity() != boot
                or os.readlink(process / "exe").removesuffix(" (deleted)") != executable
            ):
                continue
            result.append(
                {
                    "pid": pid,
                    "process_start": start,
                    "boot_id": boot,
                    "executable": executable,
                    "transport": transport,
                    "stop_command": f"kill -TERM {pid}",
                }
            )
        except (OSError, ValueError, StopIteration):
            continue
    return sorted(result, key=lambda value: value["pid"])


def require_no_unmanaged_writers(store: Store) -> None:
    processes = unmanaged_writers(store)
    if processes:
        raise BusyError(
            "Unmanaged serve process(es) still use this installation: PID(s) "
            + ", ".join(str(process["pid"]) for process in processes)
            + ". Stop them with their supervisor, or after rechecking the PID, "
            "run: " + "; ".join(process["stop_command"] for process in processes)
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
            "Identify the running process before repairing service.json. Next command: "
            + operator_command(store, "doctor")
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
            raise StateError(
                "The connection file must not be a symbolic link. Next command: "
                + operator_command(store, "doctor")
            )
        try:
            current = read_connection(path)
        except InputError:
            if not replace_invalid:
                raise StateError(
                    f"Existing client credential {path} is corrupt; it was preserved. "
                    "Review its grants, then preserve the file with mv -n "
                    + shlex.quote(str(path))
                    + " "
                    + shlex.quote(str(path) + ".before-replacement")
                    + " and retry the original command. Next command: "
                    + operator_command(store, "client list")
                ) from None
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
                raise StateError(
                    "Credential identity does not match its authenticated client. Next command: "
                    + operator_command(store, "client list")
                )
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
                    "Existing client permissions differ. Revoke this credential with "
                    + operator_command(store, "client revoke " + shlex.quote(current["client_id"]))
                    + "; preserve its file with mv -n "
                    + shlex.quote(str(path))
                    + " "
                    + shlex.quote(str(path) + ".before-replacement")
                    + ", then retry the original command to issue the requested permissions."
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
            raise StateError(
                "Existing client credential is expired or invalid; it was preserved. "
                "Review its grant before replacing the credential file. Next command: "
                + operator_command(store, "client list")
            )
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
        raise StateError(
            "Managed service state does not identify this installation; it was preserved. "
            "Next command: " + operator_command(store, "doctor")
        )
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise CapabilityError(
            "Safe process stopping requires Linux pidfd support. Next command: "
            + operator_command(store, "doctor")
        )
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
            raise StateError(
                "Managed process identity changed; it was preserved. Next command: "
                + operator_command(store, "doctor")
            )
        with contextlib.suppress(ProcessLookupError):
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        # /proc command/executable metadata can disappear before leases are
        # released. The already verified pidfd tracks exit without PID reuse.
        if not exited.poll(8000):
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            if not exited.poll(3000):
                raise BusyError(
                    "The managed service did not stop after SIGKILL. Next command: "
                    + operator_command(store, "doctor")
                )
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
                    raise BusyError(
                        "The configured local port is already occupied. Next command: "
                        f"lsof -nP -iTCP:{selected}"
                    ) from None
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
        raise StateError(
            "Managed service log must not be a symbolic link. Next command: "
            + operator_command(store, "doctor")
        )
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
            for name in ("http.lock", "writers.lock"):
                lease = lock_status(store.directory / name)
                if lease["state"] != "held" or (
                    name == "writers.lock" and lease.get("mode") != "exclusive"
                ):
                    continue
                owners = lease["owners"]
                if state["pid"] in owners or (not owners and active(state)):
                    continue  # A live child's own lease does not explain unreadiness.
                detail = f" by PID(s) {', '.join(map(str, owners))}" if owners else ""
                next_command = (
                    f"kill -TERM {owners[0]}" if owners else operator_command(store, "doctor")
                )
                raise BusyError(
                    f"Managed startup is blocked by {lease.get('mode', 'unknown')} "
                    f"lease {name}{detail}. "
                    "Recheck the owner and stop it with its supervisor; next command: "
                    + next_command
                )
            raise StateError(
                "The managed service did not pass its MCP readiness check. Next command: "
                + operator_command(store, "doctor")
                + "; inspect the local service log with tail -n 40 "
                + shlex.quote(str(store.directory / "service.log"))
            )
        time.sleep(0.1)


def up(
    store: Store, *, port: int | None = None, semantic: bool | None = None, resume: bool = False
) -> dict:
    from .oauth import configuration

    if sys.platform != "linux":
        raise CapabilityError(
            "Background startup currently requires Linux; use supervised serve elsewhere. "
            "Next command: " + operator_command(store, "serve --help")
        )
    if port is not None and not 0 <= port <= 65535:
        raise InputError("Port must be between 0 and 65535; zero selects an available port.")
    with (
        locked(store.directory / "service.lock", create_parent=True),
        resume_uninstalled(store, requested=resume),
    ):
        ensure_enabled(store)
        require_no_unmanaged_writers(store)
        store.initialize()
        state = load_state(store)
        if state and (
            type(state.get("port")) is not int
            or not 1 <= state["port"] <= 65535
            or type(state.get("semantic")) is not bool
            or state.get("url") != f"http://127.0.0.1:{state['port']}/mcp"
        ):
            raise StateError(
                "Managed service endpoint/options are invalid; state was preserved. "
                "Next command: " + operator_command(store, "doctor")
            )
        oauth = configuration(store)
        issuer = oauth["issuer"] if oauth else None
        managed = owns_process(store, state)
        if active(state) and not managed:
            raise StateError(
                "Managed service state refers to a foreign process; it was preserved. "
                "Next command: " + operator_command(store, "doctor")
            )
        if managed and (state.get("version") != __version__ or state.get("oauth_issuer") != issuer):
            raise StateError(
                f"Managed service PID {state['pid']} version or OAuth issuer differs. "
                "Next command: " + operator_command(store, "down")
            )
        if managed:
            if port not in (None, 0, state["port"]) or (
                semantic is not None and semantic != state["semantic"]
            ):
                raise StateError(
                    f"Managed service PID {state['pid']} has different runtime options. "
                    "Next command: " + operator_command(store, "down")
                )
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
    processes = unmanaged_writers(store)
    result = {"state": status, "writers": lock_status(store.directory / "writers.lock")}
    if processes:
        result.update(
            state="unmanaged_writers",
            processes=processes,
            next_command=processes[0]["stop_command"],
        )
    return result


def down(store: Store) -> dict:
    if not store.directory.exists():
        processes = unmanaged_writers(store)
        if processes:
            return {
                "state": "external_writers_remain",
                "processes": processes,
                "next_command": processes[0]["stop_command"],
                "data_preserved": True,
            }
        return {
            "state": "writer_state_unknown",
            "data_preserved": True,
            "evidence": "Installation directory is absent; no process or lease proves stopped.",
            "next_command": operator_command(store, "doctor"),
        }
    with locked(store.directory / "service.lock"):
        if state_path(store).exists():
            stop_process(store, load_state(store))
        leases = lock_status(store.directory / "writers.lock")
        processes = unmanaged_writers(store)
        result = {
            "state": "stopped"
            if leases["state"] in {"absent", "free"} and not processes
            else "writer_state_unknown"
            if leases["state"] == "unknown" and not processes
            else "external_writers_remain",
            "writers": leases,
            "data_preserved": True,
        }
        if processes:
            result.update(processes=processes, next_command=processes[0]["stop_command"])
        elif leases["state"] == "unknown":
            result.update(
                next_command="lsof " + shlex.quote(str(store.directory / "writers.lock")),
                evidence="Kernel flock inspection is unavailable; writer presence is unknown.",
            )
        return result
