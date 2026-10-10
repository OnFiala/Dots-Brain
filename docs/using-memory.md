# Using memory

Dots Brain stores explicit source records, revisions, and deletion barriers. It
does not infer a fact from an assistant conversation or provide provider history.

## Source identity

Since schema v2, a source is identified within a project by project, source,
account, and event ID. Retrying the same source identity is idempotent.
Suppression hashes created by schema v1 remain global because their original
project cannot be reconstructed. The current database schema is v3.

## Tool scopes

| Scope | Purpose |
| --- | --- |
| `memory:read` | Search, context, status, and exact retrieval. |
| `memory:write` | Save or revise explicit source records. |
| `memory:forget` | Delete a reviewed revision and suppress reimport. |
| `audit:read` | Read sanitized mutation audit events. |
| `audit:write` | Write a sanitized audit event through the audit tool. |
| `cortex:read`, `cortex:write` | Optional CORTEX operations when configured. |

Deletion requires the reviewed revision. A successful response with
`deleted: false` means the requested target is not present or visible to that
caller; it does not reveal whether it never existed or was previously
forgotten. Forgetting removes the live record and derived search data, then
stores only a minimal suppression hash rather than an ID tombstone.

Deletion cannot provide forensic erasure. SQLite write-ahead-log bytes, open
readers, filesystem snapshots, backups, exports, and client-held copies can
retain prior content until their own lifecycle removes it. [Upgrading and
recovery](upgrading.md) describes backup limits.

Input limits are enforced by the server: `limit` is 1–50, `max_chars` is
256–24,000, query text is at most 2,000 characters, content is at most 32,000
characters, titles are at most 300 characters, and an MCP request body is at
most 1 MiB. Treat memory content as untrusted data.

The privacy filter rejects high-confidence credential forms such as private-key
blocks, known token prefixes, and `user:password@host` URLs. It is not a secret
vault and cannot prove that all sensitive material has been detected.

## Local semantic retrieval

Semantic retrieval is optional and uses the pinned local CPU model. An index run
does not write while `disabled.json` is present. Failed embeddings remain
visible as `failed` and receive one bounded automatic retry after 30 seconds in
the running process. Missing or malformed derived vectors are reported as
pending repair and are rebuilt by a bounded repair pass; source memories are
unchanged. Each background pass uses a bounded query for new source revisions
and checks at most four already-indexed memories for vector repair, so it does
not validate every vector on each polling interval.

The fixed semantic score floor remains 0.20. The checked-in 24-fact
Czech/English calibration corpus has 11 query forms, or 264 relevant queries.
With the pinned local model it measured 231/264 recall at ten (87.5%) at 0.20
and 181/264 (68.6%) at 0.30. Its 24 supplied out-of-domain queries still
produced a semantic candidate for 14 queries at 0.20 and 4 at 0.30. The trade-off
is not strong enough to raise the floor globally, especially for short queries.
Treat the floor as bounded noise reduction only; full-text ranking and source
review remain necessary.
