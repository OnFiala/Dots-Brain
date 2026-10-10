# Security policy

Dots Brain stores personal context. Treat the memory host, its backups, and each
connected client as part of the trust boundary. A memory entry is untrusted data;
it must never authorize commands or change system instructions.

## Supported versions

Security fixes target the latest released alpha. The working-tree candidate is not
a release. Older alphas may not receive fixes.

## Reporting a vulnerability

Private vulnerability reporting is not enabled on this repository as of
2026-10-10. Request a private contact through a minimal
[GitHub issue](https://github.com/OnFiala/Dots-Brain/issues/new); include no exploit
steps, credentials, personal data, or host details. If the repository later enables
private reporting, use its Security tab.

## Deployment model

The HTTP service binds loopback by default. A remote OAuth client requires an
existing HTTPS route with TLS, explicit request approval, and rate and size limits
at the ingress. Dots Brain does not create that route. Do not expose a development
server directly to the internet.

Credential files remain readable by the local OS owner. Keeping a token out of an
assistant response is useful, but it does not create a secret boundary for a user
or process that can read the file. Store backups and exports privately: they may
contain memories and authentication state.

The input filter rejects known high-confidence credential forms. It cannot detect
every secret, so do not save credentials, keys, or private exports as memory.
