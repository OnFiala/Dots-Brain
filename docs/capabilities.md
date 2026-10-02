# Capability matrix

This file describes implementation and verification, not a marketing promise.

| Capability | State |
| --- | --- |
| Source-aware local memory store | Implemented; local tests pass |
| Full-text retrieval | Implemented; local tests pass |
| Bounded context | Implemented; character budget tested |
| Local semantic retrieval | Implemented; synthetic Czech-to-English model test passes |
| Six memory MCP tools | Implemented; SDK HTTP and stdio tests pass |
| Agent setup and diagnostics | Implemented for the local host |
| Repeatable background startup and recovery on local client reconnect | Implemented; process and crash-recovery tests pass |
| Claude Code, Cursor, Codex, and generic MCP configuration adapters | Implemented; actual bridge calls tested; Claude Code connection checked |
| Scoped credentials, project boundaries, expiry, revocation | Implemented; tests pass |
| Client disconnection and whole-instance disabling with data retention | Implemented; lifecycle, credential isolation between profiles, and configuration preservation tested |
| Automatic removal of package/source files or personal data | Not implemented; installation-specific program removal is documented |
| Stdio bridge to an existing HTTP instance | Implemented; subprocess-to-live-HTTP test passes |
| Autonomous remote client registration | Planned |
| OAuth for web MCP clients | Planned |
| Verified public VM ingress or tunnel | Not verified |
| Strong credential isolation | Not verified |
| Storage/HTTP load and local embedding load | Synthetic stress runs passed; see [evidence](stress-tests.md) |
| Continuous conversation capture | Planned per provider |
| Automated backup, restore, or database migration rollback | Planned |
| Native ChatGPT views and MCP Events | Planned |
| Dot VM persistence and cost guarantees | Not verified |

No named provider is production-certified until tested in that provider's actual
client. A test using the MCP SDK establishes protocol behavior, not support in
all AI products. Missing features must remain explicit in installer output.
