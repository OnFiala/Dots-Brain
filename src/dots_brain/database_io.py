"""Closed, standalone snapshots and URI-safe read-only database access."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .database_checks import validate_snapshot
from .errors import InputError
from .local import sync_file_and_parent


def open_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    # Immutable is only for a closed standalone snapshot, never a live WAL store.
    wal = Path(str(path) + "-wal")
    if immutable and wal.exists() and wal.stat().st_size:
        raise InputError("Backup has a WAL sidecar; create a standalone verified backup first.")
    options = "?mode=ro" + ("&immutable=1" if immutable else "")
    return sqlite3.connect(path.resolve().as_uri() + options, uri=True)


def snapshot(source: sqlite3.Connection, output: Path, version: int) -> Path:
    """Create and close a private snapshot without overwriting an existing file."""
    output = output.expanduser().absolute()
    if not output.parent.is_dir():
        raise InputError("Create a private backup directory first.")
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(output)) as target:
            source.backup(target)
            validate_snapshot(target, version)
            target.execute("PRAGMA journal_mode=DELETE")
        sync_file_and_parent(output)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output
