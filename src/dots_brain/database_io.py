"""Private, standalone SQLite snapshots with explicit connection ownership."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .database_checks import validate_snapshot
from .errors import InputError
from .local import publish_new, sync_file_and_parent


def open_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """Use immutable only for a closed snapshot, never for a live WAL database."""
    options = "?mode=ro" + ("&immutable=1" if immutable else "")
    return sqlite3.connect(path.resolve().as_uri() + options, uri=True)


def snapshot(source: sqlite3.Connection, output: Path, version: int) -> Path:
    """Publish a validated snapshot atomically without replacing an existing file."""
    output = output.expanduser().absolute()
    if not output.parent.is_dir():
        raise InputError("Create a private backup directory first.")
    if output.exists() or output.is_symlink():
        raise InputError("Backup already exists; choose a new output path.")
    fd, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".partial", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with closing(sqlite3.connect(temporary)) as target:
            source.backup(target)
            validate_snapshot(target, version)
            target.execute("PRAGMA journal_mode=DELETE")
        sync_file_and_parent(temporary)
        try:
            publish_new(temporary, output)
        except FileExistsError:
            raise InputError("Backup already exists; choose a new output path.") from None
        sync_file_and_parent(output)
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
    return output
