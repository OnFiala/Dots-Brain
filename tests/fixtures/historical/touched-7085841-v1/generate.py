"""Reproduce both intermediate historical fixtures with the actual 7085841 source.

Run this script with PYTHONPATH set to the immutable 7085841 src directory:
    python generate.py EXISTING_HISTORICAL_FIXTURE_ROOT NEW_OUTPUT_ROOT

The inputs are the repository's public synthetic v1/v2 fixtures. The output
root must be new. This script never opens a user's installed database.
"""

import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from dots_brain.activity import AuditLog
from dots_brain.auth import Policy
from dots_brain.migrations import migrate_v1_to_v2
from dots_brain.store import Store

INTERMEDIATE_COMMIT = "708584169b50903ed83145676a506f7cbe017744"
SOURCE_HASHES = {
    "store": "a35752d5be0c3c0b3abe4f18198dd75e5ef0c4ca86f1a1866617efbc4d0fc391",
    "migrations": "cf0b7882ea3b361eccd4ea4a88ff6cd0ffb7944998bf649533dc102b5ded29eb",
    "activity": "b5a3b05de844bbda9e052554face989d4409677b6e9e11b4ccf6ee5831823ebc",
}
for module, digest in SOURCE_HASHES.items():
    module_path = Path(sys.modules[f"dots_brain.{module}"].__file__)
    assert hashlib.sha256(module_path.read_bytes()).hexdigest() == digest, module

source_root, output_root = map(Path, sys.argv[1:])
output_root.mkdir(mode=0o700)
for origin in ("v1-263c386", "v2-c963700"):
    original_manifest = json.loads((source_root / origin / "manifest.json").read_text())
    for name, digest in original_manifest["files"].items():
        assert hashlib.sha256((source_root / origin / name).read_bytes()).hexdigest() == digest
    origin_version = original_manifest["schema_version"]
    destination = output_root / f"touched-7085841-v{origin_version}"
    shutil.copytree(source_root / origin, destination)
    store = Store(destination)
    if origin_version == 1:
        migrate_v1_to_v2(
            store,
            apply=True,
            backup_path=output_root / "disposable-v1-backup.sqlite3",
            stop_guard=lambda: None,
        )
    store.initialize()
    AuditLog(store).record(
        Policy(principal="fixture:operator"),
        project="test",
        kind="action",
        client_event_id="fixture-action",
        action={"type": "fixture-check"},
        target={"type": "synthetic-record"},
        details={"result": "complete"},
        occurred_at="2026-01-01T00:00:00+00:00",
    )
    with closing(sqlite3.connect(store.path)) as db:
        db.execute(
            "INSERT INTO oauth_grants SELECT 'fixture-revoked-grant',client_id,scopes,"
            "projects,resource,expires,1 FROM oauth_grants WHERE id='fixture-grant'"
        )
        db.execute(
            "INSERT INTO oauth_tokens SELECT ?,kind,'fixture-revoked-grant',scopes,expires "
            "FROM oauth_tokens WHERE kind='access'",
            (hashlib.sha256(b"public-synthetic-revoked-token").hexdigest(),),
        )
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
        tables = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not row[0].startswith(("sqlite_", "memory_fts"))
        ]
        counts = {
            table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in sorted(tables)
        }
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        ddl = db.execute("SELECT sql FROM sqlite_master WHERE name='memory_fts_rows'").fetchone()[0]
    (destination / "schema.lock").unlink(missing_ok=True)
    (destination / "manifest.json").unlink()
    manifest = {
        "source_commit": original_manifest["source_commit"],
        "touched_by_commit": INTERMEDIATE_COMMIT,
        "producing_source_sha256": SOURCE_HASHES,
        "origin_schema_version": origin_version,
        "schema_version": 2,
        "generator": "tests/fixtures/historical/touched-7085841-v1/generate.py",
        "touch_actions": (["migrate_v1_to_v2"] if origin_version == 1 else [])
        + ["Store.initialize", "AuditLog.record"],
        "files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(destination.iterdir())
        },
        "canonical_counts": counts,
        "memory_fts_rows_sql": ddl,
        "memory_id": original_manifest["memory_id"],
        "probe_id": original_manifest["probe_id"],
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
