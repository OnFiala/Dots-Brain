"""Explicitly downloaded, pinned CPU embeddings; no inference API calls."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from .errors import CapabilityError
from .store import Store

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REPO = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
MODEL_REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
MODEL_ID = f"{MODEL_REPO}@{MODEL_REVISION}:token-windows-v2"
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
    "config.json": ("git", "5b496dbbbe502a10e2d64525481c6f444d125403"),
    "special_tokens_map.json": ("git", "b1879d702821e753ffe4245048eee415d54a9385"),
    "tokenizer_config.json": ("git", "6af3e8bba20e4425103afb1ae3dee3cacdbe7afb"),
}


def model_directory(store: Store) -> Path:
    return store.directory / "models" / MODEL_REVISION


def verify_file(path: Path, kind: str, expected: str) -> bool:
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
        temporary = directory / f"{name}.{os.getpid()}.part"
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
        try:
            with urlopen(Request(url, headers={"User-Agent": "Dots-Brain/0.1"}), timeout=60) as src:
                with temporary.open("xb") as dst:
                    while block := src.read(1024 * 1024):
                        dst.write(block)
            if not verify_file(temporary, kind, checksum):
                raise CapabilityError("A model artifact failed integrity verification.")
            temporary.replace(target)
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
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT m.id,m.current_revision AS revision,r.title,r.content "
                "FROM memories m JOIN revisions r "
                "ON r.memory_id=m.id AND r.revision=m.current_revision WHERE NOT EXISTS "
                "(SELECT 1 FROM semantic_chunks v WHERE v.memory_id=m.id AND v.model=? "
                "AND v.revision=m.current_revision) ORDER BY m.id LIMIT ?",
                (MODEL_ID, batch_size),
            ).fetchall()
        indexed = 0
        for row in rows:
            chunks = [
                (field, start, end, self._embed(row[field][start:end]))
                for field in ("title", "content")
                if row[field].strip()
                for start, end in self._chunks(row[field])
            ]
            with self.store.connection(write=True) as db:
                current = db.execute(
                    "SELECT current_revision AS revision FROM memories WHERE id=?", (row["id"],)
                ).fetchone()
                if current is None or current["revision"] != row["revision"]:
                    continue
                db.execute(
                    "DELETE FROM semantic_chunks WHERE memory_id=? AND model=?",
                    (row["id"], MODEL_ID),
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
                indexed += 1
        return {"indexed": indexed, "examined": len(rows), **self.status()}

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
        return {
            "state": "ready" if count == indexed else "pending_index",
            "model": MODEL_ID,
            "indexed": indexed,
            "pending": count - indexed,
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
        vectors = [self._embed(query[start:end]) for start, end in self._chunks(query)]
        clause, args = self.store._filter(projects, project)
        best = {}
        with self.store.connection() as db:
            cursor = db.execute(
                "SELECT m.id,m.current_revision AS revision,v.dimension,v.vector,"
                "v.start_char,v.end_char,v.field FROM memories m JOIN semantic_chunks v "
                "ON v.memory_id=m.id AND v.revision=m.current_revision WHERE v.model=?" + clause,
                [MODEL_ID, *args],
            )
            for row in cursor:
                other = self.np.frombuffer(row["vector"], dtype="<f4")
                if row["dimension"] != len(vectors[0]) or len(other) != len(vectors[0]):
                    raise CapabilityError("A stored vector has incompatible dimensions.")
                if not self.np.isfinite(other).all():
                    raise CapabilityError("A stored vector contains invalid values.")
                score = max(float(self.np.dot(vector, other)) for vector in vectors)
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
