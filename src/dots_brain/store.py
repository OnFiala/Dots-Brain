"""Transactional source records and derived full-text indexes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import ConflictError, InputError, NotFoundError, SuppressedError

SCHEMA_VERSION = 2
WAL_LOCK_TIMEOUT = 10.0
SCHEMA = """
CREATE TABLE memories (
    id TEXT PRIMARY KEY, identity_key TEXT NOT NULL,
    source TEXT NOT NULL, account TEXT NOT NULL, event_id TEXT NOT NULL,
    project TEXT NOT NULL, title TEXT NOT NULL, current_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project, identity_key)
);
CREATE TABLE revisions (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL,
    source_uri TEXT, title TEXT NOT NULL, writer_principal TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, revision)
);
CREATE VIRTUAL TABLE memory_fts USING fts5(
    memory_id UNINDEXED, title, content, tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE suppressions (source_key TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
CREATE TABLE scoped_suppressions (
    project TEXT NOT NULL, identity_key TEXT NOT NULL,
    deleted_revision INTEGER NOT NULL, deleted_by TEXT NOT NULL, deleted_at TEXT NOT NULL,
    PRIMARY KEY(project, identity_key)
);
CREATE TABLE vectors (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, model TEXT NOT NULL,
    dimension INTEGER NOT NULL, vector BLOB NOT NULL,
    PRIMARY KEY(memory_id, model)
);
CREATE TABLE clients (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
    scopes TEXT NOT NULL, projects TEXT, expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
PRAGMA user_version=2;
"""


def extension_statements() -> Iterator[str]:
    """Load feature schemas only at setup/migration, avoiding import-time cycles."""
    from .activity import SCHEMA_SQL as audit_sql
    from .cortex_connector import SCHEMA_SQL as cortex_sql
    from .semantic import SCHEMA_SQL as semantic_sql

    yield from audit_sql
    yield from (statement for statement in cortex_sql.split(";") if statement.strip())
    yield from semantic_sql


def schema_statements() -> Iterator[str]:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            yield statement
    yield from extension_statements()


def now() -> str:
    return datetime.now(UTC).isoformat()


def validate_text(value: str, name: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise InputError(f"{name} must be text of at most {maximum} characters without NUL.")
    if not empty and not value.strip():
        raise InputError(f"{name} must not be empty.")
    return value


def source_key(source: str, account: str, event_id: str) -> str:
    return hashlib.sha256(json.dumps([source, account, event_id]).encode()).hexdigest()


def enable_wal(db: sqlite3.Connection) -> None:
    # Switching journal modes can return SQLITE_BUSY without invoking SQLite's
    # busy handler. Retry only that idempotent operation with a bounded deadline.
    deadline = time.monotonic() + WAL_LOCK_TIMEOUT
    previous_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    db.execute("PRAGMA busy_timeout=0")
    try:
        while True:
            try:
                mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                remaining = deadline - time.monotonic()
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0:
                    raise
                time.sleep(min(0.05, remaining))
            else:
                if mode.lower() != "wal":
                    raise InputError("This database does not support WAL journal mode.")
                return
    finally:
        db.execute(f"PRAGMA busy_timeout={int(previous_timeout)}")


class Store:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser().resolve()
        self.path = self.directory / "brain.sqlite3"

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise InputError("The database must not be a symbolic link.")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                raise InputError(
                    "Database schema v1 requires an explicit offline migration; "
                    "do not start this release."
                )
            if version not in (0, SCHEMA_VERSION):
                raise InputError("Unsupported database version; use a compatible Brain release.")
            enable_wal(db)
            db.execute("BEGIN IMMEDIATE")
            # Another initializer may have created the schema while this connection
            # waited for the write lock.
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                # The schema and version are committed together; failure leaves no partial schema.
                for statement in schema_statements():
                    db.execute(statement)

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            raise InputError("Memory is not initialized. Run dots-brain setup first.")
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] == 1:
                raise InputError(
                    "Database schema v1 requires an explicit offline migration; "
                    "do not access it through this release."
                )
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, SCHEMA_VERSION):
                raise InputError("Unsupported database version; use a compatible Brain release.")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA synchronous=FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _filter(projects: tuple[str, ...] | None, project: str | None = None):
        if project is not None:
            validate_text(project, "project", 200)
            if projects is not None and project not in projects:
                return " AND 0", []
            return " AND m.project=?", [project]
        if projects is None:
            return "", []
        if not projects:
            return " AND 0", []
        return f" AND m.project IN ({','.join('?' for _ in projects)})", list(projects)

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        record = dict(row)
        record.pop("identity_key", None)
        record.pop("digest", None)
        return record

    def remember(
        self,
        *,
        content: str,
        source: str,
        account: str,
        event_id: str,
        project: str = "default",
        title: str = "",
        source_uri: str | None = None,
        expected_revision: int | None = None,
        projects: tuple[str, ...] | None = None,
        writer_principal: str = "local-owner:stdio",
    ) -> dict:
        for field, value, limit in (
            ("content", content, 32000),
            ("source", source, 200),
            ("account", account, 200),
            ("event_id", event_id, 500),
            ("project", project, 200),
            ("title", title, 300),
        ):
            validate_text(value, field, limit, empty=field == "title")
        if source_uri is not None:
            validate_text(source_uri, "source_uri", 2000)
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise InputError("expected_revision must be a positive integer.")
        if projects is not None and project not in projects:
            raise NotFoundError("Project is not available to this client.")
        validate_text(writer_principal, "writer_principal", 500)
        from .privacy import guard_content

        guard_content(
            {
                "content": content,
                "source": source,
                "account": account,
                "event_id": event_id,
                "project": project,
                "title": title,
                "source_uri": source_uri,
            }
        )
        key = source_key(source, account, event_id)
        digest = hashlib.sha256(
            json.dumps([content, title, source_uri], ensure_ascii=False).encode()
        ).hexdigest()
        timestamp = now()
        with self.connection(write=True) as db:
            self.ensure_writable()
            if db.execute("SELECT 1 FROM suppressions WHERE source_key=?", (key,)).fetchone():
                raise SuppressedError("This source was forgotten and is blocked from reimport.")
            if db.execute(
                "SELECT 1 FROM scoped_suppressions WHERE project=? AND identity_key=?",
                (project, key),
            ).fetchone():
                raise SuppressedError("This source was forgotten and is blocked from reimport.")
            existing = db.execute(
                "SELECT * FROM memories WHERE project=? AND identity_key=?", (project, key)
            ).fetchone()
            if existing:
                memory_id = existing["id"]
                replay = db.execute(
                    "SELECT revision FROM revisions WHERE memory_id=? AND digest=?",
                    (memory_id, digest),
                ).fetchone()
                if replay and (
                    expected_revision != existing["current_revision"]
                    or replay["revision"] == existing["current_revision"]
                ):
                    return {
                        "id": memory_id,
                        "revision": existing["current_revision"],
                        "replayed_revision": replay["revision"],
                        "changed": False,
                    }
                if expected_revision != existing["current_revision"]:
                    raise ConflictError("Content changed; supply the current expected_revision.")
                revision = existing["current_revision"] + 1
                db.execute(
                    "UPDATE memories SET current_revision=?,title=?,updated_at=? WHERE id=?",
                    (revision, title, timestamp, memory_id),
                )
            else:
                if expected_revision is not None:
                    raise ConflictError("The source does not have an existing revision.")
                memory_id, revision = str(uuid.uuid4()), 1
                db.execute(
                    "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        memory_id,
                        key,
                        source,
                        account,
                        event_id,
                        project,
                        title,
                        revision,
                        timestamp,
                        timestamp,
                    ),
                )
            db.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?)",
                (
                    memory_id,
                    revision,
                    content,
                    digest,
                    source_uri,
                    title,
                    writer_principal,
                    timestamp,
                ),
            )
            if existing:
                db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            db.execute("INSERT INTO memory_fts VALUES (?,?,?)", (memory_id, title, content))
        return {"id": memory_id, "revision": revision, "changed": True}

    def get(
        self,
        memory_id: str,
        *,
        revision: int | None = None,
        projects: tuple[str, ...] | None = None,
    ) -> dict:
        clause, args = self._filter(projects)
        with self.connection() as db:
            row = db.execute(
                "SELECT m.id,m.source,m.account,m.event_id,m.project,m.created_at,m.updated_at,"
                "r.revision,r.title,r.content,r.source_uri,r.writer_principal "
                "FROM memories m JOIN revisions r "
                "ON r.memory_id=m.id AND r.revision=COALESCE(?,m.current_revision) "
                "WHERE m.id=?" + clause,
                [revision, memory_id, *args],
            ).fetchone()
        if row is None:
            raise NotFoundError("Memory is not available.")
        return self._record(row)

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        limit: int = 10,
        projects: tuple[str, ...] | None = None,
    ) -> list[dict]:
        validate_text(query, "query", 2000)
        if not 1 <= limit <= 50:
            raise InputError("limit must be between 1 and 50.")
        terms = re.findall(r"[^\W_]+", query, re.UNICODE)[:32]
        if not terms:
            return []
        expression = " OR ".join('"' + term + '"' for term in terms)
        clause, args = self._filter(projects, project)
        with self.connection() as db:
            rows = db.execute(
                "SELECT m.id,m.title,m.project,m.source,m.account,m.event_id,"
                "m.current_revision AS revision,"
                "r.source_uri,snippet(memory_fts,2,'','',' … ',48) AS excerpt,"
                "bm25(memory_fts) AS rank "
                "FROM memory_fts JOIN memories m ON m.id=memory_fts.memory_id "
                "JOIN revisions r ON r.memory_id=m.id AND r.revision=m.current_revision "
                "WHERE memory_fts MATCH ?" + clause + " ORDER BY rank,m.id LIMIT ?",
                [expression, *args, limit],
            ).fetchall()
        return [self._record(row) for row in rows]

    def forget(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        projects: tuple[str, ...] | None = None,
        writer_principal: str = "local-owner:stdio",
    ) -> dict:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise InputError("expected_revision must be a positive integer.")
        clause, args = self._filter(projects)
        validate_text(writer_principal, "writer_principal", 500)
        with self.connection(write=True) as db:
            self.ensure_writable()
            row = db.execute(
                "SELECT m.project,m.identity_key,m.current_revision FROM memories m "
                "WHERE m.id=?" + clause,
                [memory_id, *args],
            ).fetchone()
            if row is None:
                return {"deleted": False}
            if row["current_revision"] != expected_revision:
                raise ConflictError("Content changed; supply the current expected_revision.")
            db.execute(
                "INSERT OR IGNORE INTO scoped_suppressions VALUES (?,?,?,?,?)",
                (row["project"], row["identity_key"], expected_revision, writer_principal, now()),
            )
            db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return {"deleted": True, "reimport_suppressed": True}

    def ensure_writable(self) -> None:
        # Check after acquiring the SQLite writer lock: a recovery cutover may
        # have disabled this store while this request was waiting for that lock.
        if (self.directory / "disabled.json").exists():
            raise InputError("This installation is disabled; memory writes are unavailable.")

    def status(self, *, projects: tuple[str, ...] | None = None) -> dict:
        clause, args = self._filter(projects)
        with self.connection() as db:
            counts = db.execute(
                "SELECT COUNT(*) AS memories,COALESCE(SUM(m.current_revision),0) AS revisions "
                "FROM memories m WHERE 1" + clause,
                args,
            ).fetchone()
            sources = db.execute(
                "SELECT m.source,m.account,m.project,COUNT(*) AS memories,"
                "MAX(m.updated_at) AS last_write FROM memories m WHERE 1"
                + clause
                + " GROUP BY m.source,m.account,m.project ORDER BY m.source,m.account,m.project",
                args,
            ).fetchall()
        return {
            **dict(counts),
            "sources": [dict(r) for r in sources],
            "capture": "explicit_writes_and_opt_in_snapshots",
            "continuous_capture": "unverified",
            "history_import": "bounded_user_text_snapshots",
        }

    def export(self, *, projects: tuple[str, ...] | None = None) -> Iterator[dict]:
        clause, args = self._filter(projects)
        with self.connection() as db:
            rows = db.execute(
                "SELECT m.id,m.source,m.account,m.event_id,m.project,m.created_at,m.updated_at,"
                "r.revision,r.content,r.title,r.source_uri,r.writer_principal "
                "FROM memories m JOIN revisions r "
                "ON r.memory_id=m.id WHERE 1" + clause + " ORDER BY m.id,r.revision",
                args,
            )
            for row in rows:
                yield self._record(row)
