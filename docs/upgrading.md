# Upgrading an installation

Keep the same canonical data directory. Read the target release's changelog and
use its immutable GitHub tag; do not treat a moving development branch as a release.
An agent can do these steps within an already authorized installation request.

1. Record the installed version, actual interpreter, data directory, connected
   client paths, and whether semantic search is enabled. Do not print credentials.
2. Review user changes in the checkout before updating it. Never reset or overwrite
   them to force an upgrade.
3. Close or pause connected clients, stop the managed service with `down`, and
   stop any separately supervised processes. Client bridges can restart a stopped
   service, so they must stay closed during backup and dependency replacement.
4. If a recovery copy is needed, make an access-restricted filesystem copy of the
   complete data directory while all writers are stopped. Preserve SQLite journal
   files if present. A JSONL export is not a complete database backup. Backup/restore
   automation and deletion-aware restore are not implemented.
5. Update the existing checkout to the chosen release, preserving its path, then
   run `uv sync --frozen` (add `--extra semantic` if used). Keeping the interpreter
   path stable preserves generated client entries. For a packaged installation,
   install the target package in the same existing environment.
6. Run `up` with the original data directory and runtime options, then `doctor`.
   Repeat `connect <provider>` with each actual custom `--config` path when needed.
   Verify the bridge and the application's own health check before reporting success.

For 0.2.0-alpha.1 to 0.2.0-alpha.2, the SQLite schema is unchanged. The newer
release adds an integration registry and per-configuration credentials; it
recognizes unchanged alpha.1 generated entries and can retain their existing
credential. Re-running `connect` registers a legacy custom path for later removal.

The OAuth milestone adds optional `oauth_*` tables to the same database only when
`oauth configure` is run. Existing memories and local credentials are preserved;
an upgrade does not turn on OAuth or publish a public endpoint automatically.
Run `oauth disable` with an OAuth-aware release before downgrading: older removal
commands cannot clear OAuth grants they do not know about. A complete private
database backup contains confidential-client registration secrets as well as memory.

An intentionally uninstalled instance stays disabled during ordinary `up`. Use
`up --resume` or the bootstrap only when the user intends to reinstall. Updating
files alone should not silently reactivate a removed integration.

If the interpreter or checkout moved, the old client entry points to its old path.
Remove that recorded connection with the old installation before connecting from
the new path. A conflicting entry is preserved rather than automatically replaced.
See [troubleshooting](troubleshooting.md) and [uninstall](uninstall.md).

## Failed upgrade or rollback

Stop the new service and inspect the specific error. Never delete the database to
fix startup. Use a compatible release for the schema; future schema migrations
may prevent an older binary from opening a newer database. No general automated
rollback is provided in this alpha.

Reverting files to alpha.1 would also remove enforcement of the new disable marker.
Do not use an old binary to operate an uninstalled instance. Restoring an old
database can restore revoked credentials or forgotten content; recovery requires
reapplying revocations and deletions. Keep backups private and do not claim a
restore is deletion-safe until its contents have been reconciled.
