"""Explicitly downloaded, pinned CPU embeddings; no inference API calls."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
import time
from itertools import islice
from pathlib import Path
from urllib.request import Request, urlopen

from . import __version__
from .errors import CapabilityError
from .store import Store, validate_integer, validate_text

logger = logging.getLogger(__name__)
MIN_SEMANTIC_SCORE = 0.20
MAX_CHUNKS_PER_MEMORY = 128
MAX_QUERY_CHUNKS = 16
FAILED_RETRY_DELAY_SECONDS = 30
MODEL_VECTOR_DIMENSION = 384
REPAIR_SCAN_BATCH_SIZE = 4

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REPO = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
MODEL_REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
MODEL_ID = f"{MODEL_REPO}@{MODEL_REVISION}:token-windows-v2"
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
        self.lock = threading.RLock()
        self.model = TextEmbedding(
            model_name=MODEL_NAME,
            cache_dir=str(directory),
            specific_model_path=str(directory),
            local_files_only=True,
            threads=2,
            providers=["CPUExecutionProvider"],
        )
        # FastEmbed already owns the tokenizer used by its ONNX model.  Loading
        # tokenizer.json again doubles a large native allocation on this model.
        self.tokenizer = self.model.model.tokenizer
        if self.tokenizer is None:
            raise CapabilityError("The embedding model did not load its tokenizer.")
        self._failed_retry_deadlines: dict[tuple[str, int], float] = {}
        self._failed_cursor = ""
        self._repair_cursor = ""
        self._repair_requested: set[tuple[str, int]] = set()

    def _vector_dimension(self) -> int:
        return getattr(self, "vector_dimension", MODEL_VECTOR_DIMENSION)

    def _valid_vector(self, vector: bytes | None, dimension: int | None) -> bool:
        expected = self._vector_dimension()
        if dimension != expected:
            return False
        try:
            values = self.np.frombuffer(vector, dtype="<f4")
        except (TypeError, ValueError):
            return False
        return len(values) == expected and self.np.isfinite(values).all()

    def _encode_for_chunks(self, text: str, *, add_special_tokens: bool):
        """Use FastEmbed's tokenizer without changing its embedding configuration."""
        with self.lock:
            truncation, padding = self.tokenizer.truncation, self.tokenizer.padding
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
            try:
                return self.tokenizer.encode(text, add_special_tokens=add_special_tokens)
            finally:
                if truncation:
                    self.tokenizer.enable_truncation(**truncation)
                if padding:
                    self.tokenizer.enable_padding(**padding)

    def _chunks(self, text: str) -> list[tuple[int, int]]:
        """Token offsets keep every passage within the model's 128-token limit."""
        offsets = self._encode_for_chunks(text, add_special_tokens=False).offsets
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
        with self.lock:
            if len(self._encode_for_chunks(text, add_special_tokens=True).ids) > 128:
                raise CapabilityError("The passage exceeds the embedding token limit.")
            vector = next(iter(self.model.embed([text]))).astype("<f4")
        if vector.shape != (self._vector_dimension(),):
            raise CapabilityError("The embedding model returned the wrong vector dimension.")
        norm = self.np.linalg.norm(vector)
        if not self.np.isfinite(vector).all() or not self.np.isfinite(norm) or norm == 0:
            raise CapabilityError("The embedding model returned an invalid vector.")
        return vector / norm

    def _repair_candidates(self, db, *, limit: int) -> list:
        """Bounded integrity sweep; background indexing never scans every vector."""
        requested = getattr(self, "_repair_requested", set())
        candidates = []
        for memory_id, revision in list(islice(requested, limit)):
            row = db.execute(
                "SELECT m.id,m.current_revision AS revision,r.title,r.content,s.state "
                "FROM memories m "
                "JOIN revisions r ON r.memory_id=m.id AND r.revision=m.current_revision "
                "JOIN semantic_state s ON s.memory_id=m.id AND s.model=? "
                "AND s.revision=m.current_revision WHERE m.id=? AND s.state='indexed'",
                (MODEL_ID, memory_id),
            ).fetchone()
            if row is not None and row["revision"] == revision:
                candidates.append(row)
            else:
                requested.discard((memory_id, revision))
        if len(candidates) == limit:
            return candidates

        cursor = getattr(self, "_repair_cursor", "")

        def scan(after: str):
            return db.execute(
                "SELECT m.id,m.current_revision AS revision,r.title,r.content,s.state "
                "FROM memories m "
                "JOIN revisions r ON r.memory_id=m.id AND r.revision=m.current_revision "
                "JOIN semantic_state s ON s.memory_id=m.id AND s.model=? "
                "AND s.revision=m.current_revision WHERE s.state='indexed' AND m.id>? "
                "ORDER BY m.id LIMIT ?",
                (MODEL_ID, after, limit - len(candidates)),
            ).fetchall()

        scanned = scan(cursor)
        if not scanned and cursor:
            scanned = scan("")
        if scanned:
            self._repair_cursor = scanned[-1]["id"]
        for row in scanned:
            if row["id"] in {candidate["id"] for candidate in candidates}:
                continue
            vectors = db.execute(
                "SELECT dimension,vector FROM semantic_chunks WHERE memory_id=? AND model=? "
                "AND revision=?",
                (row["id"], MODEL_ID, row["revision"]),
            ).fetchall()
            invalid = not vectors or any(
                not self._valid_vector(vector["vector"], vector["dimension"]) for vector in vectors
            )
            if invalid:
                candidates.append(row)
        return candidates

    def _failed_candidates(
        self, db, deadlines: dict[tuple[str, int], float], *, now: float, limit: int
    ) -> list:
        """Return due failures without letting deferred rows block new source work."""
        candidates = []
        seen = set()
        # Rotate a bounded slice, including stale entries. A long failure backlog
        # must not become one SQL lookup per failed memory on every idle tick.
        for key in list(islice(deadlines, limit)):
            deadline = deadlines.pop(key)
            row = db.execute(
                "SELECT m.id,m.current_revision AS revision,r.title,r.content,s.state "
                "FROM memories m JOIN revisions r ON r.memory_id=m.id "
                "AND r.revision=m.current_revision JOIN semantic_state s "
                "ON s.memory_id=m.id AND s.model=? AND s.revision=m.current_revision "
                "WHERE m.id=? AND s.state='failed'",
                (MODEL_ID, key[0]),
            ).fetchone()
            if row is None or row["revision"] != key[1]:
                continue
            deadlines[key] = deadline
            if deadline <= now:
                candidates.append(row)
                seen.add(key)

        if len(candidates) == limit:
            return candidates
        cursor = getattr(self, "_failed_cursor", "")

        def scan(after: str):
            return db.execute(
                "SELECT m.id,m.current_revision AS revision,r.title,r.content,s.state "
                "FROM memories m JOIN revisions r ON r.memory_id=m.id "
                "AND r.revision=m.current_revision JOIN semantic_state s "
                "ON s.memory_id=m.id AND s.model=? AND s.revision=m.current_revision "
                "WHERE s.state='failed' AND m.id>? ORDER BY m.id LIMIT ?",
                (MODEL_ID, after, limit),
            ).fetchall()

        discovered = scan(cursor)
        if not discovered and cursor:
            discovered = scan("")
        if discovered:
            self._failed_cursor = discovered[-1]["id"]
        for row in discovered:
            key = (row["id"], row["revision"])
            deadline = deadlines.setdefault(key, now)
            if key not in seen and deadline <= now and len(candidates) < limit:
                candidates.append(row)
                seen.add(key)
        return candidates

    def index(self, *, batch_size: int = 16, summary: bool = True) -> dict:
        validate_integer(batch_size, "batch_size", maximum=128)
        self.store.ensure_writable()
        now = time.monotonic()
        deadlines = getattr(self, "_failed_retry_deadlines", None)
        if deadlines is None:
            deadlines = self._failed_retry_deadlines = {}
        with self.store.connection() as db:
            # This bounded query is the source-of-truth change detector.  Do not
            # collapse it to an aggregate: a same-size revision-one replacement
            # has the same count and revision sum, but still needs indexing.
            rows = db.execute(
                "SELECT m.id,m.current_revision AS revision,r.title,r.content,s.state "
                "FROM memories m JOIN revisions r ON r.memory_id=m.id "
                "AND r.revision=m.current_revision LEFT JOIN semantic_state s "
                "ON s.memory_id=m.id AND s.model=? AND s.revision=m.current_revision "
                "WHERE s.state IS NULL ORDER BY m.id LIMIT ?",
                (MODEL_ID, batch_size),
            ).fetchall()
            known = {(row["id"], row["revision"]) for row in rows}
            if len(rows) < batch_size:
                failed = self._failed_candidates(
                    db, deadlines, now=now, limit=batch_size - len(rows)
                )
                rows.extend(row for row in failed if (row["id"], row["revision"]) not in known)
                known.update((row["id"], row["revision"]) for row in rows)
            if len(rows) < batch_size:
                repair = self._repair_candidates(
                    db, limit=min(REPAIR_SCAN_BATCH_SIZE, batch_size - len(rows))
                )
                rows.extend(row for row in repair if (row["id"], row["revision"]) not in known)
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
                # Retain the failed state for truthful status, then retry it after
                # a bounded in-process delay without starving later memories.
                logger.warning("Embedding failed exception_type=%s", type(exc).__name__)
                chunks, truncated, failed = [], False, True
            with self.store.connection(write=True) as db:
                self.store.ensure_writable()
                current = db.execute(
                    "SELECT current_revision AS revision FROM memories WHERE id=?", (row["id"],)
                ).fetchone()
                if current is None or current["revision"] != row["revision"]:
                    deadlines.pop((row["id"], row["revision"]), None)
                    continue
                db.execute("DELETE FROM semantic_chunks WHERE memory_id=?", (row["id"],))
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
                    "VALUES (?,?,?,?,?) ON CONFLICT(memory_id,model) DO UPDATE SET "
                    "revision=excluded.revision,state=excluded.state,truncated=excluded.truncated",
                    (
                        row["id"],
                        MODEL_ID,
                        row["revision"],
                        "failed" if failed else "indexed" if chunks else "empty",
                        int(truncated),
                    ),
                )
                indexed += int(bool(chunks))
                key = (row["id"], row["revision"])
                if failed:
                    deadlines[key] = time.monotonic() + FAILED_RETRY_DELAY_SECONDS
                else:
                    deadlines.pop(key, None)
                    getattr(self, "_repair_requested", set()).discard(key)
        result = {"indexed_now": indexed, "examined": len(rows)}
        return {**result, **self.status()} if summary else result

    def retry_failed(self) -> int:
        """Operator-requested retry; keep successful and older model indexes intact."""
        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            retried = db.execute(
                "DELETE FROM semantic_state WHERE model=? AND state='failed'", (MODEL_ID,)
            ).rowcount
        getattr(self, "_failed_retry_deadlines", {}).clear()
        return retried

    def status(self, *, projects: tuple[str, ...] | None = None) -> dict:
        clause, args = self.store._filter(projects)
        with self.store.connection() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM memories m WHERE 1" + clause,
                args,
            ).fetchone()[0]
            indexed_rows = db.execute(
                "SELECT m.id,m.current_revision AS revision,v.dimension,v.vector FROM memories m "
                "JOIN semantic_state s ON s.memory_id=m.id AND s.model=? "
                "AND s.revision=m.current_revision JOIN semantic_chunks v ON v.memory_id=m.id "
                "AND v.model=s.model AND v.revision=s.revision WHERE s.state='indexed'" + clause,
                [MODEL_ID, *args],
            ).fetchall()
            attempts = db.execute(
                "SELECT s.state,COUNT(*),SUM(s.truncated) FROM semantic_state s "
                "JOIN memories m ON m.id=s.memory_id AND m.current_revision=s.revision "
                "WHERE s.model=?" + clause + " GROUP BY s.state",
                [MODEL_ID, *args],
            ).fetchall()
        counts = {row[0]: row[1] for row in attempts}
        failed, empty = counts.get("failed", 0), counts.get("empty", 0)
        indexed_ids = {row["id"] for row in indexed_rows}
        invalid_ids = {
            row["id"]
            for row in indexed_rows
            if not self._valid_vector(row["vector"], row["dimension"])
        }
        indexed = len(indexed_ids - invalid_ids)
        repair_pending = max(0, counts.get("indexed", 0) - indexed)
        truncated = sum(row[2] for row in attempts)
        pending = max(0, count - indexed - failed - empty)
        return {
            "state": "pending_index" if pending else "degraded" if failed or truncated else "ready",
            "model": MODEL_ID,
            "indexed": indexed,
            "pending": pending,
            "failed": failed,
            "empty": empty,
            "repair_pending": repair_pending,
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
        if not vectors or any(len(vector) != self._vector_dimension() for vector in vectors):
            raise CapabilityError("The embedding model returned an unexpected vector dimension.")
        clause, args = self.store._filter(projects, project)
        best = {}
        invalid_vectors = 0
        requested = getattr(self, "_repair_requested", None)
        if requested is None:
            requested = self._repair_requested = set()
        with self.store.connection() as db:
            cursor = db.execute(
                "SELECT m.id,m.current_revision AS revision,v.dimension,v.vector,"
                "v.start_char,v.end_char,v.field FROM memories m JOIN semantic_chunks v "
                "ON v.memory_id=m.id AND v.revision=m.current_revision WHERE v.model=?" + clause,
                [MODEL_ID, *args],
            )
            for row in cursor:
                if not self._valid_vector(row["vector"], row["dimension"]):
                    invalid_vectors += 1
                    requested.add((row["id"], row["revision"]))
                    continue
                other = self.np.frombuffer(row["vector"], dtype="<f4")
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
                passages = best.setdefault(row["id"], {"overall": candidate, "body": None})
                if candidate > passages["overall"]:
                    passages["overall"] = candidate
                if row["field"] == "content" and (
                    passages["body"] is None or candidate > passages["body"]
                ):
                    passages["body"] = candidate
                if len(best) > limit * 2:
                    best = dict(
                        sorted(best.items(), key=lambda pair: pair[1]["overall"], reverse=True)[
                            :limit
                        ]
                    )
        if invalid_vectors:
            logger.warning("Skipped %d invalid semantic vectors in this search", invalid_vectors)
        records = []
        from .errors import NotFoundError

        for memory_id, passages in sorted(
            best.items(), key=lambda pair: pair[1]["overall"], reverse=True
        )[:limit]:
            score, revision, start, end, field = passages["body"] or passages["overall"]
            overall_score = passages["overall"][0]
            try:
                record = self.store.get(memory_id, projects=projects)
            except NotFoundError:
                continue
            if record["revision"] != revision:
                continue
            record["excerpt"] = record[field][start:end][:800]
            record.pop("content")
            record["passage"] = {"field": field, "start_char": start, "end_char": end}
            record["semantic_score"] = overall_score
            records.append(record)
        return records
