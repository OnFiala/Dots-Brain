# Uninstall and data retention

You can stop using Dots Brain without losing your memories. Ask your agent:

> Uninstall Dots Brain from this host, disconnect its managed clients, and keep
> my memories. Follow the repository's uninstall guide and verify the result.

The agent should run the steps below on the actual installation host, using its
existing data directory. This operation does not require copying a token into chat.
It does not uninstall Claude Code, Cursor, Codex, or another AI application.

## Choose the operation

| Operation | Result |
| --- | --- |
| `down` | Temporarily stops the managed background process. A local bridge can restart it. |
| `disconnect <provider>` | Removes one recorded client entry and revokes its dedicated local credential. |
| `uninstall` | Disables this memory instance, revokes all its local credentials, stops its managed process, and removes unchanged managed client entries. Memories remain. |
| Remove the Python package or source checkout | Removes program files after the service is disabled. This is a separate installation-specific step. |
| Erase personal data | Not part of normal uninstall. There is no automatic purge command in this alpha. |

## Preview and disable the instance

From the source checkout, use its existing executable. Substitute the actual data
directory; do not create a new store merely to remove an old one.

```sh
.venv/bin/dots-brain --data-dir /absolute/private/memory uninstall --dry-run
.venv/bin/dots-brain --data-dir /absolute/private/memory uninstall
.venv/bin/dots-brain --data-dir /absolute/private/memory doctor
```

The preview reads configuration without creating lock files, revoking credentials,
or stopping processes. `uninstall` returns JSON and can safely be repeated.
`doctor` should report `disabled`. Exit code 0 with `state: uninstalled` confirms
the service-removal steps completed for the discovered configurations. It does not
mean the source checkout or Python package has been erased: `software_removed`
remains false. `state: partial` exits 1 and names remaining issues.

Removal deletes only the exact `dots-brain` entry recorded by `connect`. Other
MCP servers and current application settings remain. TOML comments are preserved.
A modified, unreadable, or symbolic-link configuration is preserved and reported.
The agent must inspect that reported conflict before completing cleanup; never
restore a whole `.before-dots-brain` backup over current settings. Config files,
backups, and installer lock files can remain after their Brain entry is removed.

The disable marker prevents normal `up`, generated local bridges, and new `serve`
processes from reopening the instance. Running current-version MCP servers reject
new memory calls after disabling; a call already underway can finish. Only the
recorded background process is terminated. Close any separately launched stdio
client or manually supervised `serve` process too. External supervisors, older
binaries, and user-written scripts are outside the installer's process inventory.
Do not run an older release against a disabled data directory.

## Disconnect one client

```sh
.venv/bin/dots-brain --data-dir /absolute/private/memory disconnect cursor
```

Use `--config /actual/client/config.json` when that connection used a custom
location. Each newly configured location has its own credential, so disconnecting
one profile does not revoke another profile's access. `--dry-run` is also supported.

For a client provisioned with `--credential-file`, removal leaves that supplied
file alone and reports `issuer_revocation_required`. Revoke the reported client
ID on its canonical memory host with `client revoke <client-id>` before deleting
the supplied file. This alpha cannot revoke another host's credentials over MCP.

## Connections created by 0.2.0-alpha.1

Earlier installations did not record custom configuration locations. This release
discovers unchanged generated entries at supported default locations when run
with the same installation interpreter. Include each old custom path explicitly:

```sh
.venv/bin/dots-brain --data-dir /absolute/private/memory uninstall \
  --config cursor=/actual/profile/mcp.json \
  --config codex=/actual/profile/config.toml
```

The agent should obtain these paths from the existing installation/configuration
records. An arbitrary old custom path cannot be discovered reliably by guessing.
The result reports its discovery scope. Host-wide uninstall revokes every token
in this instance even if an old client configuration is not discoverable.

Older connections could share a credential across profiles. A single-client
disconnect preserves such a credential and reports `shared_credential_retained`
instead of silently disabling the other profiles. The owner can revoke it after
identifying its other users, or use whole-instance uninstall.

## Remove program files

After service removal, the agent identifies how this installation was installed:

- For a dedicated source checkout, verify that it contains no user changes or
  personal data before removing that checkout and its dedicated `.venv`. Preserve
  the canonical data directory outside it. Do not delete a shared workspace.
- For a package installed into an existing environment, use that environment's
  package manager, for example `uv pip uninstall --python /actual/env/bin/python
  dots-brain`. Do not delete the whole shared environment or its dependencies.
- If the onboarding skill was separately installed as a plugin, remove that
  plugin using its platform's supported controls. The CLI cannot unregister it.

The agent reports whether these program/plugin steps were possible and completed.
Do not claim full software removal from the CLI's service-removal result alone.

## What remains, and how to return

Normal uninstall preserves the SQLite database, live memories, revisions, deletion
suppression records, search indexes, models, service logs/state, and disable marker.
It removes known installer-owned credential files after revoking access. Unknown
credential copies and manually created files may remain, but their host-issued
tokens are revoked. Exports, backups, application history, and caches are untouched.

To reinstall, retain or restore the same supported code version and run the
bootstrap against the same data directory, or explicitly run:

```sh
.venv/bin/dots-brain --data-dir /absolute/private/memory up --resume
.venv/bin/dots-brain --data-dir /absolute/private/memory connect cursor
```

Ordinary `setup` does not remove the disable marker. The bootstrap is an explicit
installation action and does resume the instance. Reconnecting creates fresh
credentials; old revoked tokens stay invalid. Existing memories remain available.

If the user explicitly requests erasure, first finish service and software removal
and identify the exact private data directory and any separate exports/backups.
Deleting those files is an additional destructive operation, not an uninstall
default. Removing local files cannot erase copies in other tools or guarantee
physical media erasure. See [deletion and recovery](installation.md#export-deletion-and-recovery).
