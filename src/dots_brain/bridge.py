"""A local stdio client for the same authenticated remote memory service."""

import asyncio
import os
import re
import shlex
import stat
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequest, CallToolRequestParams, JSONRPCRequest

from . import __version__
from .auth import authenticate, read_connection
from .errors import BrainError, BusyError, InputError
from .local import publish_new, read_json, sync_directory, write_json
from .protocol import INSTRUCTIONS, error_result, protect_tool_dispatch, safe_error

PROBE_SOURCE = "dots-brain-probe"
PROBE_ACCOUNT = "connection-verifier"
WRITE_TOOLS = frozenset(
    {"memory_remember", "memory_forget", "audit_record", "cortex_publish", "cortex_reconcile"}
)
# Match create_server's transport cap. HTTPx owns serialization in both places.
MAX_REQUEST_BYTES = 1048576


def _check_request_size(name, arguments, *, request_id=0):
    if name is None:
        return
    try:
        request = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments))
        message = JSONRPCRequest(
            jsonrpc="2.0",
            id=request_id,
            **request.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
        payload = message.model_dump(by_alias=True, mode="json", exclude_none=True)
        size = len(httpx.Request("POST", "http://127.0.0.1/mcp", json=payload).content)
    except (TypeError, ValueError):
        raise InputError("Arguments cannot be serialized as a valid MCP request.") from None
    if size > MAX_REQUEST_BYTES:
        raise BridgeFailure(
            {
                "code": "request_too_large",
                "message": "The serialized request exceeds the service limit; reduce its size.",
            }
        )


def _probe_command(path, action="--recover-probe"):
    return f"dots-brain verify --credential-file {shlex.quote(str(path))} {action}"


def _probe_receipt_path(path: Path) -> Path:
    """Keep a recoverable probe receipt beside its private credential."""
    return path.with_name(path.name + ".probe-recovery.json")


def _probe_identity(path: Path) -> tuple[str, Path]:
    """Reuse an unfinished probe; a completed verification gets a fresh identity."""
    receipt = _probe_receipt_path(path)
    if receipt.exists():
        event_id = read_json(receipt).get("event_id", "")
        if not isinstance(event_id, str) or not re.fullmatch(
            r"connection-probe-[a-f0-9]{32,64}", event_id
        ):
            raise InputError(
                "The unfinished probe receipt is invalid; preserve it for review. "
                f"Next command: {_probe_command(path, '--archive-probe-receipt')}"
            )
        return event_id, receipt
    return "connection-probe-" + uuid4().hex, receipt


def _entry_credential_path(entry: dict) -> Path | None:
    args = entry.get("args")
    if not isinstance(args, list):
        return None
    try:
        index = args.index("--credential-file")
        value = args[index + 1]
    except (ValueError, IndexError):
        return None
    return Path(value) if isinstance(value, str) else None


def _write_probe_receipt(path: Path | None, receipt: dict) -> None:
    if path is not None:
        write_json(path, receipt)


def _clear_probe_receipt(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


def _pending_probe(path: Path | None, *, event_id: str, project: str) -> None:
    if path is None or not path.exists():
        return
    receipt = read_json(path)
    if (
        receipt.get("version") != 1
        or receipt.get("event_id") != event_id
        or receipt.get("project") != project
        or receipt.get("source") != PROBE_SOURCE
        or receipt.get("account") != PROBE_ACCOUNT
    ):
        raise InputError(
            "A different unresolved connection probe exists; it was preserved. "
            "Next command: dots-brain verify --credential-file <credential> --recover-probe"
        )
    raise InputError(
        "An unresolved connection probe exists; its receipt was preserved. "
        "Next command: dots-brain verify --credential-file <credential> --recover-probe"
    )


def resume_local_connection(path: Path, directory: Path) -> None:
    """Resume only an existing service matched by its local credential and endpoint."""
    from .local import read_json
    from .runtime import ensure_enabled, state_path, up
    from .store import Store

    store = Store(directory)
    ensure_enabled(store)
    store.status()  # Validate the existing store without initializing a new one.
    connection = read_connection(path)
    if authenticate(store, connection["token"]) is None:
        raise InputError("The connection credential is not valid for this local store.")
    if not state_path(store).is_file():
        raise InputError("No managed service state exists; configure the local service explicitly.")
    state = read_json(state_path(store))
    port = state.get("port")
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or state.get("url") != f"http://127.0.0.1:{port}/mcp"
        or connection["url"] != state["url"]
    ):
        raise InputError("The connection endpoint does not match the existing local service.")
    up(store)


@asynccontextmanager
async def connect(path: Path):
    connection = read_connection(path)
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + connection["token"]},
        timeout=30,
        follow_redirects=False,
        trust_env=False,
    ) as http:
        async with streamable_http_client(connection["url"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def verify_connection(
    path: Path,
    *,
    write: bool = False,
    project: str = "default",
    cleanup_store=None,
    recover_probe=False,
    probe_memory_id=None,
    probe_revision=None,
    archive_probe_receipt=False,
) -> dict:
    if (
        sum(bool(value) for value in (write, recover_probe, archive_probe_receipt)) > 1
        or (probe_memory_id is not None) != (probe_revision is not None)
        or ((probe_memory_id is not None or probe_revision is not None) and not recover_probe)
    ):
        raise InputError(
            "Choose one verification action and supply recovery ID/revision together. "
            f"Next command: {_probe_command(path)}"
        )
    if archive_probe_receipt:
        return _archive_probe_receipt(path)
    if recover_probe:
        return await _recover_probe(
            path, memory_id=probe_memory_id, revision=probe_revision, cleanup_store=cleanup_store
        )
    receipt_path = _probe_receipt_path(path)
    if write and receipt_path.exists():
        raise InputError(
            "An unresolved connection probe exists; its receipt was preserved. "
            f"Next command: {_probe_command(path)}"
        )
    event_id = _probe_identity(path)[0] if write else None
    async with connect(path) as session:
        result = await verify_session(
            session,
            write=write,
            project=project,
            cleanup_store=cleanup_store,
            probe_event_id=event_id,
            probe_receipt_path=receipt_path,
            fresh_session=lambda: connect(path),
        )
    if receipt_path.exists():
        result.update(probe_recovery="pending", next_command=_probe_command(path))
    return result


@asynccontextmanager
async def _stdio_session(entry: dict):
    async with stdio_client(
        StdioServerParameters(command=entry["command"], args=entry["args"])
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def verify_command(entry: dict, **checks) -> dict:
    credential_path = _entry_credential_path(entry)
    if credential_path is not None:
        if checks.get("write") and _probe_receipt_path(credential_path).exists():
            raise InputError(
                "An unresolved connection probe exists; its receipt was preserved. "
                f"Next command: {_probe_command(credential_path)}"
            )
        event_id, receipt_path = _probe_identity(credential_path)
        checks.setdefault("probe_event_id", event_id)
        checks.setdefault("probe_receipt_path", receipt_path)
    checks.setdefault("fresh_session", lambda: _stdio_session(entry))
    async with _stdio_session(entry) as session:
        return await verify_session(session, **checks)


def _matches_probe(record, *, memory_id, revision, event_id, project) -> bool:
    expected = {
        "id": memory_id,
        "revision": revision,
        "event_id": event_id,
        "project": project,
        "source": PROBE_SOURCE,
        "account": PROBE_ACCOUNT,
    }
    return (
        isinstance(record, dict)
        and isinstance(memory_id, str)
        and bool(memory_id)
        and type(revision) is int
        and revision >= 1
        and type(record.get("revision")) is int
        and all(record.get(key) == value for key, value in expected.items())
    )


def _archive_probe_receipt(path):
    receipt_path = _probe_receipt_path(path)
    command = _probe_command(path, "--archive-probe-receipt")
    try:
        fd = os.open(receipt_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise InputError(
            "No readable regular probe receipt is available; preserve any local evidence. "
            f"Next command: {_probe_command(path, '')}"
        ) from None
    temporary = None
    try:
        with os.fdopen(fd, "rb") as source:
            identity = os.fstat(source.fileno())
            if not stat.S_ISREG(identity.st_mode):
                raise InputError(f"Probe receipt must be a regular file. Next command: {command}")
            content = source.read()
        descriptor, temporary_name = tempfile.mkstemp(prefix=".probe-archive-", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # A fresh name plus publish_new never replaces earlier recovery evidence.
        for _ in range(3):
            archive = receipt_path.with_name(receipt_path.name + ".archived-" + uuid4().hex)
            try:
                publish_new(temporary, archive)
            except FileExistsError:
                continue
            break
        else:
            raise InputError(
                "Cannot allocate a new probe archive; all evidence was preserved. "
                f"Next command: {command}"
            )
        sync_directory(path.parent)
        current = receipt_path.lstat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            identity.st_dev,
            identity.st_ino,
            identity.st_size,
            identity.st_mtime_ns,
        ):
            raise InputError(
                "Probe receipt changed during archival; both files were preserved. "
                f"Next command: {command}"
            )
        receipt_path.unlink()
        sync_directory(path.parent)
        return {
            "state": "probe_receipt_archived",
            "archive": archive.name,
            "remaining_probe": "unknown",
            "probe_removed": "unknown",
            "next_command": _probe_command(path, "--write"),
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def _recover_probe(path, *, memory_id=None, revision=None, cleanup_store=None):
    receipt_path = _probe_receipt_path(path)
    archive_command = _probe_command(path, "--archive-probe-receipt")
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise InputError(
            "No regular probe receipt is available for recovery. "
            f"Next command: {_probe_command(path, '')}"
        )
    try:
        receipt = read_json(receipt_path)
    except InputError:
        raise InputError(
            f"Probe receipt is invalid; it was preserved. Next command: {archive_command}"
        ) from None
    if (
        type(receipt.get("version")) is not int
        or receipt["version"] != 1
        or receipt.get("state") not in {"sending", "written"}
        or not isinstance(receipt.get("project"), str)
        or receipt.get("source") != PROBE_SOURCE
        or receipt.get("account") != PROBE_ACCOUNT
    ):
        raise InputError(
            f"Probe receipt is invalid; it was preserved. Next command: {archive_command}"
        )
    event_id, _ = _probe_identity(path)
    if receipt["state"] == "written":
        if memory_id is not None and (memory_id, revision) != (
            receipt.get("memory_id"),
            receipt.get("revision"),
        ):
            raise InputError(
                "Recovery target differs from the saved probe; it was preserved. "
                f"Next command: {_probe_command(path)}"
            )
        memory_id, revision = receipt.get("memory_id"), receipt.get("revision")
    elif memory_id is None:
        return {
            "state": "probe_recovery_needed",
            "write": "unknown",
            "probe_removed": "unknown",
            "next_command": (
                _probe_command(path) + " --probe-memory-id <id> --probe-revision <revision>"
            ),
            "inspection": "On the memory host, inspect dots-brain audit events for this probe. "
            "If its outcome cannot be resolved, explicitly archive its receipt.",
            "archive_command": archive_command,
        }
    if not isinstance(memory_id, str) or not memory_id or type(revision) is not int or revision < 1:
        raise InputError(
            "Recovery needs an observed probe ID and positive integer revision. "
            f"Next command: {archive_command}"
        )
    removed = await _remove_probe_on_fresh_session(
        lambda: connect(path),
        memory_id=memory_id,
        revision=revision,
        event_id=event_id,
        project=receipt["project"],
        cleanup_store=cleanup_store,
    )
    if removed:
        _clear_probe_receipt(receipt_path)
    return {
        "state": "probe_recovered" if removed else "probe_recovery_needed",
        "probe_removed": True if removed else "pending_recovery",
        "next_command": _probe_command(path, "--write") if removed else archive_command,
    }


async def _remove_probe(session, *, memory_id, revision, event_id, project, cleanup_store) -> bool:
    if cleanup_store is not None:
        record = cleanup_store.get(memory_id)
    else:
        checked = await session.call_tool("memory_get", {"memory_id": memory_id})
        record = None if checked.isError else checked.structuredContent
    if not _matches_probe(
        record, memory_id=memory_id, revision=revision, event_id=event_id, project=project
    ):
        raise InputError("Probe identity mismatch; no memory was removed.")
    if cleanup_store is not None:
        cleanup_store.forget(memory_id, expected_revision=revision)
        return True
    removed = await session.call_tool(
        "memory_forget", {"memory_id": memory_id, "expected_revision": revision}
    )
    return bool(
        not removed.isError
        and removed.structuredContent
        and removed.structuredContent.get("deleted") is True
    )


async def _remove_probe_on_fresh_session(
    fresh_session, *, memory_id, revision, event_id, project, cleanup_store
) -> bool:
    try:
        if cleanup_store is not None:
            return await _remove_probe(
                None,
                memory_id=memory_id,
                revision=revision,
                event_id=event_id,
                project=project,
                cleanup_store=cleanup_store,
            )
        if fresh_session is None:
            return False
        async with fresh_session() as session:
            return await _remove_probe(
                session,
                memory_id=memory_id,
                revision=revision,
                event_id=event_id,
                project=project,
                cleanup_store=None,
            )
    except Exception:
        return False


def _verification_result(*, state, names, read, write, probe_removed):
    return {
        "state": state,
        "tools": names,
        "read": read,
        "write": write,
        "probe_removed": probe_removed,
        "capture": "not_verified_by_connection_check",
    }


async def verify_session(
    session,
    *,
    write=False,
    project="default",
    cleanup_store=None,
    probe_event_id=None,
    probe_receipt_path=None,
    fresh_session=None,
) -> dict:
    tools = await session.list_tools()
    names = [tool.name for tool in tools.tools]
    check = "memory_status" if "memory_status" in names else "audit_report"
    if check not in names:
        if write or not names:
            raise InputError("This client has no supported read tool for verification.")
        return _verification_result(
            state="verified_connection",
            names=names,
            read="not_available",
            write="not_tested",
            probe_removed="not_applicable",
        )
    result = await session.call_tool(check, {})
    if result.isError:
        return _verification_result(
            state="verification_failed",
            names=names,
            read=False,
            write="not_tested" if not write else False,
            probe_removed="not_applicable" if not write else False,
        )
    if not write:
        return _verification_result(
            state="verified_read",
            names=names,
            read=True,
            write="not_tested",
            probe_removed="not_applicable",
        )
    if "memory_forget" not in names and cleanup_store is None:
        raise InputError("Write verification needs a way to remove its synthetic probe.")
    if "memory_remember" not in names or "memory_get" not in names:
        raise InputError("This client cannot write and read a connection probe.")
    event_id = probe_event_id or "connection-probe-" + str(uuid4())
    _pending_probe(probe_receipt_path, event_id=event_id, project=project)
    receipt = {
        "version": 1,
        "event_id": event_id,
        "project": project,
        "source": PROBE_SOURCE,
        "account": PROBE_ACCOUNT,
        "state": "sending",
    }
    _write_probe_receipt(probe_receipt_path, receipt)
    try:
        saved = await session.call_tool(
            "memory_remember",
            {
                "content": "Synthetic Dots Brain connection probe.",
                "source": PROBE_SOURCE,
                "account": PROBE_ACCOUNT,
                "event_id": event_id,
                "project": project,
            },
        )
    except Exception:
        # The write may have reached the service. Keep the receipt for explicit
        # identity-checked recovery; never replay the write automatically.
        return _verification_result(
            state="verification_failed",
            names=names,
            read=True,
            write="unknown",
            probe_removed="unknown",
        )
    if saved.isError or not saved.structuredContent:
        # A server error can occur after a commit; retain the idempotency receipt.
        return _verification_result(
            state="verification_failed",
            names=names,
            read=True,
            write="unknown",
            probe_removed="unknown",
        )
    try:
        memory_id = saved.structuredContent["id"]
        revision = saved.structuredContent["revision"]
    except (KeyError, TypeError):
        return _verification_result(
            state="verification_failed",
            names=names,
            read=True,
            write="unknown",
            probe_removed="unknown",
        )
    receipt.update({"state": "written", "memory_id": memory_id, "revision": revision})
    _write_probe_receipt(probe_receipt_path, receipt)
    try:
        checked = await session.call_tool("memory_get", {"memory_id": memory_id})
        verified_write = bool(
            not checked.isError
            and _matches_probe(
                checked.structuredContent,
                memory_id=memory_id,
                revision=revision,
                event_id=event_id,
                project=project,
            )
        )
    except Exception:
        verified_write = False
    if verified_write:
        try:
            removed = await _remove_probe(
                session,
                memory_id=memory_id,
                revision=revision,
                event_id=event_id,
                project=project,
                cleanup_store=cleanup_store,
            )
        except Exception:
            removed = False
    else:
        # Never trust a broken session to clean up a write it may have accepted.
        removed = await _remove_probe_on_fresh_session(
            fresh_session,
            memory_id=memory_id,
            revision=revision,
            event_id=event_id,
            project=project,
            cleanup_store=cleanup_store,
        )
    if removed:
        _clear_probe_receipt(probe_receipt_path)
    return _verification_result(
        state="verified_read_write" if verified_write and removed else "verification_failed",
        names=names,
        read=True,
        write=verified_write,
        probe_removed=removed if removed else "pending_recovery",
    )


class BridgeFailure(BrainError):
    def __init__(self, error):
        super().__init__(error["message"])
        self.code = error["code"]
        self.reference = error.get("reference")


@dataclass
class _BridgeRequest:
    name: str | None
    arguments: dict | None
    result: asyncio.Future
    reference: str
    deadline: float
    sent: bool = False

    def failure(self, fallback):
        if self.sent and self.name in WRITE_TOOLS:
            return {
                "code": "outcome_unknown",
                "message": (
                    "The write was sent but its outcome is unknown. Do not replay it. "
                    "On the memory host, inspect dots-brain audit events and the target revision; "
                    "use this reference when checking the bridge log."
                ),
                "reference": self.reference,
            }
        return fallback


class BridgeSession:
    """One owner for the HTTP context, with bounded concurrent tool calls.

    Every request has one deadline including queue time. A failed transport ends
    the session and fails its outstanding requests; no operation is replayed.
    """

    MAX_PENDING = 128
    MAX_CONCURRENT = 8
    REQUEST_TIMEOUT = 30

    def __init__(self, path):
        self.path = path
        self.queue = asyncio.Queue(maxsize=self.MAX_PENDING)
        self.pending = set()
        self.requests = {}
        self.closed = False
        self.owner = None

    async def request(self, name, arguments=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.REQUEST_TIMEOUT
        _check_request_size(name, arguments)
        if self.closed:
            raise BridgeFailure(
                {"code": "service_unavailable", "message": "The local bridge is closed."}
            )
        if len(self.pending) >= self.MAX_PENDING:
            raise BusyError("The local bridge capacity is full; retry later.")
        if loop.time() >= deadline:
            raise BridgeFailure(safe_error(TimeoutError()))
        result = loop.create_future()
        request = _BridgeRequest(name, arguments, result, uuid4().hex, deadline)
        self.pending.add(result)
        self.requests[result] = request
        try:
            self.queue.put_nowait(request)
        except asyncio.QueueFull:
            self.pending.discard(result)
            self.requests.pop(result, None)
            result.cancel()
            raise BusyError("The local bridge capacity is full; retry later.") from None
        try:
            return await asyncio.wait_for(result, timeout=max(0, deadline - loop.time()))
        except TimeoutError as exc:
            raise BridgeFailure(request.failure(safe_error(exc))) from None
        finally:
            self.pending.discard(result)
            self.requests.pop(result, None)

    def _settle_pending(self, error=None):
        while not self.queue.empty():
            self.queue.get_nowait()
        for result in tuple(self.pending):
            if not result.done():
                if error is None:
                    result.cancel()
                else:
                    request = self.requests[result]
                    result.set_exception(BridgeFailure(request.failure(error)))

    async def close(self):
        self.closed = True
        self._settle_pending()
        if self.owner is not None and not self.owner.done():
            self.owner.cancel()

    async def _call(self, session, slots, failed, request):
        name, arguments, result = request.name, request.arguments, request.result
        task = asyncio.current_task()

        def cancel_abandoned(future):
            if future.cancelled():
                task.cancel()

        result.add_done_callback(cancel_abandoned)
        try:
            async with slots:
                if result.done() or failed.is_set():
                    return
                _check_request_size(name, arguments, request_id=getattr(session, "_request_id", 0))
                if asyncio.get_running_loop().time() >= request.deadline:
                    if not result.done():
                        result.set_exception(BridgeFailure(safe_error(TimeoutError())))
                    return
                request.sent = True
                value = (
                    await session.list_tools()
                    if name is None
                    else await session.call_tool(name, arguments)
                )
                if not result.done():
                    result.set_result(value)
        except Exception as exc:
            error = safe_error(exc)
            if error["code"] in {"request_too_large", "invalid_input"}:
                if not result.done():
                    result.set_exception(BridgeFailure(error))
            else:
                failed.set()
                raise
        finally:
            result.remove_done_callback(cancel_abandoned)

    async def run(self):
        self.owner = asyncio.current_task()
        slots = asyncio.Semaphore(self.MAX_CONCURRENT)
        try:
            while not self.closed:
                request = await self.queue.get()
                if request.result.done():
                    continue
                try:
                    async with connect(self.path) as session, asyncio.TaskGroup() as calls:
                        failed = asyncio.Event()
                        while not self.closed:
                            if not request.result.done():
                                calls.create_task(self._call(session, slots, failed, request))
                            request = await self.queue.get()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = safe_error(exc, operation="bridge/session")
                    if error["code"] in {"internal_error", "request_too_large", "timed_out"}:
                        error = {
                            "code": "service_unavailable",
                            "message": (
                                "The shared memory session failed; check the service and reconnect."
                            ),
                        }
                    self._settle_pending(error)
        except asyncio.CancelledError:
            if not self.closed:
                raise
        finally:
            self.closed = True
            self._settle_pending()


async def run_bridge(path: Path) -> None:
    server = Server("Dots Brain bridge", version=__version__, instructions=INSTRUCTIONS)
    remote = BridgeSession(path)

    @server.list_tools()
    async def list_tools():
        return (await remote.request(None)).tools

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            return await remote.request(name, arguments)
        except Exception as exc:
            return error_result(exc)

    protect_tool_dispatch(server)
    worker = asyncio.create_task(remote.run())
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await remote.close()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
