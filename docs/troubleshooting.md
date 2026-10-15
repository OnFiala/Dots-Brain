# Troubleshooting

Use the actual host, interpreter and canonical data directory. Keep credentials,
databases and private exports out of issues and conversations.

| Observation | Next step |
| --- | --- |
| `migration_required` | Stop supported writers and use the explicit v1 upgrade in [recovery](upgrading.md). |
| `store_disabled` | Inspect the recovery marker. Only ordinary uninstall permits `up --resume`; restore requires its exact activation procedure. |
| `invalid_input` | Read the command's specific message and `--help`; preserve rejected state. |
| `operation_failed` | Check local permissions, connectivity and arguments without printing secrets. |
| Busy installation/writer lock | Stop writers through their existing supervisor or client before offline maintenance. Never delete a held lock file. |
| External HTTP service | Use its explicit private `--mcp-url`; do not start another server or ask `down` to stop it. |
| `doctor` MCP `not_verified` | Supply the existing private URL and matching `--credential-file`. Local database health alone is not connection proof. |
| `endpoint_or_credential_failed` | Check the chosen store, endpoint and scoped credential; a credential from another store cannot establish local readiness. |
| `public_oauth_listener_missing` / `public_oauth_listener_unverified` | Inspect the intended private/public topology and prove both sockets; see [OAuth](oauth.md). |
| OAuth pairing waits | Inspect `oauth pending`, verify the displayed callback/scopes and approve the exact initiated request on the host. |
| Client tools absent | Reload the actual client and verify a tool call; bridge verification is a separate result. |
| `outcome_unknown` | Inspect the source identity/audit before a deliberate retry. A lost response may follow a committed write. |
| `audit_warning` | The memory operation committed; inspect audit availability without blindly repeating the mutation. |
| `partial` | Resolve the named issues; do not report complete success. |
| `Output cleanup incomplete` warning | A private credential or partial export may remain at the requested output path. Inspect it privately and resolve permissions before retrying; the original operation still failed. |

`doctor` is read only and reports independent checks. Corrupt or unbound
`service.json` is preserved. Do not remove it to force startup. Establish the
process owner and data path through the actual operator/supervisor first.

After an interrupted restore, keep both directories and retry only the exact
validated source/target recovery described in [upgrading](upgrading.md). This v2
release has no generic cancellation command and cannot restore total host loss
without the surviving deletion history. A code rollback does not undo data,
revocations, deletion barriers or the FTS format.

A credential file can outlive a failed publication or revoked grant. Its
existence does not prove access. Verify through MCP or provision an explicitly
authorized replacement at a fresh private path. Capture and review automation
have separate remaining work; see [activity](activity.md).
