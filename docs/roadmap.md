# Roadmap

## Implemented source milestones

- Local memory, revisions, full-text/semantic search, MCP and scoped local clients.
- Lifecycle, explicit reinstall, OAuth PKCE, refresh and revocation.
- Candidate schema v2, authenticated authorship, project identities and suppression.
- Sanitized audit, explicit coverage gaps and bounded resumable snapshot collectors.
- Scoped CORTEX context and selected publication with uncertain-write reconciliation.
- Offline migration, verified backups and guarded restore/cutover.

See [capabilities](capabilities.md) and [verification](verification.md) for evidence.
The owner-approved internal appliance deployment passed synthetic acceptance on
2026-10-09. Both bots subsequently verified actual chat read/write access;
Grok's reverse read of Botter's synthetic fact and external integrations remain.

## Next acceptance: canonical appliance and clients

1. Complete encrypted off-host backup and host-loss recovery. The [internal
   deployment](appliance-deployment.md), service restart, online backup and isolated
   synthetic recovery are verified; a real appliance reboot is still untested.
2. Provision dedicated CORTEX access over a supported authenticated boundary;
   verify both original references and one authorized selected write/receipt.
3. The constrained public MCP/OAuth route and both actual OAuth callbacks are
   verified. Keep onboarding closed between owner-approved pairing sessions;
   Tailscale remains the administration/private-client path.
4. Complete Grok's reverse read of Botter's synthetic fact, then owner cleanup.
   Both separate bot connections, each write/readback and Botter's cross-read are
   verified. Project denial and isolated revocation passed synthetic acceptance.
5. Each bot submits its available knowledge with stable identities and uncertainty
   labels, verifies receipts, then demonstrates relevant recall in a later session.
6. Establish the available live capture mechanisms and verify the first scheduled
   run of the registered twice-daily Codex audit review. Its manual baseline passed;
   it cannot infer complete provider activity from submitted audit events.

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
