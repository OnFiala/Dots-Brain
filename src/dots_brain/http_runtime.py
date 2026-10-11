"""Listener policy and bounded diagnostic state for one supervised HTTP host."""

from __future__ import annotations

import contextlib
import os
import shlex
import socket
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import InputError
from .local import lock_status, read_json, write_json

LISTENER_STATE_VERSION = 1


@dataclass(frozen=True)
class ListenerPlan:
    port: int
    public_port: int | None = None
    public_gateway: bool = False

    def __post_init__(self):
        if type(self.port) is not int:
            raise InputError("Choose the private or single-listener TCP port with serve --port.")
        for value in (self.port, self.public_port):
            if value is not None and (type(value) is not int or not 1 <= value <= 65535):
                raise InputError(
                    "Choose a TCP port from 1 to 65535 with serve --port/--public-port."
                )
        if self.public_port is not None and (self.public_gateway or self.public_port == self.port):
            raise InputError(
                "Use serve --port PRIVATE_PORT --public-port PUBLIC_PORT with distinct ports, "
                "or --public-gateway for one OAuth-only listener."
            )

    @property
    def mode(self):
        return (
            "dual"
            if self.public_port is not None
            else "public"
            if self.public_gateway
            else "private"
        )

    def policy_for(self, scope):
        if self.public_port is None:
            return "public" if self.public_gateway else "private"
        address = scope.get("server")
        port = address[1] if isinstance(address, (tuple, list)) and len(address) == 2 else None
        if port == self.public_port:
            return "public"
        return "private" if port == self.port else "unknown"

    def warnings(self, config, directory=None):
        if (
            config
            and self.mode == "private"
            and urlsplit(config["issuer"]).hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            data = f" --data-dir {shlex.quote(str(directory))}" if directory is not None else ""
            public_port = 8787 if self.port != 8787 else 8788
            return [
                {
                    "code": "public_listener_missing",
                    "message": "A public OAuth issuer has only a private listener.",
                    "next_command": (
                        f"dots-brain{data} serve --transport http "
                        f"--port {self.port} --public-port {public_port}"
                    ),
                }
            ]
        return []


@contextlib.contextmanager
def listener_sockets(plan, inherited_fd=None):
    """Reserve both endpoints before serving; no process can choose policy via headers."""
    with contextlib.ExitStack() as stack:
        if inherited_fd is None:
            private = stack.enter_context(socket.create_server(("127.0.0.1", plan.port)))
        else:
            private = stack.enter_context(socket.socket(fileno=os.dup(inherited_fd)))
            if private.getsockname() != ("127.0.0.1", plan.port) or not private.getsockopt(
                socket.SOL_SOCKET, socket.SO_ACCEPTCONN
            ):
                raise InputError("The inherited listener is invalid; run dots-brain down then up.")
        sockets = [private]
        if plan.public_port is not None:
            sockets.append(
                stack.enter_context(socket.create_server(("127.0.0.1", plan.public_port)))
            )
        yield sockets


@contextlib.contextmanager
def record_listeners(store, plan, config):
    """Versioned diagnostic cache only; authentication never reads this file."""
    path = store.directory / "http-listeners.json"
    instance = uuid.uuid4().hex
    value = {
        "version": LISTENER_STATE_VERSION,
        "instance": instance,
        "pid": os.getpid(),
        "mode": plan.mode,
        "private_port": None if plan.public_gateway else plan.port,
        "public_port": plan.port if plan.public_gateway else plan.public_port,
        "public_issuer": bool(plan.warnings(config)),
    }
    write_json(path, value)
    try:
        yield
    finally:
        # Retain the last configuration for diagnosis, explicitly marked stopped.
        # A killed host may leave this unset; status also checks the HTTP lease.
        try:
            if read_json(path).get("instance") == instance:
                write_json(path, {**value, "stopped": True})
        except (OSError, InputError):
            pass


def listener_status(store):
    from .oauth import configuration_status

    path = store.directory / "http-listeners.json"
    if not path.exists():
        return {"state": "not_recorded", "mode": "unknown"}
    try:
        value = read_json(path)
        if type(value.get("version")) is not int or value["version"] != LISTENER_STATE_VERSION:
            raise InputError("Unsupported listener diagnostic version.")
        private, public = value.get("private_port"), value.get("public_port")
        plan = ListenerPlan(
            private if private is not None else public,
            public if private else None,
            public_gateway=private is None,
        )
        if value.get("mode") != plan.mode or type(value.get("pid")) is not int:
            raise InputError("Invalid listener diagnostic state.")
    except (OSError, InputError, ValueError):
        return {"state": "invalid", "mode": "unknown", "next_command": "dots-brain serve --help"}
    lease = lock_status(store.directory / "http.lock")
    state = (
        "stopped"
        if value.get("stopped") or lease["state"] in {"free", "absent"}
        else "recorded"
        if lease["state"] == "held"
        else "unknown"
    )
    config = configuration_status(store)
    return {
        "state": state,
        "mode": value["mode"],
        "private_port": private,
        "public_port": public,
        "pid": value["pid"],
        "process_verified": state == "recorded" and value["pid"] in lease["owners"],
        **(
            {"next_command": shlex.join(["lsof", str(store.directory / "http.lock")])}
            if state == "unknown"
            else {}
        ),
        "readiness": "not_probed",
        "warnings": plan.warnings(
            config if config["state"] == "configured" else None,
            store.directory,
        ),
    }
