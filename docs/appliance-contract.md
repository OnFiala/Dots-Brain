# Appliance, CORTEX, and bot onboarding contract

## Selected target and current state

On 2026-10-09 the owner selected `openclaw-appliance` as the host for the bots'
shared external memory and required CORTEX connectors. Botter and Grok Bot will
each contribute the knowledge currently available to them, then use this memory
in their ordinary work. Recovering the historical Dot VM is not a prerequisite.
This document defines the delivery contract. The unreleased candidate implements
scoped connectors and per-record write receipts; appliance deployment and actual
client onboarding remain unverified.

Keep one canonical Dots Brain instance on the appliance, with persistent private
storage, local embeddings, and its own supervised process. The development
checkout and each bot's execution environment are clients, not alternate stores.
Record the new instance identity and creation time. Do not claim that a fresh
instance restores the unavailable historical database or erase any recovered data.

## Ownership and dependencies

| System | Owns |
| --- | --- |
| Dots Brain | Shared bot memory records, their own revisions, source references, and deletion suppression |
| Agent Workspace | Its authoritative source objects/revisions, claims, files, tasks, handoffs, and receipts/audit |
| CORTEX | Its canonical decisions, outcomes, doctrine, and existing knowledge |

Dots Brain can hold memories submitted directly by either bot; a Workspace source
is not required for every memory. When a record refers to Workspace or CORTEX,
keep that system's object identity and revision/evidence reference. Do not create
independently editable copies of Workspace source objects, source revisions,
claims, files, tasks, or audit records, or of CORTEX canonical records. A bot's
directly submitted recollection remains a Dots memory with its own provenance.

The memory core must keep working when Workspace, CORTEX, or a cloud model is
unavailable. Report external lookup or write failures separately from local memory
status. A CORTEX outage is not an empty result or proof that no relevant fact exists.

## CORTEX connectors — implemented locally, upstream access pending

Provide two separately authorized capabilities over CORTEX's supported API/MCP
boundary. Never mount, copy, or directly edit its database or credential files.

- **Read:** retrieve bounded relevant context and fetch its supporting records.
  Keep the original CORTEX identifiers and source links in results. A retrieval
  alone does not import that result into Dots Brain. Persistent imports, if added,
  need explicit source identity, freshness, correction, and deletion behavior.
- **Write:** submit selected decisions, outcomes, or notes with their Dots Brain
  source references and originating actor. Ordinary bot memories remain local;
  they are not automatically promoted to CORTEX doctrine. Existing read-only
  clients must not gain CORTEX write authority through the bridge.

Enforce caller/project authorization on the connector before invoking CORTEX.
Possessing a service credential must not allow callers to read additional projects
or choose arbitrary CORTEX tools. Keep connector credentials on the appliance and
out of bot prompts, memory text, logs, and repository files.

Persist the identity and result of each selected outbound operation. Use upstream
idempotency where supported. If a write times out with an unknown result, reconcile
its receipt/source reference before retrying; do not assume every CORTEX write tool
supports replay. Preserve both the write acknowledgement and the retrievable object
reference; an event acknowledgement alone is not necessarily a fetchable object ID.

No automatic bidirectional database mirroring. A return lookup must not re-export
the same statement as a new fact. Deletion in Dots Brain does not silently delete
CORTEX records, and deletion in either system must not be undone by cached results
or reimport. A request to erase information across systems needs explicit per-system
receipts and remaining-copy reporting.

The existing Dots OAuth issuer and another service's issuer are separate token
authorities. Reusing HTTPS routing does not make their tokens interchangeable.
External issuer support requires a separately tested authorization contract.

## Initial contribution by each bot

After its own authenticated connection is verified, each bot writes directly to
the canonical Dots Brain instance. Do not route a personal knowledge dump through
Git or require the owner to paste it between chats.

1. Inventory knowledge actually accessible to that bot: user preferences, project
   context, relevant people/relationships, working agreements, decisions, and open
   questions. Do not claim access to hidden or unavailable conversation history.
2. Submit small, independently revisable records. Preserve available source links
   and dates. Label a recollection without an original source as the bot's initial
   self-report, not as a newly confirmed user statement. Separate fact, inference,
   uncertainty, and potentially outdated information. Never invent missing dates.
3. Use a stable batch identity and stable record identities; keep Botter's and
   Grok Bot's origins distinct. Preserve the server-authenticated writer separately
   from the source claimed in the content. A retry resumes the same contribution.
4. Keep conflicting statements visible with their evidence. Do not let the later
   uploader silently replace another bot's account, or treat two copies of one
   source as independent confirmation. Corrections use the observed revision.
5. Verify saved records and return content-free counts for accepted, duplicate,
   conflicting, and failed items, plus resumable receipts. A generated summary or
   a successful connection is not evidence that the contribution was stored.

Exclude credentials, tokens, private keys, and inaccessible provider-internal
instructions. The owner-authorized personal knowledge remains in the private store.
This contribution is a snapshot of what the bot can report, not a complete archive
or a restoration of the historical VM.

The contribution protocol does not require a new server batch API: stable
per-record identities and verified individual write receipts may implement it.
The server stores its authenticated writer separately from client-supplied source
labels. A locally assembled summary still does not prove a completed contribution.

## Ongoing use

Each bot's installed operating instructions should require relevant memory lookup
before substantive work, source inspection when accuracy matters, and saving useful
new context or corrections after significant work. Record new decisions, changes,
preferences, and outcomes with evidence; avoid saving every conversational filler.
Use the CORTEX write connector for the authorized durable record types above.

This is agent-directed use of tools, not guaranteed provider-wide capture. Verify
it in each actual bot across a later session. MCP availability alone does not make
a bot call the tools. Missing tools or a failed write must remain visible.

An optional cloud helper may later propose memories from authorized excerpts.
It has no direct database or deletion access; the host validates sources, projects,
and revisions before accepting writes. Local reads and explicit writes remain
available without the model. Its budget, retry limits, and data boundary must be
configured before activation.

## Acceptance milestones

- An isolated appliance test proves persistent data, supervised restart, backup,
  restore, and the remaining host-loss boundary before personal contributions.
- Botter writes a synthetic record that Grok reads at the same ID/revision, and
  vice versa; test scoped rejection, revoked access, and authorized cleanup.
- CORTEX lookup returns original references; an authorized synthetic outbound
  record has a receipt and retrievable source link. Test read-only denial, project
  isolation, unavailability, and an uncertain write without duplicate replay.
- Both bots complete resumable initial contributions without silent conflict
  replacement, then demonstrate relevant recall and a new sourced write in a
  subsequent session. Core memory remains usable during a CORTEX outage.

See the [roadmap](roadmap.md) for the required shared-writer/schema work and the
[capability matrix](capabilities.md) for implemented versus planned behavior.
