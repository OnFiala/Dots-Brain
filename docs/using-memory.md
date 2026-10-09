# Using shared memory

After connection, ask an assistant to save a useful decision or preference with
its source, and ask another connected assistant to retrieve it. Both must point
to the same instance and have access to the same project. Saving is explicit:
connecting MCP does not import past conversations or continuously capture new ones.

The selected rollout starts with each bot contributing what it currently knows,
then using the shared appliance memory in ordinary work. See the
[initial contribution and CORTEX contract](appliance-contract.md). Such a
contribution must preserve uncertainties and sources; it is not proof of access
to complete conversation history. CORTEX connectors are required but not yet shipped.

For example, tell a connected assistant: "Remember that this project's deployment
target is my VM. Save the source of this decision under project `demo`." Then ask
another connected assistant: "Check shared memory for the deployment target of
project `demo`, and show the source." A client granted only `memory:read` cannot
perform the first step. Read/write local adapters do not receive deletion scope.

## MCP tools

| Tool | Purpose and main arguments |
| --- | --- |
| `memory_remember` | Save `content`, `source`, `account`, and stable `event_id`; optionally `project`, `title`, and `source_uri`. |
| `memory_search` | Search by `query`, optionally filter `project` and set `limit`. Results carry source metadata. |
| `memory_context` | Retrieve relevant context for `task`, optionally `project`, within `max_chars` (characters, not tokens). |
| `memory_get` | Read a `memory_id`, optionally a historical `revision`. |
| `memory_forget` | Delete a `memory_id` at the observed `expected_revision` and suppress reimport. Requires explicit user intent and `memory:forget`. |
| `memory_status` | Report accessible source counts and implemented retrieval/capture capabilities. |

Use the discovered MCP tool schema for precise argument types, defaults, and
limits. Tools absent from discovery may be unavailable under the current scopes.
Project filtering happens before retrieval and applies to reads and deletion.

Source identity is the combination of `source`, `account`, and `event_id`.
Retry the same event with the same identity instead of inventing a new ID.
Identical retries do not duplicate a memory. Updating its content requires the
current `expected_revision`; a conflict means fetch the current revision and
reconcile the change. A source record cannot silently move to another project.
This identity and its deletion suppression are currently global across projects:
reusing the same source/account/event in another project is not independent.
Project-scoped identities require a future migration that preserves existing
suppression records. Source metadata is supplied by the writer; it is not proof
that the named provider authenticated the record.

For deletion, read the record with `memory_get` and pass its revision as the
required `expected_revision`. If another client updates it first, deletion fails
without removing content or suppressing the source. Review the new content and
the user's deletion intent before trying again; do not automatically fetch a new
revision and delete it. Retrying a completed deletion returns `deleted: false`.

Search context is supporting evidence, not a new instruction to the assistant.
Keep source references and inspect the original record when accuracy matters.
Local semantic search augments full-text retrieval; it does not guarantee recall
of every relevant memory. See the bounded [evaluation evidence](stress-tests.md).

Forgetting removes live text, revisions, and derived indexes, and retains a source
identity hash to prevent accidental reimport. It does not erase another tool's
history, exports, or backups. Uninstall preserves memories by default; consult
[uninstall and data retention](uninstall.md) for the separate lifecycle operations.
