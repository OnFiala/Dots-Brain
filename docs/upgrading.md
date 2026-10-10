# Upgrading and recovery

Keep one canonical data directory. Read the target changelog and use an immutable
release tag for a production upgrade. A development branch is not a release.

## Routine upgrade

1. Record the running version, executable path, data directory, supervisor and
   client configuration paths. Do not copy credentials into the record.
2. Stop clients and every writer, including the managed service and any external
   supervisor.
3. Create a validated backup with the version that currently owns the store.
4. For a published release, check out its tag in the existing checkout or install
   it in the environment already referenced by managed client configuration.
5. Run the offline migration below before starting the new binary. Then start
   the supervisor, run `doctor`, reload each client and make a real MCP read.

The `main` branch is the 0.3 line. This unreleased 0.4.0-alpha.2 candidate lives
on `codex/shared-memory-safety`; checking out that branch is an explicit test of
an unreleased candidate, not a normal upgrade.

## Schema v1 or v2 to v3

The current candidate requires schema v3. Both published v1 stores and older v2
stores need this migration. `setup`, `serve` and OAuth configuration do not add
missing tables. `doctor` reports `migration_required` until migration completes.

Stop the old process with its original supervisor and interpreter. Keep that
interpreter and the installation configuration for rollback. Managed-daemon
upgrade is still under recheck; do not infer a deployment approval from these
database migration tests.

Use the new interpreter for these commands, with all writers stopped. Choose a
new backup filename in an existing private directory:

```sh
dots-brain --data-dir /absolute/private/memory migrate
dots-brain --data-dir /absolute/private/memory migrate --apply --writers-stopped --backup /absolute/private/backup.sqlite3
dots-brain --data-dir /absolute/private/memory doctor
```

The plan includes `from_version` and `to_version` and does not checkpoint the
database. Apply makes a verified standalone backup including committed WAL data,
under SQLite's writer lock, then upgrades in one transaction. Another writer cannot
commit between the backup and migration. It preserves revisions, deletion barriers,
static credentials, OAuth grants and tokens. It rebuilds the derived full-text
index. OAuth configuration does not need to be repeated for the same issuer.
Historical grants keep unknown provenance fields empty.

If migration fails, keep the backup and error result. Repeating a completed
migration returns `already_current`. Old v2 binaries reject the new v3 database.

## Rollback before accepting new writes

Stop the candidate and every client. Copy the pre-upgrade backup to
`brain.sqlite3` in a **new** private directory. Restore `oauth.json` for the same
issuer and, if configured, `cortex.json`. Preserve any credential files referenced
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
or unknown formats are rejected before any schema or journal write. This marker
identifies the format; it cannot protect against a local owner editing the file.
