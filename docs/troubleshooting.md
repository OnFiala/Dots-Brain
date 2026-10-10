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
