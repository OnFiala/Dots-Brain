# Upgrading and recovery

Keep one canonical data directory. Read the target changelog and use an immutable
release tag for a production upgrade. A development branch is not a release.

## Routine upgrade

1. Record the running version, executable path, data directory, supervisor and
   client configuration paths. Do not copy credentials into the record.
2. Stop clients and every writer, including the managed service and any external
   supervisor.
3. With the old process stopped, copy the database and any committed `-wal` and
   `-shm` files to a private backup location. Keep that full copy unchanged.
4. For a published release, check out its tag in the existing checkout or install
   it in the environment already referenced by managed client configuration.
5. Run the offline migration below before starting the new binary. Then start
   the supervisor, run `doctor`, reload each client and make a real MCP read.

Use a reviewed commit or published tag. This unreleased candidate is not a normal
upgrade path and this documentation makes no production-upgrade claim.

### HTTP and OAuth listener upgrade

Update the supervisor's command before reconnecting a public gateway. Use one
service with `serve --transport http --port 8765 --public-port 8787`; local static
clients keep port 8765 and the gateway must target only port 8787. A previous
single public listener can still use `--public-gateway`, but cannot also serve
private static credentials on that socket. Do not run a second service for the
same store. See [OAuth](oauth.md) for the full policy and diagnostics.

```sh
dots-brain --data-dir /absolute/private/memory oauth status
dots-brain --data-dir /absolute/private/memory oauth recover
dots-brain --data-dir /absolute/private/memory serve --transport http --port 8765 --public-port 8787
```

`oauth recover` is a no-op when no configuration journal exists. It restores a
valid interrupted transition without discarding prior grants. Invalid journals
remain for explicit owner recovery. On first HTTP startup, absent legacy listener
diagnostics are replaced by version-1 `http-listeners.json`; this derived state
does not change the database schema, tokens or listener authorization policy.

## Schema v1, v2 or v3 to v4

The `0.4.0a4` candidate requires schema v4. The earlier unreleased `0.4.0a2`
number was reused across v2 and v3 builds; record the exact commit when identifying
one of those builds. `0.4.0a3` uses v3. Published v1 and older v2/v3
stores need this migration. `setup`, `serve` and OAuth configuration do not add
missing tables. `doctor` reports `migration_required` until migration completes.

Stop the old process with its original supervisor and interpreter. Keep that
interpreter and the installation configuration for rollback. The new `down`
recognizes the exact managed command from 0.3.0-alpha.2 and
0.4.0-alpha.1 using PID start time, data path and port. It never adopts a different
release during `up`: stop it explicitly, migrate, then start. Startup replaces only
the old installation probe with a read-only credential; ordinary client scope
changes still require explicit replacement.

Use the new interpreter for these commands, with all writers stopped. Choose a
new backup filename in an existing private directory. `migrate --apply` takes a
second validated, standalone snapshot before changing the database; it does not
replace the full pre-upgrade copy made above.

```sh
dots-brain --data-dir /absolute/private/memory migrate
dots-brain --data-dir /absolute/private/memory migrate --apply --writers-stopped --backup /absolute/private/backup.sqlite3
dots-brain --data-dir /absolute/private/memory doctor
```

The plan includes `from_version` and `to_version`. It checks the same schema,
SQLite integrity, foreign keys, revision sequence and audit chain as apply.
It does not modify the main database or checkpoint committed WAL data. SQLite
read-only WAL access may create an empty WAL and shared-memory coordination file;
it never discards an existing committed WAL. A plan is a snapshot of that check,
so apply repeats validation after taking the writer lock.

Historical DDL is frozen separately from the current schema. Formatting outside
SQL tokens is ignored; literals, identifiers, constraints and triggers are checked.
This includes v1/v2 stores opened or migrated by commit `7085841`, and the full
v3 layout with its required application ID.

Apply makes a verified standalone backup including committed WAL data,
under SQLite's writer lock, then upgrades in one transaction. Another writer cannot
commit between the backup and migration. It preserves revisions, deletion barriers,
static credentials, OAuth grants and tokens. It rebuilds the derived full-text
index. OAuth configuration does not need to be repeated for the same issuer.
Historical grants keep unknown provenance fields empty.

V4 recreates the derived FTS table with indexed memory IDs so normal UUID updates
and deletions can find every copy without scanning unrelated text. Exact equality
still protects other IDs with the same tokenizer representation; unusual historical
IDs use the slower equality fallback. Text search matches only titles and content.
The extra indexed ID tokens increase index size and can change BM25 scores and
result ordering, including hybrid ranking. This follows SQLite's document-length
normalization in [BM25](https://www.sqlite.org/fts5.html#the_bm25_function).
Canonical contents, revisions, scopes and deletion barriers are unchanged.

### Failed migration and disk space

Reserve space for the original database, its snapshot, journals and temporary
tables. Use at least **3.5 times the database size** as an initial working allowance
for the original, backup and rebuilt index, plus existing WAL and manual copies.
The indexed IDs enlarge FTS, especially for very short records. These are planning
estimates, not guaranteed upper bounds; content and SQLite page usage vary.

After an ordinary failure, the migration rolls back and validates the old store.
Only then does it remove the new automatic backup it created. An explicit
`--backup` is retained for the operator, as are all pre-existing files and backups
when rollback cannot be proved. The error names the retained snapshot and the
next `migrate` check. A committed migration keeps its snapshot even if the later
WAL checkpoint must be deferred. Repeating it returns `already_current`.

A force-kill or power loss can leave `brain.sqlite3.vN-backup-*` or hidden
`.partial` files. Keep writers stopped; run
`dots-brain --data-dir /absolute/private/memory migrate` to validate the surviving
database before retrying. Inspect leftover paths individually. Remove only
abandoned outputs whose owner and recovery purpose you have confirmed; do not
use a wildcard cleanup or remove the only recovery copy.

Historical v2 snapshots may contain full-text terms from records deleted by an
older version. Treat every snapshot as private. After accepting the upgrade and
ending the rollback window, remove obsolete snapshots according to the host's
encrypted-storage retention policy. Ordinary file removal does not guarantee
physical erasure on SSDs or removal from filesystem snapshots and other backups.

### A plan reports corrupt or unsupported data

Do not remove constraints, repair revision numbers or edit the audit chain to
force migration. Keep the rejected store for inspection and stop all writers.
Use a known-good pre-upgrade snapshot in a **new** private directory:

```sh
mkdir -m 700 /absolute/private/recovery
cp /absolute/private/known-good.sqlite3 /absolute/private/recovery/brain.sqlite3
dots-brain --data-dir /absolute/private/recovery migrate
dots-brain --data-dir /absolute/private/recovery migrate --apply --writers-stopped
```

The `cp` example requires a standalone snapshot. For a manual WAL copy, include
its saved WAL companions as described below. The successful check and upgrade on
that copy do not reconcile later writes or deletions; review that loss window
before using it. Without a valid backup, preserve the evidence and recover it
offline before retrying. An unknown schema requires its compatible producer or a
separately reviewed migration; this candidate does not guess at foreign formats.

## Rollback before accepting new writes

Capture cursors and private review checkpoints now declare `format_version: 1`.
Keep their pre-upgrade copies with the private rollback material. Capture validates
and atomically upgrades unversioned/v0 cursors without replaying acknowledged
bytes. Review checkpoints upgrade only at explicit reviewed completion; legacy
pending evidence remains compatible. Future or malformed state is preserved with
a diagnostic/recovery command. See [state upgrades](activity.md#cursor-and-review-state).
The new review helper requires the compatible memory-host CLI option
`audit events --review-projection`; install that version through the separately
approved host upgrade path before running it. Source checkout changes alone are
not proof of that host's version.

Stop the candidate and every client. Copy the standalone snapshot produced by
`migrate --apply` to `brain.sqlite3` in a **new** private directory. If instead
using the manual pre-upgrade copy, restore the database together with its saved
WAL companions under their original names; copying only the main file can lose
committed writes. Restore `oauth.json` for the same issuer and, if configured,
`cortex.json`. Preserve any credential files referenced
by clients in their private locations; never include them in Git or terminal output.
Do not copy `service.json`, PID or lock files, `disabled.json`, restore markers or
capture cursors into the rollback directory. Recreate runtime state with the old
interpreter and supervisor after checking paths. Open the database copy with the
pinned old interpreter. First run
`doctor` and verify the expected record counts and permissions before changing
any client endpoint. Keep the migrated store for inspection.

This rollback is safe only before the candidate accepts new writes or deletions.
An older backup cannot reconcile those changes. Do not overwrite the current
database or remove recovery markers to force startup.

## Restore within the current schema

Backup and restore use staged directories. A restored copy is disabled until an
offline cutover validates current deletion and history state. Do not use a backup
as host-loss recovery: a surviving current store is required for reconciliation.
If validation fails, preserve both stores and follow the command's recovery result;
do not overwrite the current store or delete markers to force startup.

With every writer stopped, these are the current-schema commands. The source is
still canonical until activation succeeds:

```sh
dots-brain --data-dir /absolute/private/memory backup --output /absolute/private/current.sqlite3
dots-brain --data-dir /absolute/private/memory restore --backup /absolute/private/current.sqlite3 --target /absolute/private/restored
dots-brain --data-dir /absolute/private/memory activate-restore --target /absolute/private/restored --writers-stopped
```

After successful activation, point the supervisor at the restored directory and
reconnect clients with newly approved credentials. The old directory stays
disabled. Do not start it as a second writable copy.

The partial abort-recovery path cancels only a pending cutover and keeps the staged
target disabled. It does not undo a committed cutover. Use it after a failed
cutover only when all writers are stopped:

```sh
dots-brain --data-dir /absolute/private/memory abort-restore --target /absolute/private/restored --writers-stopped
```

Preserve both stores and inspect the command result before resuming service. This
candidate has not been deployed or accepted.

New databases and migrations carry Dots Brain's SQLite `application_id`. Migration
accepts historical ID zero only after checking the known table layouts. Foreign
or unknown formats are rejected by read-only preflight before a writable database
connection is opened. The main file and committed WAL are preserved. This marker
identifies the format; it cannot protect against a local owner editing the file.
