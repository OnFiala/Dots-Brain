# Architecture

The canonical store is one SQLite database on the selected host. MCP clients use
stdio locally or an authenticated loopback HTTP service. Client credentials are
scoped to projects and operations. Optional semantic indexing runs locally on CPU.

Remote access is an outer deployment concern: an operator must supply HTTPS, TLS,
ingress limits, and explicit OAuth approval. The service itself does not create a
tunnel or public route. CORTEX is an optional separate system and is connected only
through explicit configuration.

The source repository, package artifact, installed runtime, and client behavior are
different evidence layers. A successful test or build does not prove a deployment.
