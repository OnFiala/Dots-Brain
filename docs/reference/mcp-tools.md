# MCP tools

Dots Brain exposes a local MCP server over stdio or authenticated loopback HTTP.
Client grants are scoped by project and capability. Content and tool arguments are
untrusted input.

| Tool | Scope | Use |
| --- | --- | --- |
| `memory_context` | `memory:read` | Bounded relevant context for a task. |
| `memory_search` | `memory:read` | Search accessible memories. |
| `memory_get` | `memory:read` | Read a current or historical revision. |
| `memory_status` | `memory:read` | Read local memory and index status. |
| `memory_remember` | `memory:write` | Create or revise a source record. |
| `memory_forget` | `memory:forget` | Forget an exact reviewed revision and suppress reimport. |
| `audit_record` | `audit:write` | Append a sanitized client report. |
| `audit_events` | `audit:read` | Page sanitized audit events by event ID. |
| `audit_report` | `audit:read` | Read the standard coverage report. |

Use the same `event_id` on a retry. Revising an existing record requires its
`expected_revision`; forgetting also requires the reviewed revision. Source
identity since schema v2 is project, source, account, and event ID. That makes a
retry idempotent inside its project. Legacy v1 suppression hashes remain global.

Limits are enforced at the server: query text is at most 2,000 characters,
content 32,000 characters, title 300 characters, search `limit` 1–50, and
context `max_chars` 256–24,000. The HTTP MCP body is capped at 1 MiB.
Long full-text queries keep at most 32 unique terms (first 16 and last 16).
Search/context report `fulltext_query_limited` and `fulltext_query_terms` counts;
semantic retrieval receives the full query. See [using memory](../using-memory.md).

Hybrid search retains `semantic_score` when a full-text body excerpt replaces
a semantic title match. That excerpt has `passage.field="content"` and
`passage.source="fulltext"`; it has no character offsets because full-text
snippets may contain omitted text. Semantic passages include their source offsets.

Tool failures return structured error data with a stable `code`. Clients should
use that code for control flow and treat the message as operator context. Common
codes include `invalid_input`, `revision_conflict`, `source_suppressed`,
`forbidden`, `not_found`, `migration_required`, `capability_unavailable`, `busy`,
`credential_rejected`, `service_unavailable`, `timed_out`, `store_disabled`,
`invalid_state`, `integrity_error`, `database_error`, `permission_denied`, and
`internal_error`. See [troubleshooting](../troubleshooting.md) for safe next steps.
Bridge transport failures also include `request_too_large` and `outcome_unknown`;
see [bridge limits and recovery](cli.md#credentials-and-local-bridge).
An error code reports only this request's result; it does not prove or revoke an
external client configuration.

## Optional CORTEX tools

These tools appear only after a valid dedicated CORTEX configuration is loaded.
They do not copy CORTEX data into local memory automatically.

| Tool | Scope | Use |
| --- | --- | --- |
| `cortex_context` | `cortex:read` | Read bounded context from the mapped project. |
| `cortex_publish` | `cortex:write` and `memory:read` | Publish one exact local revision as a note, decision, or outcome. |
| `cortex_operation` | `cortex:read` | Read one local delivery receipt. |
| `cortex_operations` | `cortex:read` | List available operation receipts for a project. |
| `cortex_reconcile` | `cortex:write` | Look for a projected upstream receipt. |

Each publish starts with a local intent record and ends with an acknowledged
receipt only after the upstream response is stored. Unknown delivery remains
uncertain and needs reconciliation. A local-owner CLI retry is an explicit risk
decision for notes, decisions, and outcomes; decisions and outcomes can duplicate
because they have no upstream idempotency key. Recovering an orphaned `sending`
receipt only marks it uncertain while all cooperating writers are stopped; it does
not send or authorize a retry.

## CORTEX upstream contract

The configured endpoint must implement Streamable HTTP MCP with a dedicated
credential and the following tools. Local project names map one-to-one to explicit
upstream `project_id` values; the connector never guesses a project or exposes a
generic upstream call tool.

| Upstream tool | Request fields |
| --- | --- |
| `cortex_brief` | `project_id`, `query` |
| `cortex_search` | `project_id`, `query`, `limit` |
| `cortex_fetch` | `object_id` (reconciliation only) |
| `cortex_record_note` | `project_id`, `title`, `summary`, `content`, `topic`, `idempotency_key` |
| `cortex_record_decision` | `project_id`, `title`, `summary`, `doctrine_statement`, `doctrine_level` |
| `cortex_record_outcome` | `project_id`, `title`, `summary`, `status`, `topic` |

Responses must contain a JSON object in MCP `structuredContent` or a JSON text
content block. An MCP `isError` response is a failure, not proof that a write did
not commit. A write acknowledgement must contain a safe `event_id` or `object_id`;
only these identifiers are retained in the local receipt. The connector appends
its origin reference to note content or decision/outcome summary. Only notes send
the operation ID as `idempotency_key`; the upstream must honor it for retry safety.

Reconciliation searches for the operation ID, reads up to 20 `results` containing
`object_id`, then fetches a candidate object. Fetch must expose `project_id` and
`content_text` for notes or `summary_text` for decisions/outcomes, either directly
or inside `stored_object`. Matching project and origin text returns only
`candidate_unverified`: it does not acknowledge delivery or authorize a retry.
Missing tools or incompatible receipts fail explicitly. Endpoint configuration
alone does not establish that a real CORTEX service satisfies this contract.
