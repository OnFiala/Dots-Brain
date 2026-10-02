"""Private, atomic installation state on a POSIX memory host."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

from .errors import InputError


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = 40):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise InputError(
                        "Another installation is still running; retry this command."
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise InputError("Refusing to replace a symbolic link.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".brain-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
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


def read_json(path: Path) -> dict:
    if path.is_symlink():
        raise InputError("Installation state must not be a symbolic link.")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise InputError("Expected an object in the configuration file.")
    return value
