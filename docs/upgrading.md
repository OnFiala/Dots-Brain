# V2 upgrade and recovery

This version retains application schema **v2**. Production rollout requires separate owner approval
and evidence for the exact installed artifact. Keep one canonical data directory.
Do not restore an older database over newer writes, revisions or deletions.

## Compatibility and offline maintenance

Python must supply SQLite **3.42 or newer with FTS5**. Dots Brain enables FTS5
secure-delete and compacts old FTS tombstones once, in a transaction. The ordinary
SQLite secure-delete setting remains enabled. Application schema v2 does not
mean the FTS file remains readable by older SQLite builds. Existing backups,
filesystem snapshots and old WAL frames are not erased by this change.

Supported HTTP and stdio servers hold a shared `writers.lock` for their lifetime;
local CLI writes hold it for the operation. Maintenance takes its exclusive side.
Nested Store connections reuse the current operation's lease. HTTP also holds
`http.lock`: one process owns the HTTP listeners for one data directory. These
locks coordinate supported entrypoints, not arbitrary SQLite programs or old
binaries that do not participate. Stop those explicitly as well.

No command searches for or kills foreign processes. Stop the original supervisor
and all clients before migration or activation, then pass `--writers-stopped`.
An occupied maintenance lock refuses the operation. Advisory lock files are not
PID files; do not delete them while a process might hold them.

## Updating to 0.4.3

Version 0.4.3 gives replacement full-text documents a fresh row ID. This prevents
the reproduced secure-delete failure after updating and deleting a long record.
The application schema and canonical memory/revision format remain unchanged.

Installing the fix does not repair an already damaged full-text index. Check
database integrity before promotion. If it reports a malformed `memory_fts`
inverted index, preserve a private backup and arrange an approved offline rebuild
of that derived index. Stop writers and hold the exclusive maintenance lease;
verify canonical memories, revisions and the complete audit chain are unchanged
before committing the repair. Keep secure deletion enabled. An index rebuild
does not require restoring an older database or resetting OAuth access.

## Released 0.3 / schema v1 to v2

Record the old executable, source version, supervisor and data directory. Preserve
a private pre-upgrade copy including any committed WAL companions while all
writers are stopped. Keep credentials out of Git and terminal output.

```sh
dots-brain --data-dir /absolute/private/memory migrate
dots-brain --data-dir /absolute/private/memory migrate --apply --writers-stopped
```

The plan reads v1 integrity, foreign keys and current revisions without opening a
writable handle. Apply repeats the checks under the exclusive writer lease and a
SQLite write transaction. It creates a standalone private v1 snapshot beside the
database before DDL, preserves canonical records and credentials, and rebuilds
the derived full-text index. Existing OAuth installations receive the v2 provenance
tables without inventing historical approval metadata. An explicit `--backup`
path must also be beside the database. Repeating migration of v2 is a no-op.

A failed DDL transaction rolls back and retains its completed backup for inspection.
A process crash may leave an incomplete backup; do not use it until validated.
Unknown or newer application versions require a compatible release or a separate
migration plan. Migration does not relabel v3/v4 or accept unreleased formats.
Read-only WAL access can create SQLite coordination files; rejected version
preflight must not change the main file or checkpoint its committed WAL.

## Older 0.2 client configurations

The old provider-only credential layout is not adopted or rewritten automatically.
Connect reports the exact client configuration and a reconnect command. Close its
client/editor, manually remove only the named `dots-brain` MCP entry, preserve
other settings and credential files, then run that command. Inspect the new bridge
read verification and reload the client. Database migration and this manual client
step are separate; see [disconnect and uninstall](uninstall.md) for ownership rules.

## Backup and staged restore

Use a private directory with no concurrent actor replacing backup files. `backup`
uses an exclusive new filename, SQLite's backup API and integrity checks. The
returned snapshot is explicitly closed in DELETE journal mode, without required
WAL companions. A backup is verified only after the command succeeds.

```sh
dots-brain --data-dir /absolute/private/memory backup --output /absolute/private/backup.sqlite3
dots-brain --data-dir /absolute/private/memory restore --backup /absolute/private/backup.sqlite3 --target /absolute/private/restored
```

Restore requires a new target directory and a surviving current store. It keeps
the input snapshot unchanged, merges deletion barriers and leaves the target
disabled. Static credentials are revoked during staging; OAuth credentials stay
quarantined behind the mandatory disabled barrier until activation can reset them
and append its audit event together. The result says `disabled_pending_activation`,
not that all credentials have already been revoked. A nonempty WAL sidecar is refused;
create a standalone backup with its compatible producer. For a v1 snapshot, copy
it to a new private working directory as `brain.sqlite3`, migrate that copy, then
create a v2 backup from the copy. Never migrate the original backup in place.
This is not recovery after losing the only current deletion state.

## Activation, failure and retry

```sh
dots-brain --data-dir /absolute/private/memory activate-restore --target /absolute/private/restored --writers-stopped
```

Activation locks both directories in stable order. It checks the recovery UUID,
source/target paths, current deletions, audit suffix and canonical history before
disabling the source. A rejected validation keeps an otherwise enabled source
writable; the target remains disabled. After the pending marker is written,
failures preserve the recovery barriers. Retry only the same still-disabled
source/target pair. A changed UUID, replacement path or unrelated barrier refuses
activation. No command automatically removes a recovery barrier.

Retained OAuth clients, grants and grant provenance must match the surviving
source. New grant history after the snapshot requires a current backup; activation
does not invent a history merge. Credential secrets, retirement flags and revoked
flags are excluded from this comparison because the target reset changes them.
The reset and its recovery-bound audit event commit in one target transaction.
An interrupted activation can retain exactly that one final event after the exact
source audit prefix. Retry verifies its hash, identity and reset state and does not
append it again. Unrelated audit suffixes or changed credentials refuse activation.

After target commit and the source committed marker, the source stays disabled.
If enabling the target or its final fsync fails, inspect both markers: target
activation may already have happened. Do not assume rollback or restart the old
source. A successful retry of the same disabled target completes activation.
Repointing the supervisor and issuing new credentials remain explicit operator
steps; activation does not start a server.

Uninstall preserves existing recovery and unknown barriers. `up --resume` accepts
only an ordinary uninstall marker and restores it if startup fails. Recovery uses
`activate-restore`; unknown state requires inspection with `doctor`.

## Rollback boundary

Each local source checkpoint can be reverted on disposable test data. A code
revert cannot restore deleted content, revoked grants or the old FTS format.
Preserve SQLite 3.42+, deletion suppression and the new authorization state in any
replacement artifact. Once an installation accepts real writes, do not roll back
to an old snapshot or an incompatible binary. Prepare a compatible replacement
or an explicitly approved outage and forward repair before deployment.
