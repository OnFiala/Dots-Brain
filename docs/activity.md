# Activity and capture

The mutation audit records operations observed by this service. It does not record
everything an assistant says, reads, or does outside Dots Brain.

`capture` imports one bounded JSONL snapshot after explicit configuration. It is
not continuous provider capture and does not establish complete history. Input
from a provider is untrusted and must not be treated as operational instruction.

Audit and capture scopes are separate from memory read and write scopes. An audit
review can analyze only data that was actually collected. Keep audit exports and
operator reports private because they can expose local paths or metadata.

Sanitization detects known credential patterns; it cannot identify every secret.
It limits text before pattern matching and reports omitted content. Oversized JSON
keys and their values are omitted together, since a shortened key cannot safely
identify a credential field. Memory writes reject truncation; capture reports it.

Use `audit events` to page after an event ID and `audit report` for the standard
coverage summary. New v3 audit hashes bind the event ID and `recorded_at`; v2
history is still verified with its original, unkeyed format. A private review
target describes only host, user, executable, data directory, origin and timezone.
It starts in review state. Review every progressive chunk before checkpointing it;
do not put production memory content in the target.

The private review helper requires a compatible host CLI with
`audit events --review-projection`. This versioned read format verifies each full
original row on the owner host. Rows above 256 KiB in the CLI serialization become
bounded identity, pairing, timestamp and hash metadata, with an `oversized_event`
finding. The helper verifies their chain links and projection checksum. Its output
explicitly says `verified_by_owner_cli` and `content_reviewed: false`: omitted
content was not analyzed locally. Ordinary rows are also verified locally in full.
The original audit is unchanged. These are unkeyed integrity checks; the fixed
CLI over authenticated SSH is trusted to verify omitted content.

Pages are at most 1 MiB. The host processes one original row at a time; verifying
an unusually large historical row still needs memory and time proportional to
that row. Unknown formats, invalid hashes and unavailable helpers stop the scan
without moving its checkpoint. Confirm the compatible executable on the memory
host before using the new helper; upgrading local source alone does not upgrade
that host.

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
`batch_limit_reached` is true. Acknowledged cursor updates survive interruption.
An atomic replacement continues only when every acknowledged byte is unchanged.
If a replacement, truncation, or rewrite changes an acknowledged prefix, capture
records a durable gap and blocks that source under the current cursor. Review the
export and start an explicitly chosen new cursor before importing it again.

## Cursor and review state

Capture cursors use `format_version: 1`. A validated unversioned or explicit v0
cursor upgrades atomically, preserving source keys, acknowledged offsets,
generations, flags, sampled fingerprints and existing prefix chains. Missing
chain evidence is never reconstructed from mutable source bytes. A legacy cursor
without a full chain can continue an append on the same inode; replacement of
that inode requires an explicit operator reset.

Unknown versions and malformed checkpoints stop before delivery or replacement
of the cursor. The error names a read-only `python3 -m json.tool` command for the
actual cursor path. Preserve the source and cursor; restore a known-good cursor
backup or use a compatible release. Do not delete the cursor to bypass recovery.

The separate review helper writes `format_version: 1` to pending reviews and
completed checkpoints. Unversioned/v0 state remains readable, including legacy
unresolved receipt keys. A checkpoint upgrades only after explicit completion of
the matching review; its prior on-disk bytes still bind the scan's base digest.
A compatible legacy pending review can be completed without changing its evidence
digest. Unknown or malformed state remains untouched. Its recovery command starts
a separate full review with the same target file and preserves the original state;
review any findings retained in that original state before accepting the restart.

## Forget and capture identity

Forget suppresses the stored provenance identity within its project. Capture IDs
depend on the resolved source path, generation, byte offset and raw JSONL line.
Storage also binds the source and account. Copying identical content to another
resolved path, using another account or importing into another project can create
a new memory after forgetting the original. There is no content-wide or
cross-project forgetting guarantee. Keep the original path, account, project and
cursor for retries, and review alternate imports explicitly.
