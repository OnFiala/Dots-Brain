# Roadmap

## Implemented source milestones

- Local memory, revisions, full-text/semantic search, MCP and scoped local clients.
- Lifecycle, explicit reinstall, OAuth PKCE, refresh and revocation.
- Candidate schema v2, authenticated authorship, project identities and suppression.
- Sanitized audit, explicit coverage gaps and bounded resumable snapshot collectors.
- Scoped CORTEX context and selected publication with uncertain-write reconciliation.
- Offline migration, verified backups and guarded restore/cutover.

See [capabilities](capabilities.md) and [verification](verification.md) for evidence.
These changes do not certify actual bot access or the production appliance.

## Next acceptance: canonical appliance and clients

1. Deploy the reviewed [appliance plan](appliance-deployment.md) under its owner
   approval. Verify its identified disk, supervision, restart and recovery with
   synthetic data. Complete the off-host backup/restore boundary.
2. Provision dedicated CORTEX access over a supported authenticated boundary;
   verify both original references and one authorized selected write/receipt.
3. Expose only the required MCP/OAuth paths for cloud clients. Tailscale remains
   the administration/private-client path; shell reachability does not prove a
   cloud MCP backend can reach it. Verify the actual provider callback registration.
4. Connect Botter and Grok separately. Each writes a synthetic note the other reads
   at the same ID/revision; test project denial, revocation and explicit cleanup.
5. Each bot submits its available knowledge with stable identities and uncertainty
   labels, verifies receipts, then demonstrates relevant recall in a later session.
6. Establish the available capture mechanisms honestly and enable twice-daily
   Codex review of audit gaps, unexpected writes and affected resources.

## Capture and durability gaps

Grok's inspected JSONL files may be stale snapshots; persistent live collection
is unverified. Botter's enterprise hooks are unverified for the actual account.
Neither model instructions nor MCP availability guarantee every action is captured.
Preserve gaps instead of treating missing observations as safe activity.

A backup can contain revoked credentials and forgotten content. Recovery revokes
credentials and reconciles surviving deletion barriers. If the original store is
lost, a trustworthy current deletion history is still required. Restoring an old
backup after new facts/audit/CORTEX operations requires reconciliation; cutover
refuses canonical divergence rather than silently discarding newer history.

## Optional later work

- Fixed bilingual quality/capacity evaluation and comparison with EmbeddingGemma.
- Additional provider adapters with verified public-message visibility and live sources.
- Budgeted Cerebras extraction proposals/triage, never canonical authority.
- Native client views and MCP Events, without moving the canonical store.
