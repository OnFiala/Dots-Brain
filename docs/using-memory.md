# Using memory

Dots Brain stores explicit source records, revisions, and deletion barriers. It
does not infer a fact from an assistant conversation or provide provider history.

## Source identity

Since schema v2, a source is identified within a project by project, source,
account, and event ID. Retrying the same source identity is idempotent.
Suppression hashes created by schema v1 remain global because their original
project cannot be reconstructed. The current database schema is v4.

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
blocks, known token prefixes, literal sensitive fields, authentication headers,
credential arguments and `user:password@host` URLs. Whole placeholders and code
references are retained. Uncertain credential-shaped text is accepted unchanged
with `warnings: [{"code": "possible_secret", "category": "…"}]` in the write
response, including an idempotent replay. Review or replace that text before
sharing it. Optional CORTEX operations return the same warning metadata.

Capture and audit minimization redact both certain and uncertain matches. This
does not change the stored memory text or revision identity. The filter is
heuristic: unknown formats, unlabeled opaque values and intentionally disguised
secrets can escape detection. It is not a secret vault and cannot prove that all
sensitive material has been detected.

## Full-text search and context bounds

Text queries are NFC-normalized and tokenized before lowercasing. They retain
short words and identifiers. Up to 32 unique terms are used; longer tasks keep
the first 16 and last 16 terms in encounter order. Terms in the middle can be
omitted, so narrow the query when a specific middle term matters. Search and
context return `fulltext_query_limited` plus `fulltext_query_terms` with `total`,
`used` and `omitted` counts. Semantic search receives the complete query.

Context rechecks each referenced revision and project. A title match expands to
body text before the space check. A snippet is omitted when fewer than 64
characters fit, unless its complete excerpt is shorter. `omitted_memories` and
`truncated_excerpts` describe the result; the budget is characters, not tokens.

## Local semantic retrieval

Semantic retrieval is optional and uses the pinned local CPU model. An index run
does not write while `disabled.json` is present. Failed embeddings remain
visible as `failed`. Per-record retries back off from 30 seconds to one hour;
automatic retries are limited to one failed record per five seconds across the
instance, with new source work preferred. A backlog can delay retries further.
This timing state resets on process restart. After fixing the cause, the explicit
`index --retry-failed` command clears failure state and those delays.

Status aggregates counts, dimensions, byte lengths and missing chunks in SQL;
it does not load every vector into Python or check numeric finiteness. Search and
repair reject NaN/Inf and malformed vectors. Thus a structurally valid corrupt
vector may be counted as indexed until those paths discover it.
Missing or structurally malformed derived vectors are reported as pending repair
and rebuilt by a bounded repair pass; source memories are
unchanged. Each background pass uses a bounded query for new source revisions
and checks at most four already-indexed memories for vector repair, so it does
not validate every vector on each polling interval. The CLI drains a complete
repair pass before exiting, even when the first slice contains no work.

The pending-source query still examines the source set when all records are
indexed. Bounded result size is not a constant-time guarantee; see the
[stress measurements](stress-tests.md).

The fixed semantic score floor remains 0.20. The public `neutral-objects-v1`
regression fixture contains 24 imaginary objects in Czech and English and 11
query forms, or 264 relevant queries. It is repository-authored synthetic data,
not an independent held-out evaluation. Its reviewed SHA-256 is pinned by
`tests/test_calibration_fixture.py`.

On a MacBook Pro, macOS 26.6.2 arm64, Python 3.11.14 and the pinned local CPU
model, both `555f615` and the candidate after `800eeda` measured **232/264
recall at ten (87.9%)**. Both returned a semantic candidate for **24/24
out-of-domain questions**. These are retrieval counts, not latency or memory
measurements. This fixture replacement does not improve ranking. The current
floor provides no useful rejection of those unrelated questions; verify source
relevance before using a result. Short cross-language retrieval remains a known
limitation.

Reproduce with a locally prepared model (the test never downloads one):

```sh
BRAIN_TEST_MODEL_DIR=/path/to/prepared/model uv run pytest -q tests/test_semantic.py -k calibration --junitxml=calibration.xml -o junit_family=xunit1
```

The XML records recall and unrelated-candidate counts. The test enforces a broad
80% retrieval regression floor; it makes no out-of-domain accuracy guarantee.

### Independent threshold evaluation

On the same machine and pinned model, a separate verifier sealed 24 neutral
records, 144 relevant queries and 30 unrelated queries before seeing the proposed
policy. A separately sealed supplement tested 24 one-word cross-language queries
and 24 unrelated words. The candidate used cosine floors of 0.15 for one or two
words, 0.30 for three to five words, and 0.45 for longer queries.

| Dataset | `555f615` relevant at ten | Candidate relevant at ten | `555f615` unrelated queries returning a candidate | Candidate unrelated queries returning a candidate |
| --- | --- | --- | --- | --- |
| Short, medium and long queries | 141/144 | 137/144 | 30/30 | 14/30 |
| One-word cross-language queries | 17/24 | 17/24 | 24/24 | 24/24 |

The candidate passed the original dataset's predeclared margins, but failed the
supplement's maximum 50% unrelated-candidate rate. It was **not adopted**. The
current floor remains 0.20 and the one-word retrieval issue remains open. No
queries, expected answers or acceptance margins were changed after measurement.
The valid run verified all 24 records indexed before querying. An earlier attempt
with only 16 indexed records was invalidated and excluded. These finite synthetic
results do not establish production retrieval quality.
