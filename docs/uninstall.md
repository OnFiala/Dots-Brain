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

The complete disconnect ownership matrix, including preservation of modified
surroundings, backups and legacy-client guidance, belongs to the next batch.
Do not treat the current disconnect implementation as acceptance of that matrix.
A supplied remote credential stays under its issuer's revocation authority.
