"""Private, atomic installation state on a POSIX memory host."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path

from .errors import InputError

OUTPUT_CLEANUP_WARNING = (
    "Output cleanup incomplete. The requested private output file may remain; "
    "check it before retrying."
)


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = 40, shared: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise InputError(
                        "The installation lock is busy. Stop running writers before "
                        "offline maintenance, then retry."
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


_writer_leases: ContextVar[tuple] = ContextVar("dots_brain_writer_leases", default=())


@contextlib.contextmanager
def writer_lock(directory: Path, *, exclusive: bool = False):
    """Reenter an operation's lease; never silently upgrade a shared writer lease.

    Server entrypoints hold SH for their lifetime. Maintenance holds EX across
    its database and file changes. Nested Store connections reuse that lease.
    """
    directory = directory.resolve()
    held = dict(_writer_leases.get())
    if directory in held:
        if exclusive and not held[directory]:
            raise InputError("Offline maintenance cannot run inside an active writer.")
        yield
        return
    with locked(directory / "writers.lock", shared=not exclusive, timeout=0):
        token = _writer_leases.set((*held.items(), (directory, exclusive)))
        try:
            yield
        finally:
            _writer_leases.reset(token)


def atomic_write(
    path: Path, content: str, *, mode: int = 0o600, preserve=False, expected=None
) -> None:
    if path.is_symlink():
        raise InputError("Refusing to replace a symbolic link.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".brain-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if preserve and path.exists():
                info = path.stat()
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                mode = info.st_mode & 0o7777
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None and (
            path.is_symlink()
            or (path.exists(), path.read_bytes() if path.exists() else b"") != expected
        ):
            raise InputError(
                "Configuration changed concurrently; close the client/editor and retry."
            )
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, indent=2) + "\n")


@contextlib.contextmanager
def exclusive_output(path: Path):
    """Create private output and retain its inode through success or cleanup.

    Preserve replacements and report incomplete cleanup on the original error.
    This assumes a cooperative directory; the stat/unlink pair is not atomic
    against hostile writers.
    Callers must keep the descriptor open, using fdopen with closefd=False.
    """
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        created = os.fstat(descriptor)
        try:
            yield descriptor
        except BaseException as exc:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                exc.add_note(
                    f"{OUTPUT_CLEANUP_WARNING} Path: {path}. "
                    f"Cleanup error: {type(cleanup_error).__name__}."
                )
            raise
    finally:
        os.close(descriptor)


def sync_file_and_parent(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path: Path) -> dict:
    if path.is_symlink():
        raise InputError("Installation state must not be a symbolic link.")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise InputError("Expected an object in the configuration file.")
    return value
