"""Run with PYTHONPATH pointing to the manifest's immutable historical src tree.

Only synthetic records and public test tokens are produced. Never use a real
installation as input. The output directory must not already exist.
"""

import hashlib
import json
import sys
from pathlib import Path

from dots_brain.auth import issue_client
from dots_brain.oauth import configure
from dots_brain.store import SCHEMA_VERSION, Store

commit, destination = sys.argv[1], Path(sys.argv[2])
destination.mkdir(mode=0o700)
store = Store(destination)
store.initialize()
identity = dict(source="fixture", account="test", project="test")
kept = store.remember(**identity, event_id="keep", content="Synthetic first revision")
store.remember(
    **identity, event_id="keep", content="Synthetic second revision", expected_revision=1
)
deleted = store.remember(**identity, event_id="deleted", content="Synthetic deleted note")
store.forget(deleted["id"], **({"expected_revision": 1} if SCHEMA_VERSION >= 2 else {}))
issue_client(
    store,
    name="service-probe",
    scopes=["memory:read", "memory:write", "memory:forget"],
    projects=None,
    days=365,
    output=destination / "service-probe.json",
    url="http://127.0.0.1:9876/mcp",
)
probe = json.loads((destination / "service-probe.json").read_text())
probe["token"] = "public-synthetic-upgrade-probe"
(destination / "service-probe.json").write_text(json.dumps(probe) + "\n")
configure(store, "https://memory.example.test")
with store.connection() as db:
    db.execute(
        "UPDATE clients SET token_hash=?,expires_at=?",
        (hashlib.sha256(probe["token"].encode()).hexdigest(), 4102444800),
    )
    db.execute(
        "INSERT INTO oauth_clients VALUES (?,?,?)",
        (
            "fixture-client",
            json.dumps({"client_id": "fixture-client", "client_name": "Fixture"}),
            0,
        ),
    )
    db.execute(
        "INSERT INTO oauth_grants VALUES (?,?,?,?,?,?,?)",
        (
            "fixture-grant",
            "fixture-client",
            '["memory:read"]',
            '["test"]',
            "https://memory.example.test/mcp",
            4102444800,
            0,
        ),
    )
    db.execute(
        "INSERT INTO oauth_tokens VALUES (?,?,?,?,?)",
        (
            hashlib.sha256(b"public-synthetic-oauth-token").hexdigest(),
            "access",
            "fixture-grant",
            '["memory:read"]',
            4102444800,
        ),
    )
    version = db.execute("PRAGMA user_version").fetchone()[0]
files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.iterdir()}
(destination / "manifest.json").write_text(
    json.dumps(
        {
            "source_commit": commit,
            "schema_version": version,
            "generator": "tests/fixtures/historical/generate.py",
            "files": files,
            "memory_id": kept["id"],
            "revision": 2,
            "probe_id": probe["client_id"],
            "oauth_grant_id": "fixture-grant",
        },
        indent=2,
    )
    + "\n"
)
