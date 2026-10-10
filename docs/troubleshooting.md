# Troubleshooting

If credential creation was interrupted after file publication, an output file
can remain without a matching active client. Verify it through MCP; do not treat
its presence as access. Preserve the file for inspection and create a replacement
at a new private path. Existing credential files are never overwritten silently.

Use the actual host and data directory. Never paste a bearer token, connection
file, database, or private export into an issue or conversation.

| Result | Next step |
| --- | --- |
| `setup_required` | Locate the canonical directory before creating a store. |
| `disabled` | Reinstall only intentionally with `up --resume` or bootstrap. |
| Loopback connection fails | Check the exact interpreter, service state, and credential file. |
| Client tools are absent | Reload the client configuration; bridge success is not UI activation. |
| OAuth flow waits | Inspect and approve only the exact pending request from that flow. |
| `partial` | Resolve each reported issue and rerun; do not declare success. |
| `capture_partial` or `capture_recovery_required` | Keep the cursor and source file. Inspect the reported gap or pending receipt before an explicit recovery or new cursor. |
| `migration_required` | Stop writers, make a full backup, and follow the offline upgrade procedure. |
| `capability_unavailable` during backup or export | Use a local POSIX destination that supports hard links. |
| `credential_rejected` | Replace the credential through the owner flow; it may be expired, revoked, or tied to a disabled installation. |
| Restore validation fails | Preserve both stores and follow the recovery result. |

`preflight` and `doctor` are read-only starting points. A remote device, public
ingress, or missing external supervisor needs separate access and evidence.

A corrupt `service.json` is preserved. Do not delete it and retry startup: first
identify the running process, its interpreter and its data path. The tool does
not start a replacement when that identity is unknown.

For `external_writers_remain`, stop the named supervisor or client. A lease error
includes its filename and, on Linux when visible, its owner PIDs. Do not delete
a held lock file; that would allow a second process to acquire a different lock.

After an interrupted restore, `abort-restore` can cancel a pending cutover even
when its staged target was removed. If the source was also uninstalled, abort
keeps it disabled. A committed cutover cannot be cancelled.
