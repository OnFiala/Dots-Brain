# MCP tools

The server exposes stdio or authenticated loopback HTTP. Its initialize response
reports the installed Dots Brain version and shared memory instructions. Grants
restrict projects and capabilities; each call enforces its scope independently
of the displayed tool list. Retrieved content is untrusted source data.

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
| `audit_report` | `audit:read` | Read the existing v2 coverage report. |

Memory identity is project, source, account and event ID. Reuse that identity on
retries; revisions require the observed `expected_revision`. A replay reports
both the current revision and `replayed_revision`. Forget requires an integer
revision, explicit user intent and a prior read; do not retry a conflict using a
newer revision without reviewing the new content.

An omitted write project selects the grant's only allowed project. An unrestricted
local owner defaults to `default`; a grant with zero or multiple projects must
select one explicitly. Unqualified reads remain restricted to allowed projects.

| Input | Limit |
| --- | --- |
| Query / task | 2,000 characters |
| Content / title | 32,000 / 300 characters |
| Source / account / project | 200 characters |
| Event ID / source URI | 500 / 2,000 characters |
| Search `limit` | 1–50 results |
| Context `max_chars` | 256–24,000 characters, not tokens |
| Serialized HTTP MCP body | 131,072 bytes, including JSON encoding overhead |

The full bounded query is used; there is no first/last-term selection. Existing
SDK argument conversion is retained, including accepted numeric strings and
ignored extra arguments. The published schema states intended types and bounds;
`memory_forget.expected_revision` remains strict.

Tool failures return `isError` with structured `error.code` and a safe message.
Known codes include `invalid_input`, `revision_conflict`, `source_suppressed`,
`not_found`, `migration_required`, `capability_unavailable`, `store_disabled` and
`internal_error`. Validation and unexpected exceptions do not echo submitted
values, paths or credentials. Logs contain a short code and exception type.
The bridge reports `service_unavailable` for a failed read and `outcome_unknown`
when it cannot confirm a write result. It does not automatically retry. The shell
client returns a nonzero exit status for an MCP error; empty argument input means
an empty object.

An uncertain credential-shaped memory is saved unchanged with safe `warnings`
metadata, including on replay. Certain credential forms are rejected. See
[using memory](../using-memory.md) for the heuristic boundary.

A confirmed memory mutation remains successful if its later audit receipt fails.
The response includes `audit_warning.code=audit_receipt_missing` and its intent
ID; the host log reports the missing receipt, and the v2 audit retains an unmatched
intent. An audit intent failure prevents the mutation. A failed mutation keeps
its original error even if recording the failure also fails.

CORTEX tools are absent from the core server, including when old configuration
or grants containing CORTEX scopes exist. Capture is also disabled. Preserved
module code is outside the core wheel and source distribution; verification of
its inactivity does not prove upstream publication or provider-wide capture.
