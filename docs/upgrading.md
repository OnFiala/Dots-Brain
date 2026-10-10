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
5. Start the supervisor, run `doctor`, then reload each client and make a real
   MCP read.

The `main` branch is the 0.3 line. This unreleased 0.4.0-alpha.2 candidate lives
on `codex/shared-memory-safety`; checking out that branch is an explicit test of
an unreleased candidate, not a normal upgrade.

Schema v1 requires an explicit offline migration to schema v2. The migration must
run with all writers stopped and a fresh private backup:

```sh
dots-brain --data-dir /absolute/private/memory migrate
dots-brain --data-dir /absolute/private/memory migrate --apply --writers-stopped --backup /absolute/private/backup.sqlite3
```

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

New databases and v1 migrations carry Dots Brain's SQLite `application_id`.
Earlier schema-v2 databases with ID zero remain compatible; a nonzero foreign ID
is rejected. This marker identifies the format and is not protection against a
local owner who can edit the database directly.
