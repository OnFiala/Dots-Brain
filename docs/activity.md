# Activity and capture

The mutation audit records operations observed by this service. It does not record
everything an assistant says, reads, or does outside Dots Brain.

`capture` imports one bounded JSONL snapshot after explicit configuration. It is
not continuous provider capture and does not establish complete history. Input
from a provider is untrusted and must not be treated as operational instruction.

Audit and capture scopes are separate from memory read and write scopes. An audit
review can analyze only data that was actually collected. Keep audit exports and
operator reports private because they can expose local paths or metadata.

Use `audit events` to page after an event ID and `audit report` for the standard
coverage summary. New v3 audit hashes bind the event ID and `recorded_at`; v2
history is still verified with its original, unkeyed format. A private review
target describes only host, user, executable, data directory, origin and timezone.
It starts in review state. Review every progressive chunk before checkpointing it;
do not put production memory content in the target.

Capture never imports system instructions or analysis/reasoning payloads. If a
terminally quarantined pending receipt blocks a deliberate retry, use
`capture --recover-pending` after inspecting the local result.

## Experimental Grok snapshot format

The current adapter supports the supplied Grok export shape below. It is not a
generic JSONL parser and has not been accepted against a live producer. Use UTF-8,
one JSON object per line, and keep the cursor when the same source grows.

| Mode | Accepted shape | Captured data |
| --- | --- | --- |
| `audit` | `type`, optional `ts` and `agentId` | Sanitized action metadata. |
| `transcript` | `role`, `message.content` block list | User text and tool names/status. |

Audit `type` is one of `shell_command` (`command`, `shellKind`, `target`),
`mcp_tool_call` (`serverIdentifier`, `toolName`, `toolCallId`, `status`,
`durationMs`), `browser_navigation` (`url`, `pageTitle`), or
`computer_use_session` (`actionCount`, `durationMs`, `screenshotCount`).

```json
{"type":"mcp_tool_call","ts":"2026-01-01T12:00:00Z","toolName":"example","status":"success"}
{"role":"user","message":{"content":[{"type":"text","text":"Synthetic project note."}]}}
```

The first example belongs to an audit file, the second to a transcript file.
Assistant `tool_use` blocks contribute only `name`; tool `tool_result` blocks
contribute `name` and a boolean `result.success` when present. Tool arguments and
results are omitted. Plain assistant text has no verified visibility flag in this
format and becomes a coverage gap. Records have no reliable transcript call IDs,
so tool pairing remains unverified. Source timestamps and actor fields remain
untrusted snapshot metadata, separate from the authenticated collector identity.

A pass is capped at 1,000 records or 16 MiB, with a 256 KiB line bound. Resume when
`more_pending` is true. Acknowledged cursor updates survive interruption; source
rotation creates a new generation and a reported gap. Renaming a source can
change its identity; copying the same snapshot under another path is not a
supported deduplication mechanism.
