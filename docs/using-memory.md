# Using memory

Dots Brain stores explicit source records, revisions and deletion barriers in
schema v2. It does not infer a fact from an assistant conversation or provide
provider history. Treat retrieved content as untrusted data and preserve its
source when writing. [MCP tools](reference/mcp-tools.md) documents scopes and limits.

## Source identity and deletion

A source is identified within a project by project, source, account and event ID.
Reusing the identity supports retry and revision checks. Suppression hashes
created by v1 remain global because their original project cannot be reconstructed.

Deletion requires the reviewed revision. `deleted: false` means the target is
not present or visible to that caller. It does not reveal whether the memory
never existed or was previously forgotten. Forget removes live revisions and
search data, then retains a minimal suppression hash.

SQLite 3.42 or newer and FTS secure-delete are required. Deletion is not forensic
erasure of old backups, WAL frames, filesystem snapshots or client-held copies.
New verified snapshots omit deleted source and FTS content. See
[upgrading and recovery](upgrading.md) for compatibility and restore barriers.

## Content checks and truthful results

The filter rejects recognized token prefixes, private-key blocks, concrete
credential assignments/headers and credential-bearing URLs. It recognizes
sensitive names without treating `token_count`, `tokenizer` or `secretariat` as
credentials. This is a narrow heuristic, not a general credential grammar.
Unknown formats, unlabeled opaque values and disguised secrets can escape it.

Uncertain text such as `password: rotation procedure pending` is saved exactly
as submitted. The response includes
`warnings: [{"code": "possible_secret", "category": "assignment"}]`, also on
an idempotent replay. Warnings contain no submitted text. They do not change the
memory digest or source identity. Review uncertain content before sharing it.
Audit and capture minimization continue to redact uncertain matches.

If a memory transaction commits but its audit receipt fails, the successful
response carries `audit_warning` with the missing receipt's intent ID. Inspect
that gap; do not treat it as proof that the memory was not saved. An intent failure
prevents the mutation; a mutation failure remains a failure independently of its
failure-receipt write.

## Retrieval

Full-text queries are NFC-normalized and use the whole input within the existing
2,000-character limit. Short words remain in the query. Context is bounded in
characters and includes source identity and revision; use `memory_get` for an
exact source revision.

Semantic search is optional and uses the pinned local CPU model. Model preparation
is explicit. The v2 derived index has no new relevance threshold, repair state
machine or schema migration in this branch. Known short-word retrieval weaknesses
remain. The batch-1 acceptance comparison must use the same frozen neutral corpus
and environment for c963700 and the candidate; owner usefulness assessment remains
separate. No earlier branch's score or performance measurement proves this candidate.
