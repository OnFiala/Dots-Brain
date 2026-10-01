"""Transactional source records and derived full-text indexes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import ConflictError, InputError, NotFoundError, SuppressedError

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE memories (
    id TEXT PRIMARY KEY, source_key TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL, account TEXT NOT NULL, event_id TEXT NOT NULL,
    project TEXT NOT NULL, title TEXT NOT NULL, revision INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE revisions (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL,
    source_uri TEXT, title TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, revision)
);
CREATE VIRTUAL TABLE memory_fts USING fts5(
    memory_id UNINDEXED, title, content, tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE suppressions (source_key TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
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
PRAGMA user_version=1;
"""


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
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise InputError("Unsupported database version; use a compatible Brain release.")
            if version == 0:
                # The schema and version are committed together; failure leaves no partial schema.
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            raise InputError("Memory is not initialized. Run dots-brain setup first.")
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
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
        record.pop("source_key", None)
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
        key = source_key(source, account, event_id)
        digest = hashlib.sha256(
            json.dumps([content, title, source_uri], ensure_ascii=False).encode()
        ).hexdigest()
        timestamp = now()
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM suppressions WHERE source_key=?", (key,)).fetchone():
                raise SuppressedError("This source was forgotten and is blocked from reimport.")
            existing = db.execute("SELECT * FROM memories WHERE source_key=?", (key,)).fetchone()
            if existing:
                if existing["project"] != project:
                    raise ConflictError("A source record cannot move between projects.")
                memory_id = existing["id"]
                replay = db.execute(
                    "SELECT revision FROM revisions WHERE memory_id=? AND digest=?",
                    (memory_id, digest),
                ).fetchone()
                if replay and (
                    expected_revision != existing["revision"]
                    or replay["revision"] == existing["revision"]
                ):
                    return {
                        "id": memory_id,
                        "revision": existing["revision"],
                        "replayed_revision": replay["revision"],
                        "changed": False,
                    }
                if expected_revision != existing["revision"]:
                    raise ConflictError("Content changed; supply the current expected_revision.")
                revision = existing["revision"] + 1
                db.execute(
                    "UPDATE memories SET revision=?,title=?,updated_at=? WHERE id=?",
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
                "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                (memory_id, revision, content, digest, source_uri, title, timestamp),
            )
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
                "r.revision,r.title,r.content,r.source_uri FROM memories m JOIN revisions r "
                "ON r.memory_id=m.id AND r.revision=COALESCE(?,m.revision) "
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
                "SELECT m.id,m.title,m.project,m.source,m.account,m.event_id,m.revision,"
                "r.source_uri,substr(r.content,1,800) AS excerpt,bm25(memory_fts) AS rank "
                "FROM memory_fts JOIN memories m ON m.id=memory_fts.memory_id "
                "JOIN revisions r ON r.memory_id=m.id AND r.revision=m.revision "
                "WHERE memory_fts MATCH ?" + clause + " ORDER BY rank,m.id LIMIT ?",
                [expression, *args, limit],
            ).fetchall()
        return [self._record(row) for row in rows]

    def forget(self, memory_id: str, *, projects: tuple[str, ...] | None = None) -> dict:
        clause, args = self._filter(projects)
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT m.source_key FROM memories m WHERE m.id=?" + clause,
                [memory_id, *args],
            ).fetchone()
            if row is None:
                return {"deleted": False}
            db.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)", (row[0], now()))
            db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return {"deleted": True, "reimport_suppressed": True}

    def status(self, *, projects: tuple[str, ...] | None = None) -> dict:
        clause, args = self._filter(projects)
        with self.connection() as db:
            counts = db.execute(
                "SELECT COUNT(*) AS memories,COALESCE(SUM(m.revision),0) AS revisions "
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
            "capture": "explicit_writes_only",
            "history_import": "not_implemented",
        }

    def export(self, *, projects: tuple[str, ...] | None = None) -> Iterator[dict]:
        clause, args = self._filter(projects)
        with self.connection() as db:
            rows = db.execute(
                "SELECT m.id,m.source,m.account,m.event_id,m.project,m.created_at,m.updated_at,"
                "r.revision,r.content,r.title,r.source_uri FROM memories m JOIN revisions r "
                "ON r.memory_id=m.id WHERE 1" + clause + " ORDER BY m.id,r.revision",
                args,
            )
            for row in rows:
                yield self._record(row)
