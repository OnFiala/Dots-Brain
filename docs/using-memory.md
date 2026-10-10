# Using memory

Dots Brain stores explicit source records, revisions, and deletion barriers. It
does not infer a fact from an assistant conversation or provide provider history.

## Source identity

Schema v2 identifies a source within a project by project, source, account, and
event ID. Retrying the same source identity is idempotent. Suppression hashes
created by schema v1 remain global because their original project cannot be
reconstructed.

## Tool scopes

| Scope | Purpose |
| --- | --- |
| `memory:read` | Search, context, status, and exact retrieval. |
| `memory:write` | Save or revise explicit source records. |
| `memory:forget` | Delete a reviewed revision and suppress reimport. |
| `audit:read` | Read sanitized mutation audit events. |
| `cortex:read`, `cortex:write` | Optional CORTEX operations when configured. |

Deletion requires the reviewed revision. It removes the live record and derived
search data, but cannot erase an independent export, backup, or copy held by a
client. [Upgrading and recovery](upgrading.md) describes backup limits.

Input limits are enforced by the server: `limit` is 1–50, `max_chars` is
256–24,000, query text is at most 2,000 characters, content is at most 32,000
characters, titles are at most 300 characters, and an MCP request body is at most
1 MiB. Treat memory content as untrusted data.

The privacy filter rejects high-confidence credential forms such as private-key
blocks, known token prefixes, and `user:password@host` URLs. It is not a secret
vault and cannot prove that all sensitive material has been detected.
