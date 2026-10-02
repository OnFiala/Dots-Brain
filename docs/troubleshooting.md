# Troubleshooting

Ask your agent to diagnose the existing instance using `preflight`, `doctor`, the
command's JSON result, and the client's supported health check. Use the actual
memory host and data directory. Never paste a connection file, bearer token, or
private memory export into an issue or conversation.

| Symptom or result | Meaning and next step |
| --- | --- |
| `setup_required` | No database exists at the selected directory. Locate the existing canonical directory before creating a new instance. |
| `disabled` | The instance was uninstalled. Only an intentional reinstall should use `up --resume` or the bootstrap. |
| `blocked` for a web or unknown provider | There is no tested adapter/public ingress/OAuth flow. This is a missing feature, not a token-copying task. |
| Local port is occupied | Stop or identify the specific competing process. Use `up --port 0` for a new free port and reconnect affected local clients. Do not kill arbitrary processes. |
| Readiness or bridge verification fails | Check that the exact interpreter exists, dependencies match the release, the credential is private and valid, and the loopback service is running. Use `up` and then `connect` again. |
| Another installation is running | Let its bounded operation finish, then retry the same command. Lock files may remain on disk without an active lock; deleting them is not a recovery step. |
| A different Dots Brain entry already exists | Inspect its host and paths without exposing credentials. Remove or disconnect the known obsolete entry before connecting again. Other settings are preserved. |
| Existing client permissions differ | The command refuses to silently broaden access. Disconnect a dedicated local connection, then reconnect with the explicitly intended projects. |
| `partial` during removal | Service disabling may have succeeded while configuration cleanup or remote revocation remains. Read `clients` and `issues`, resolve those exact items, and rerun. |
| HTTP 401 | The credential is missing, expired, revoked, or its host is disabled. Reconnect locally under the host owner's authority; remote clients need the issuer to provision access. |
| Bridge verifies but the application cannot see tools | Reload its MCP configuration and check its own status. SDK verification does not establish activation in an existing conversation. Keep required application permissions enabled. |
| A laptop cannot reach `127.0.0.1` on the VM | Loopback means the current machine. Public HTTPS ingress, secure credential delivery, and web OAuth are not automated in this release. Do not create a second memory as a workaround. |
| Semantic search is unavailable | Install the semantic extra and run `model prepare`, then start with `--semantic`. Only explicit model preparation downloads weights; there is no paid API fallback. |
| Newly saved text is not in semantic results yet | Indexing is asynchronous. Check the semantic backlog with `memory_status`; full-text retrieval remains available. |
| The VM stopped or was replaced | This alpha provides reconnect recovery, not guaranteed boot supervision or persistent VM storage. Check the actual platform lifecycle and your external recovery copy. |

`service.log` and `service.json` reside in the private data directory. Logs can
contain local paths or diagnostic details; inspect and redact before sharing.
Connection credentials are stored separately and must never be pasted into a
diagnostic transcript. File permissions do not isolate secrets from the OS owner.

For a reproducible bug, report the release, Python/OS versions, transport, sanitized
command result, and a synthetic reproduction in
[GitHub issues](https://github.com/OnFiala/Dots-Brain/issues). Include whether the
problem is a generated bridge failure or an actual application connection failure.
See [verification](verification.md) for what has already been tested.
