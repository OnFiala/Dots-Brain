# Capability status

| Capability | Source status | Acceptance boundary |
| --- | --- | --- |
| SQLite source records, revisions, and full-text search | Implemented and tested | Requires local runtime verification per host |
| Local CPU embeddings | Optional implementation | Model download and corpus quality are host-specific |
| Scoped local credentials and bridge | Implemented and tested | Client UI activation needs a client-level check |
| Remote OAuth | Implemented locally | Existing HTTPS route and real client flow required |
| Backup and staged restore | Implemented | Host-loss recovery is not supported |
| Snapshot capture | Disabled; source retained outside core | Re-enabling and internal repairs require a separate plan |
| CORTEX connector | Disabled; source retained outside core | No tools or connection are loaded by core, including from old configuration |

This table makes no claim about a particular deployment. See [verification](verification.md).
