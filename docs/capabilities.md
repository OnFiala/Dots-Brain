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
| Scoped credentials, project boundaries, expiry, revocation | Implemented; tests pass |
| Stdio bridge to an existing HTTP instance | Implemented; end-to-end verification pending |
| Autonomous remote client registration | Planned |
| OAuth for web MCP clients | Planned |
| Verified public VM ingress or tunnel | Not verified |
| Strong credential isolation | Not verified |
| Continuous conversation capture | Planned per provider |
| Native ChatGPT views and MCP Events | Planned |
| Dot VM persistence and cost guarantees | Not verified |

No named provider is production-certified until tested in that provider's actual
client. A test using the MCP SDK establishes protocol behavior, not support in
all AI products. Missing features must remain explicit in installer output.
