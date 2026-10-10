"""Transactional source records and derived full-text indexes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import (
    CapabilityError,
    ConflictError,
    InputError,
    IntegrityError,
    MigrationRequiredError,
    NotFoundError,
    StoreDisabledError,
    SuppressedError,
)
from .installation_state import marker_path
from .local import locked, publish_new, sync_directory, sync_file_and_parent
from .schema import APPLICATION_ID, SCHEMA_VERSION, create_schema, validate_schema

MIN_SQLITE_VERSION = (3, 42, 0)
WAL_LOCK_TIMEOUT = 10.0


def now() -> str:
    return datetime.now(UTC).isoformat()


def validate_text(value: str, name: str, maximum: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or "\x00" in value
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
        or any(
            unicodedata.category(character) == "Cc" and character not in "\n\r\t"
            for character in value
        )
    ):
        raise InputError(f"{name} must be valid Unicode text of at most {maximum} characters.")
    if not empty and not value.strip():
        raise InputError(f"{name} must not be empty.")
    return value


def validate_identifier(value: str, name: str, maximum: int) -> str:
    validate_text(value, name, maximum)
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise InputError(f"{name} must not contain control or formatting characters.")
    return value


def validate_integer(value: int, name: str, minimum: int = 1, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError(f"{name} must be a positive integer between {minimum} and {maximum}.")
    return value


def fulltext_terms(query: str) -> list[str]:
    """Prefer informative words when a natural-language task exceeds the FTS budget."""
    words = list(
        dict.fromkeys(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", query).casefold()))
    )
    return sorted(words, key=len, reverse=True)


def normalize_projects(projects) -> tuple[str, ...] | None:
    if projects is None:
        return None
    if not isinstance(projects, (tuple, list, set, frozenset)):
        raise InputError("projects must be a collection of project names, not a string.")
    return tuple(sorted({validate_identifier(project, "project", 200) for project in projects}))


def require_sqlite() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise CapabilityError("SQLite 3.42 or newer with FTS5 is required for secure deletion.")


def secure_fts(db: sqlite3.Connection) -> None:
    """Enable real FTS deletion and compact tombstones from earlier releases once."""
    require_sqlite()
    configured = db.execute("SELECT v FROM memory_fts_config WHERE k='secure-delete'").fetchone()
    if not configured or configured[0] != 1:
        db.execute("INSERT INTO memory_fts(memory_fts,rank) VALUES ('secure-delete',1)")
        db.execute("INSERT INTO memory_fts(memory_fts) VALUES ('optimize')")


def rebuild_fts(db: sqlite3.Connection) -> None:
    """Rebuild both derived indexes from canonical current revisions."""
    secure_fts(db)
    db.execute("DELETE FROM memory_fts_rows")
    db.execute("DELETE FROM memory_fts")
    db.execute(
        "INSERT INTO memory_fts(memory_id,title,content) "
        "SELECT m.id,r.title,r.content FROM memories m JOIN revisions r "
        "ON r.memory_id=m.id AND r.revision=m.current_revision ORDER BY m.id"
    )
    db.execute(
        "INSERT INTO memory_fts_rows(memory_id,fts_rowid) SELECT memory_id,rowid FROM memory_fts"
    )


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
        require_sqlite()
        if marker_path(self).exists():
            raise StoreDisabledError("This installation is disabled; inspect its recovery state.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A distinct schema lock avoids reentering installation/service locks held by callers.
        with locked(self.directory / "schema.lock"):
            self._initialize()

    def _initialize(self) -> None:
        if self.path.is_symlink():
            raise InputError("The database must not be a symbolic link.")
        if not self.path.exists():
            self._create_database()
        with self.connection() as db:
            validate_schema(db, SCHEMA_VERSION)
            enable_wal(db)

    def _create_database(self) -> None:
        """Publish a complete initial database; a crash never leaves a version-zero store."""
        descriptor, name = tempfile.mkstemp(prefix=".dots-initialize-", dir=self.directory)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(temporary)) as db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA secure_delete=ON")
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    create_schema(db)
                    secure_fts(db)
            sync_file_and_parent(temporary)
            publish_new(temporary, self.path)
            sync_directory(self.directory)
        finally:
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + "-journal").unlink(missing_ok=True)

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        require_sqlite()
        if not self.path.exists():
            raise InputError("Memory is not initialized. Run dots-brain setup first.")
        if self.path.is_symlink():
            raise InputError("The database must not be a symbolic link.")
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version in (1, 2):
                raise MigrationRequiredError(
                    f"Database schema v{version} requires an explicit offline migration; "
                    "run dots-brain migrate, then migrate --apply --writers-stopped."
                )
            if version != SCHEMA_VERSION:
                raise IntegrityError(
                    "Unsupported database version; use a compatible Brain release."
                )
            application = db.execute("PRAGMA application_id").fetchone()[0]
            if application != APPLICATION_ID:
                raise IntegrityError("This file belongs to another application.")
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
        projects = normalize_projects(projects)
        if project is not None:
            validate_identifier(project, "project", 200)
            if projects is not None and project not in projects:
                return " AND 0", []
            return " AND m.project=?", [project]
        if projects is None:
            return "", []
        if not projects:
            return " AND 0", []
        return f" AND m.project IN ({','.join('?' for _ in projects)})", list(projects)

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
        for name, value, limit in (
            ("source", source, 200),
            ("account", account, 200),
            ("event_id", event_id, 500),
            ("project", project, 200),
        ):
            validate_identifier(value, name, limit)
        if expected_revision is not None:
            validate_integer(expected_revision, "expected_revision")
        projects = normalize_projects(projects)
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
                current = existing["current_revision"]
                candidate = (
                    current
                    if expected_revision == current
                    else (1 if expected_revision is None else expected_revision + 1)
                )
                replay = (
                    db.execute(
                        "SELECT revision FROM revisions "
                        "WHERE memory_id=? AND revision=? AND digest=?",
                        (memory_id, candidate, digest),
                    ).fetchone()
                    if candidate <= current
                    else None
                )
                if replay:
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
                # A derived rowid mapping cannot authorize deletion of another memory's text.
                db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            inserted = db.execute(
                "INSERT INTO memory_fts(memory_id,title,content) VALUES (?,?,?)",
                (memory_id, title, content),
            )
            db.execute(
                "INSERT INTO memory_fts_rows(memory_id,fts_rowid) VALUES (?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET fts_rowid=excluded.fts_rowid",
                (memory_id, inserted.lastrowid),
            )
        return {"id": memory_id, "revision": revision, "changed": True}

    def get(
        self,
        memory_id: str,
        *,
        revision: int | None = None,
        projects: tuple[str, ...] | None = None,
    ) -> dict:
        validate_identifier(memory_id, "memory_id", 500)
        if revision is not None:
            validate_integer(revision, "revision")
        clause, args = self._filter(projects)
        with self.connection() as db:
            row = db.execute(
                "SELECT m.id,m.source,m.account,m.event_id,m.project,m.created_at,m.updated_at,"
                "r.revision,r.title,r.content,r.source_uri,r.writer_principal,"
                "r.created_at AS revision_created_at "
                "FROM memories m JOIN revisions r "
                "ON r.memory_id=m.id AND r.revision=COALESCE(?,m.current_revision) "
                "WHERE m.id=?" + clause,
                [revision, memory_id, *args],
            ).fetchone()
        if row is None:
            raise NotFoundError("Memory is not available.")
        return dict(row)

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        limit: int = 10,
        projects: tuple[str, ...] | None = None,
    ) -> list[dict]:
        validate_text(query, "query", 2000)
        validate_integer(limit, "limit", maximum=50)
        terms = fulltext_terms(query)[:32]
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
        return [dict(row) for row in rows]

    def forget(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        projects: tuple[str, ...] | None = None,
        writer_principal: str = "local-owner:stdio",
    ) -> dict:
        validate_integer(expected_revision, "expected_revision")
        validate_identifier(memory_id, "memory_id", 500)
        clause, args = self._filter(projects)
        validate_text(writer_principal, "writer_principal", 500)
        with self.connection(write=True) as db:
            self.ensure_writable()
            secure_fts(db)
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
        if marker_path(self).exists():
            raise StoreDisabledError(
                "This installation is disabled; memory writes are unavailable."
            )

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
                "r.revision,r.content,r.title,r.source_uri,r.writer_principal,"
                "r.created_at AS revision_created_at "
                "FROM memories m JOIN revisions r "
                "ON r.memory_id=m.id WHERE 1" + clause + " ORDER BY m.id,r.revision",
                args,
            )
            for row in rows:
                yield dict(row)
