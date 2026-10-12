# Architecture

The canonical store is one SQLite database on the selected host. MCP clients use
stdio locally or an authenticated loopback HTTP service. Client credentials are
scoped to projects and operations. Optional semantic indexing runs locally on CPU.

Remote access is an outer deployment concern: an operator must supply HTTPS, TLS,
ingress limits, and explicit OAuth approval. The service itself does not create a
tunnel or public route. CORTEX and snapshot capture are disabled optional modules.
Their preserved source lives in `modules/dots_brain_cortex` and
`modules/dots_brain_capture`, outside the core wheel and source distribution.
There is no discovery mechanism, plugin framework or activation path in this
candidate. Existing CLI names return a capability error before touching resources.

Core startup does not read `cortex.json`, import module code or register CORTEX
tools. Fresh setup and released-v1 migration install only core extensions; the
existing optional ledger SQL and schema version 2 are unchanged. Existing ledger
tables and module files are preserved. Explicit restore still marks historical
in-flight sends uncertain and protects the ledger at cutover when it exists;
recovery also works when neither store has that optional table. A table present
on only one side prevents cutover rather than silently dropping history.

The source repository, package artifact, installed runtime, and client behavior are
different evidence layers. A successful test or build does not prove a deployment.
