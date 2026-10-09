# Upgrading and recovery

The `0.4.0-alpha.1` candidate uses **schema v2**. The published `0.3.0-alpha.2`
uses v1. Never run old and new binaries against the same database. New code rejects
v1 until explicit migration; renamed columns also prevent old write SQL succeeding.
That is an extra safeguard, not permission to leave old processes running.

## Offline v1 migration

1. Record the exact executable/version, data directory, client paths, supervisor,
   OAuth issuer, model options and rollback package. Do not print credentials.
2. Stop connected bridges, stdio processes, the managed service (`down`) and any
   external supervisor. Keep them stopped through migration and dependency changes.
3. Install the candidate in an immutable separate environment. Run its command:

```sh
dots-brain --data-dir /private/current migrate
dots-brain --data-dir /private/current migrate --apply --writers-stopped \
  --backup /private/backups/pre-v2.sqlite3
```

The dry run only reports the version. Apply holds local operator locks, rejects
an active managed process, writes a private exclusive verified/fsynced v1 backup,
and migrates in one transaction. The flag attests that **all** other writers are
stopped; the program cannot discover arbitrary external processes. A failed
migration rolls back and retains the backup. Repeating a completed migration is a no-op.

Memory history, credentials, OAuth state and old suppression hashes are preserved.
Historical authorship is unknown. New source identities and deletion barriers are
project-scoped; legacy hashes remain global because their projects are unrecoverable.
Semantic passage indexes are derived and rebuild with the new model index identity.

Start the exact new executable under the original supervisor, run `doctor`, refresh
MCP tool discovery and verify actual calls. Deletion now requires the positive
`expected_revision` that the caller reviewed. New audit/CORTEX scopes are not added
to existing grants or default OAuth approvals.

## OAuth provenance extension within schema v2

The candidate now retains the original pairing request ID and its creation time
with each new grant. Three companion tables keep the existing OAuth table layouts
unchanged. Existing grants are not revoked or widened, and missing historical
provenance remains `null`; it is never reconstructed from expiry times.

Before starting this code on an existing OAuth installation:

1. Record the existing issuer with `oauth status`, and stop the service and other
   writers. Preserve the current release for rollback.
2. Make and validate a private backup using the existing release.
3. Using the new release, run `oauth configure --issuer <exact-existing-origin> --no-start`
   against the same data directory. A different issuer revokes existing
   clients, so do not substitute or guess the origin.
4. Restart the supervised service and verify existing client access and owner
   `oauth grants` metadata. Repeating configuration with the same issuer is safe.

The constructor only checks the extension and reports a required upgrade; read
commands do not install it. Schema creation is transactional. Failure leaves the
original OAuth rows intact. A code produced before the upgrade still exchanges;
its unknown request association remains `null`.

Returning to the previous schema-v2 release keeps the companion tables in place.
Its existing positional SQL remains compatible. Flows created by that older
release have no new provenance, and later code reports that absence explicitly.
This compatibility does not apply to the published schema-v1 release.

## Online backup and staged restore

```sh
dots-brain --data-dir /private/current backup --output /private/backups/snapshot.sqlite3
dots-brain --data-dir /private/current restore \
  --backup /private/backups/snapshot.sqlite3 --target /private/restored
```

The output must be a new file in a private directory. SQLite backup includes WAL;
version, integrity, foreign keys and current revision chains are checked. A JSONL
export is not this backup. Keep snapshots private: they contain confidential
memory and authentication state. Copy them off-host through an authorized private
backup channel; same-disk snapshots do not survive disk loss.

Restore requires a **surviving current store** as the source of deletion barriers.
It creates a new private disabled directory, reapplies barriers, revokes all local
clients and OAuth clients/pending flows/codes/grants/tokens, and marks in-flight
CORTEX writes uncertain. It preserves acknowledged outbound receipts. It does not
copy host-specific configuration, credential files, or model artifacts.

## Explicit cutover

After checking the staged data and stopping every writer/supervisor:

```sh
dots-brain --data-dir /private/current activate-restore \
  --target /private/restored --writers-stopped
```

The command freezes the old store under its SQLite writer lock and durably disables
it. It merges the **latest** deletions and the surviving audit suffix, after verifying
the staged audit is an exact prefix. Original IDs and hash links are preserved.
It then compares canonical memories, revision history, audit and CORTEX operations. Divergence fails with content-free counts and
leaves both stores disabled. Reconcile the histories or stage a current backup;
memory/revision/CORTEX histories are not automatically merged or discarded.

Only after validation and durable commit is the restored copy enabled. The old
source remains disabled and cannot activate a different recovery identity, even
if a new target reuses the old path. No process is
started. Reprovision scoped credentials and reviewed host configuration, restore
verified model artifacts, point the supervisor to the new directory and verify it.
A failed cutover keeps the original data; do not delete either copy to fix failure.
`up --resume` cannot bypass either recovery marker. If the process dies after the
source is marked committed but before the target is enabled, repeat the same
`activate-restore` command with writers still stopped. It revalidates the same
persisted recovery UUID and paths before continuing. A recreated target has a new
UUID and is rejected; never edit markers to force a different successor.

## Rollback limits

Returning to a pre-migration v1 backup is an offline recovery operation using the
matching old package. It is lossless only if no newer writes, deletions, credentials
or external operations need preserving. Do not run old code against v2 or restore
old auth grants. Total loss of the current store/deletion history is a separate
recovery incident, not something this CLI can safely infer from an old snapshot.
An intentional uninstall still uses the ordinary `up --resume` reinstall path;
recovery markers require the explicit cutover above.
