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
