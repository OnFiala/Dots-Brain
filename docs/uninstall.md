# Uninstall and data retention

`uninstall` is offline maintenance: first stop the managed service with `down`,
or stop HTTP/stdio writers through their own supervisor/client. An active writer
lease rejects maintenance before data changes. A preview can run while serving.

```sh
dots-brain --data-dir /absolute/private/memory uninstall --dry-run
dots-brain --data-dir /absolute/private/memory uninstall
dots-brain --data-dir /absolute/private/memory doctor
```

Uninstall disables the instance, revokes access and detaches unchanged managed
entries. It keeps the database, deletion history, exports and backups. OAuth
client/grant provenance is retained after invalidation. An unbound process state
is preserved and reported as an issue; it is never signalled.

`doctor` returns `state: "disabled"` for a disabled installation. A partial
uninstall names unresolved configuration/revocation issues. `down` controls only
a process whose saved identity and canonical data directory match; an external
HTTP service remains running and is reported separately. There is no automatic
process scan or forced supervisor shutdown.

An ordinary uninstall may be resumed explicitly with `up --resume`. Recovery
markers require the [restore procedure](upgrading.md). Removing code, erasing
data or deleting backups is a separate action.

## Disconnect one client

```sh
dots-brain --data-dir /absolute/private/memory disconnect codex --dry-run
dots-brain --data-dir /absolute/private/memory disconnect codex
```

Close the client and configuration editor while connecting or disconnecting.
Dots Brain's advisory lock serializes its own operations; another editor does
not participate. A detected concurrent write is preserved and must be retried
after that writer stops. Last-moment checks cannot exclude every external race.

Connect records ownership in the existing integration registry. Disconnect
compares its own entry: an unchanged owned entry is removed and its unique owned
local credential revoked even when other settings or comments have changed.
An edited entry and its access are preserved, with a partial result. Unknown old
ownership is not inferred from a matching command or filename.

| Artifact | Cleanup rule |
| --- | --- |
| Existing configuration | Remove only the owned entry; preserve other settings, TOML comments, UTF-8 and file ownership/mode. |
| Configuration created by connect | Delete only when unchanged and empty after removal; retain user additions. |
| Backup created by connect | Delete only when its bytes and the whole configuration around the entry remain unchanged; otherwise report its retained path. |
| Pre-existing or changed backup | Preserve. A whole-file backup is never restored over current settings. |
| Local credential created by connect | Revoke only its recorded access; remove the file only with unchanged ownership evidence. A changed file is preserved. |
| Supplied remote credential | Preserve the file; report `issuer_revocation_required`. Local entry removal does not prove remote revocation. |
| Created directories | Remove only empty directories. Persistent advisory lock files and nonempty directories remain. |

Results distinguish entry cleanup, access revocation, credential-file cleanup
and directory retention. A revocation failure keeps the registration for retry.
Uninstall additionally disables the whole local instance and invalidates its
local access, even when an edited client configuration must be preserved.
The diagnostic `probe.connection.json` has no recorded file-ownership digest;
uninstall reports it as retained if present. Its local token is covered by the
reported bulk revocation. A retained file is not proof of active access.

Unregistered 0.2 client entries are reported with their actual configuration
path. Manually remove only `mcp_servers.dots-brain` (Codex) or
`mcpServers.dots-brain` (JSON), preserve other settings and the old credential,
then run the reported connect command. There is no automatic adoption.
