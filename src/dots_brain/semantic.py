"""Explicitly downloaded, pinned CPU embeddings; no inference API calls."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from . import __version__
from .errors import CapabilityError
from .store import Store, validate_integer, validate_text

logger = logging.getLogger(__name__)
MIN_SEMANTIC_SCORE = 0.20
MAX_CHUNKS_PER_MEMORY = 128
MAX_QUERY_CHUNKS = 16

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REPO = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
MODEL_REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
MODEL_ID = f"{MODEL_REPO}@{MODEL_REVISION}:token-windows-v2"
STATE_SQL = """CREATE TABLE IF NOT EXISTS semantic_state (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    model TEXT NOT NULL, revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('indexed','empty','failed')),
    truncated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(memory_id, model)
)"""
SCHEMA_SQL = (
    """
CREATE TABLE semantic_chunks (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, model TEXT NOT NULL, chunk_index INTEGER NOT NULL,
    field TEXT NOT NULL CHECK(field IN ('title','content')),
    start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
    dimension INTEGER NOT NULL, vector BLOB NOT NULL,
    PRIMARY KEY(memory_id, model, chunk_index)
)
""",
    STATE_SQL,
)
FILES = {
    "model_optimized.onnx": (
        "sha256",
        "634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99",
    ),
    "tokenizer.json": (
        "sha256",
        "fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00",
    ),
    "config.json": (
        "sha256",
        "c8ec081fdad2df991bf5abbf18418fec7a5cdaa421f60ffb060a30040b8c376f",
    ),
    "special_tokens_map.json": (
        "sha256",
        "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
    ),
    "tokenizer_config.json": (
        "sha256",
        "0666eebf692422757e1dddf3c9fb1ded73ba3dc726c5828671fc89e45bf3609f",
    ),
}


def model_directory(store: Store) -> Path:
    return store.directory / "models" / MODEL_REVISION


def verify_file(path: Path, kind: str, expected: str) -> bool:
    if kind not in {"sha256", "git"}:
        raise CapabilityError("Unsupported model artifact hash algorithm.")
    if not path.is_file() or path.is_symlink():
        return False
    digest = hashlib.sha256() if kind == "sha256" else hashlib.sha1(usedforsecurity=False)
    if kind == "git":
        digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest() == expected


def prepare_model(store: Store) -> dict:
    directory = model_directory(store)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, (kind, checksum) in FILES.items():
        target = directory / name
        if verify_file(target, kind, checksum):
            continue
        fd, pending = tempfile.mkstemp(prefix=f".{name}.", suffix=".part", dir=directory)
        temporary = Path(pending)
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
        try:
            with os.fdopen(fd, "wb") as dst:
                with urlopen(
                    Request(url, headers={"User-Agent": f"Dots-Brain/{__version__}"}), timeout=60
                ) as src:
                    while block := src.read(1024 * 1024):
                        dst.write(block)
                dst.flush()
                os.fsync(dst.fileno())
            if not verify_file(temporary, kind, checksum):
                raise CapabilityError("A model artifact failed integrity verification.")
            temporary.replace(target)
            from .local import sync_directory

            sync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "model": MODEL_ID,
        "directory": str(directory),
        "integrity": "verified",
        "license": "Apache-2.0",
        "inference": "local_cpu",
    }


def verify_model_artifacts(store: Store) -> Path:
    directory = model_directory(store)
    for name, (kind, checksum) in FILES.items():
        if not verify_file(directory / name, kind, checksum):
            raise CapabilityError("Run dots-brain model prepare before enabling semantic search.")
    return directory


class SemanticIndex:
    def __init__(self, store: Store):
        with store.connection() as db:
            if (
                db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_state'").fetchone()
                is None
            ):
                raise CapabilityError("Run dots-brain setup to prepare semantic index state.")
        directory = verify_model_artifacts(store)
        try:
            import onnxruntime

            onnxruntime.disable_telemetry_events()
            import numpy
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise CapabilityError("Install the semantic extra to enable local embeddings.") from exc
        self.np = numpy
        self.store = store
        self.lock = threading.Lock()
        self.model = TextEmbedding(
            model_name=MODEL_NAME,
            cache_dir=str(directory),
            specific_model_path=str(directory),
            local_files_only=True,
            threads=2,
            providers=["CPUExecutionProvider"],
        )
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()

    def _chunks(self, text: str) -> list[tuple[int, int]]:
        """Token offsets keep every passage within the model's 128-token limit."""
        offsets = self.tokenizer.encode(text, add_special_tokens=False).offsets
        if not offsets:
            return [(0, len(text))]
        chunks = []
        for start in range(0, len(offsets), 80):
            end = min(start + 96, len(offsets))
            first = 0 if start == 0 else offsets[start][0]
            last = len(text) if end == len(offsets) else offsets[end - 1][1]
            chunks.append((first, last))
            if end == len(offsets):
                break
        return chunks

    def _embed(self, text: str):
        if len(self.tokenizer.encode(text, add_special_tokens=True).ids) > 128:
            raise CapabilityError("The passage exceeds the embedding token limit.")
        with self.lock:
            vector = next(iter(self.model.embed([text]))).astype("<f4")
        norm = self.np.linalg.norm(vector)
        if not self.np.isfinite(vector).all() or norm == 0:
            raise CapabilityError("The embedding model returned an invalid vector.")
        return vector / norm

    def index(self, *, batch_size: int = 16) -> dict:
        validate_integer(batch_size, "batch_size", maximum=128)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT m.id,m.current_revision AS revision,r.title,r.content "
                "FROM memories m JOIN revisions r "
                "ON r.memory_id=m.id AND r.revision=m.current_revision WHERE NOT EXISTS "
                "(SELECT 1 FROM semantic_chunks v WHERE v.memory_id=m.id AND v.model=? "
                "AND v.revision=m.current_revision) AND NOT EXISTS "
                "(SELECT 1 FROM semantic_state s WHERE s.memory_id=m.id AND s.model=? "
                "AND s.revision=m.current_revision) ORDER BY m.id LIMIT ?",
                (MODEL_ID, MODEL_ID, batch_size),
            ).fetchall()
        indexed = 0
        for row in rows:
            failed = False
            try:
                passages = [
                    (field, start, end)
                    for field in ("title", "content")
                    if row[field].strip()
                    for start, end in self._chunks(row[field])
                ]
                truncated = len(passages) > MAX_CHUNKS_PER_MEMORY
                chunks = [
                    (field, start, end, self._embed(row[field][start:end]))
                    for field, start, end in passages[:MAX_CHUNKS_PER_MEMORY]
                ]
            except Exception as exc:
                # Record a terminal attempt for this revision; explicit retry or a
                # new revision can try again without starving subsequent memories.
                logger.warning("Embedding failed exception_type=%s", type(exc).__name__)
                chunks, truncated, failed = [], False, True
            with self.store.connection(write=True) as db:
                current = db.execute(
                    "SELECT current_revision AS revision FROM memories WHERE id=?", (row["id"],)
                ).fetchone()
                if current is None or current["revision"] != row["revision"]:
                    continue
                db.execute(
                    "DELETE FROM semantic_chunks WHERE memory_id=?",
                    (row["id"],),
                )
                db.executemany(
                    "INSERT INTO semantic_chunks VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            row["id"],
                            row["revision"],
                            MODEL_ID,
                            number,
                            field,
                            start,
                            end,
                            len(vector),
                            vector.tobytes(),
                        )
                        for number, (field, start, end, vector) in enumerate(chunks)
                    ],
                )
                db.execute(
                    "INSERT INTO semantic_state(memory_id,model,revision,state,truncated) "
                    "VALUES (?,?,?,?,?) "
                    "ON CONFLICT(memory_id,model) DO UPDATE SET revision=excluded.revision,"
                    "state=excluded.state,truncated=excluded.truncated",
                    (
                        row["id"],
                        MODEL_ID,
                        row["revision"],
                        "failed" if failed else "indexed" if chunks else "empty",
                        int(truncated),
                    ),
                )
                indexed += int(bool(chunks))
        return {"indexed_now": indexed, "examined": len(rows), **self.status()}

    def retry_failed(self) -> int:
        """Operator-requested retry; keep successful and older model indexes intact."""
        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            return db.execute(
                "DELETE FROM semantic_state WHERE model=? AND state='failed'", (MODEL_ID,)
            ).rowcount

    def status(self, *, projects: tuple[str, ...] | None = None) -> dict:
        clause, args = self.store._filter(projects)
        with self.store.connection() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM memories m WHERE 1" + clause,
                args,
            ).fetchone()[0]
            indexed = db.execute(
                "SELECT COUNT(DISTINCT m.id) FROM memories m "
                "JOIN semantic_chunks v ON v.memory_id=m.id "
                "AND v.revision=m.current_revision WHERE v.model=?" + clause,
                [MODEL_ID, *args],
            ).fetchone()[0]
            attempts = db.execute(
                "SELECT s.state,COUNT(*),SUM(s.truncated) FROM semantic_state s "
                "JOIN memories m ON m.id=s.memory_id AND m.current_revision=s.revision "
                "WHERE s.model=?" + clause + " GROUP BY s.state",
                [MODEL_ID, *args],
            ).fetchall()
        counts = {row[0]: row[1] for row in attempts}
        failed, empty = counts.get("failed", 0), counts.get("empty", 0)
        truncated = sum(row[2] for row in attempts)
        pending = max(0, count - indexed - failed - empty)
        return {
            "state": "pending_index" if pending else "degraded" if failed or truncated else "ready",
            "model": MODEL_ID,
            "indexed": indexed,
            "pending": pending,
            "failed": failed,
            "empty": empty,
            "truncated": truncated,
            "inference": "local_cpu",
        }

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
        vectors = [
            self._embed(query[start:end]) for start, end in self._chunks(query)[:MAX_QUERY_CHUNKS]
        ]
        clause, args = self.store._filter(projects, project)
        best = {}
        invalid_vectors = 0
        with self.store.connection() as db:
            cursor = db.execute(
                "SELECT m.id,m.current_revision AS revision,v.dimension,v.vector,"
                "v.start_char,v.end_char,v.field FROM memories m JOIN semantic_chunks v "
                "ON v.memory_id=m.id AND v.revision=m.current_revision WHERE v.model=?" + clause,
                [MODEL_ID, *args],
            )
            for row in cursor:
                try:
                    other = self.np.frombuffer(row["vector"], dtype="<f4")
                except (TypeError, ValueError):
                    invalid_vectors += 1
                    continue
                if row["dimension"] != len(vectors[0]) or len(other) != len(vectors[0]):
                    invalid_vectors += 1
                    continue
                if not self.np.isfinite(other).all():
                    invalid_vectors += 1
                    continue
                score = max(float(self.np.dot(vector, other)) for vector in vectors)
                if score < MIN_SEMANTIC_SCORE:
                    continue
                candidate = (
                    score,
                    row["revision"],
                    row["start_char"],
                    row["end_char"],
                    row["field"],
                )
                if row["id"] not in best or candidate > best[row["id"]]:
                    best[row["id"]] = candidate
                if len(best) > limit * 2:
                    best = dict(
                        sorted(best.items(), key=lambda pair: pair[1], reverse=True)[:limit]
                    )
        if invalid_vectors:
            logger.warning("Skipped %d invalid semantic vectors in this search", invalid_vectors)
        records = []
        from .errors import NotFoundError

        for memory_id, (score, revision, start, end, field) in sorted(
            best.items(), key=lambda pair: pair[1], reverse=True
        )[:limit]:
            try:
                record = self.store.get(memory_id, projects=projects)
            except NotFoundError:
                continue
            if record["revision"] != revision:
                continue
            record["excerpt"] = record[field][start:end][:800]
            record.pop("content")
            record["passage"] = {"field": field, "start_char": start, "end_char": end}
            record["semantic_score"] = score
            records.append(record)
        return records
