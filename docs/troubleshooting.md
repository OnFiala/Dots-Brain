# Troubleshooting

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
