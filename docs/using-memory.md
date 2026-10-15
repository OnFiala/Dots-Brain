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
Audit minimization continues to redact uncertain matches. Snapshot capture is
disabled; its retained source is described in [activity](activity.md).

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

A title match locates a record; its search excerpt contains the body of that
revision. Hybrid search keeps the full-text body excerpt when the semantic winner
is the title. `match_field` identifies the semantic winning field separately
from the returned `passage`. Context rechecks access to the exact revision and
omits deleted records; a concurrent update does not mix a new body into an old
revision. A bounded title in the context header helps identify the source.

Semantic search is optional and uses the pinned local CPU model. Model preparation
is explicit. There is no relevance threshold that guarantees unrelated queries
return no results. Known short-word retrieval weaknesses remain. Compare versions
using the same frozen neutral corpus, pinned model and environment. Check exact
source, revision and body passage as well as result rank; the owner's usefulness
assessment is a separate requirement. See [performance checks](stress-tests.md).

If semantic search fails, search returns full-text results with
`semantic: degraded`; it does not expose the underlying exception. Indexing reports
`indexed_now` for the current call and `indexed` for the total current index.
Committing an indexed revision replaces that memory's old model rows. Disabled
stores reject an index commit even if embedding started before the barrier.

The HTTP bridge creates a separate authenticated MCP session for each call and
ignores proxy settings from the environment. It never automatically retries a
failed call. Loss of a write response produces `outcome_unknown`: the write may
have committed. Check the source identity and revision before deciding to retry.
Default connection verification reads tool discovery and `memory_status` only;
an explicit `verify --write` is a separate, mutating synthetic probe.
