"""A local stdio client for the same authenticated remote memory service."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import __version__
from .auth import authenticate, read_connection
from .errors import BrainError, InputError
from .local import read_json, write_json
from .protocol import INSTRUCTIONS, error_result, safe_error

PROBE_SOURCE = "dots-brain-probe"
PROBE_ACCOUNT = "connection-verifier"


def _probe_receipt_path(path: Path) -> Path:
    """Keep a recoverable probe receipt beside its private credential."""
    return path.with_name(path.name + ".probe-recovery.json")


def _probe_identity(path: Path) -> tuple[str, Path]:
    """Derive a stable probe identity without retaining the credential token."""
    token = read_connection(path)["token"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    return "connection-probe-" + digest[:32], _probe_receipt_path(path)


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
        raise InputError("A different unresolved connection probe exists; it was preserved.")


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
    path: Path, *, write: bool = False, project: str = "default", cleanup_store=None
) -> dict:
    event_id, receipt_path = _probe_identity(path)
    async with connect(path) as session:
        return await verify_session(
            session,
            write=write,
            project=project,
            cleanup_store=cleanup_store,
            probe_event_id=event_id,
            probe_receipt_path=receipt_path,
            fresh_session=lambda: connect(path),
        )


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
        event_id, receipt_path = _probe_identity(credential_path)
        checks.setdefault("probe_event_id", event_id)
        checks.setdefault("probe_receipt_path", receipt_path)
    checks.setdefault("fresh_session", lambda: _stdio_session(entry))
    async with _stdio_session(entry) as session:
        return await verify_session(session, **checks)


async def _remove_probe(session, *, memory_id, revision, event_id, cleanup_store) -> bool:
    if cleanup_store is not None:
        record = cleanup_store.get(memory_id)
        if record["event_id"] != event_id or record["source"] != PROBE_SOURCE:
            raise InputError("Probe identity mismatch; no memory was removed.")
        cleanup_store.forget(memory_id, expected_revision=revision)
        return True
    removed = await session.call_tool(
        "memory_forget", {"memory_id": memory_id, "expected_revision": revision}
    )
    return not removed.isError


async def _remove_probe_on_fresh_session(
    fresh_session, *, memory_id, revision, event_id, cleanup_store
) -> bool:
    try:
        if cleanup_store is not None:
            return await _remove_probe(
                None,
                memory_id=memory_id,
                revision=revision,
                event_id=event_id,
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
        raise InputError("This client has no supported read tool for verification.")
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
        # The write may have reached the service. Keep the receipt and let an
        # explicit later --write rerun use the same idempotency identity.
        return _verification_result(
            state="verification_failed",
            names=names,
            read=True,
            write="unknown",
            probe_removed="unknown",
        )
    if saved.isError or not saved.structuredContent:
        _clear_probe_receipt(probe_receipt_path)
        return _verification_result(
            state="verification_failed",
            names=names,
            read=True,
            write=False,
            probe_removed=False,
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
            and checked.structuredContent
            and checked.structuredContent.get("event_id") == event_id
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


class BridgeSession:
    """Own the HTTP context in one task and reuse it until a transport failure.

    A failed operation is never replayed. The next request opens a new session.
    Keeping context entry and exit in the same task preserves AnyIO ownership.
    """

    def __init__(self, path):
        self.path = path
        self.queue = asyncio.Queue(maxsize=32)
        self.closed = False

    async def request(self, name, arguments=None):
        if self.closed:
            raise BridgeFailure(
                {"code": "service_unavailable", "message": "The local bridge is closed."}
            )
        result = asyncio.get_running_loop().create_future()
        try:
            await self.queue.put((name, arguments, result))
            return await result
        except asyncio.CancelledError:
            # A canceled caller must not leave a result waiter behind in the
            # bounded queue. The worker observes this before issuing the call.
            result.cancel()
            raise

    def _cancel_pending(self):
        while True:
            try:
                request = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if request is not None and not request[2].done():
                request[2].cancel()

    async def close(self):
        self.closed = True
        self._cancel_pending()
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            # The worker will receive its cancellation path during bridge
            # shutdown, which drains the queue and resolves all callers.
            pass

    async def run(self):
        request = None
        try:
            while True:
                request = await self.queue.get()
                if request is None:
                    return
                try:
                    async with connect(self.path) as session:
                        while request is not None:
                            name, arguments, result = request
                            if not result.cancelled():
                                value = (
                                    await session.list_tools()
                                    if name is None
                                    else await session.call_tool(name, arguments)
                                )
                                if not result.done():
                                    result.set_result(value)
                            request = await self.queue.get()
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if request is not None and not request[2].done():
                        request[2].set_exception(BridgeFailure(safe_error(exc)))
        finally:
            self.closed = True
            if request is not None and not request[2].done():
                request[2].cancel()
            self._cancel_pending()


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
