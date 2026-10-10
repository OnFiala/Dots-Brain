# Capability status

| Capability | Source status | Acceptance boundary |
| --- | --- | --- |
| SQLite source records, revisions, and full-text search | Implemented and tested | Requires local runtime verification per host |
| Local CPU embeddings | Optional implementation | Model download and corpus quality are host-specific |
| Scoped local credentials and bridge | Implemented and tested | Client UI activation needs a client-level check |
| Remote OAuth | Implemented locally | Existing HTTPS route and real client flow required |
| Backup and staged restore | Implemented | Host-loss recovery is not supported |
| Snapshot capture | Bounded JSONL import | Continuous provider capture is not implemented |
| CORTEX connector | Optional configuration surface | Endpoint and actual upstream calls require separate proof |

This table makes no claim about a particular deployment. See [verification](verification.md).
