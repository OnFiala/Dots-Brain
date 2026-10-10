"""Private, atomic installation state on a POSIX memory host."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

from .errors import BusyError, InputError


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = 40, shared: bool = False, create_parent: bool = False):
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BusyError(
                        "Another process holds the installation lock; stop writers and retry."
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def atomic_write(path: Path, content: str, *, mode: int = 0o600, preserve: bool = False) -> None:
    if path.is_symlink():
        raise InputError("Refusing to replace a symbolic link.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".brain-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if preserve and path.exists():
                previous = path.stat()
                os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
                mode = previous.st_mode & 0o777
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
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


def sync_file_and_parent(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path: Path) -> dict:
    if path.is_symlink():
        raise InputError("Installation state must not be a symbolic link.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise InputError("Configuration is not a valid UTF-8 JSON object.") from None
    if not isinstance(value, dict):
        raise InputError("Expected an object in the configuration file.")
    return value
