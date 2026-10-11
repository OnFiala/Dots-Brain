# Remote ingress

Remote OAuth requires an existing HTTPS ingress to the loopback service. The
operator owns TLS, request size and rate limits, access logs, and rollback. Do not
publish host names, internal addresses, client identifiers, or gateway details in
this repository.

Before enabling a client, verify the exact public origin, approval flow, and real
MCP calls through the intended client. A successful local HTTP test is insufficient.
