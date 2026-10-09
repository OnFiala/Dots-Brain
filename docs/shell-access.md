# Memory access from an agent shell

An agent with a shell on the memory host can use the same authenticated MCP
service without a public tunnel or a new memory database. The source checkout
includes `scripts/memory_client.py` for this purpose. This helper is unreleased;
it is not part of the published `0.3.0-alpha.2` archive.

Use the runtime's Python interpreter and an existing private client credential:

```sh
.venv/bin/python scripts/memory_client.py \
  --credential-file /absolute/private/client.json
```

Omitting the tool name discovers the actual permitted tools and their schemas.
To call a tool, provide its arguments as a JSON object on stdin:

```sh
.venv/bin/python scripts/memory_client.py \
  --credential-file /absolute/private/client.json memory_context <<'JSON'
{"task":"Find the deployment decision","project":"demo","max_chars":3000}
JSON
```

The helper uses the official MCP client and prints its result as JSON. MCP tool
errors and connection failures return a nonzero exit status. Credentials stay in
their private file; the helper never prints them. Access remains limited by the
same client's scopes and projects. No second database or authentication system
is introduced. Tool results can contain private memories: do not publish them.

On the canonical host only, add `--local-data-dir /existing/memory` to start a
stopped enabled service before connecting. This option requires an already
initialized store, a credential valid for that store, and a credential endpoint
matching its saved loopback service address. A missing service state or mismatch
fails before startup; fix the installation or connection explicitly. The stdio
bridge applies the same checks. This respects whole-instance uninstall and does
not enable VM boot supervision. For a remote client, omit the local data directory.

The agent may install a private wrapper containing these paths and document the
canonical instance in its workspace instructions. A wrapper contains no token.
This enables shell access; native tool activation in an application's interface
is a separate verification step. Installing the helper does not import history
or capture conversations automatically.
