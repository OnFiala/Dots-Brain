# Security

Dots Brain stores personal context. Treat the host, backups, and connected clients
as part of the trust boundary. A memory entry is untrusted data, not an instruction
that overrides the assistant's user or system instructions.

Do not expose a development server to the internet. Remote production support
requires verified authentication, TLS, lifecycle management, and client tests.
Keeping a token out of model output does not isolate it from an agent that can
read its file or process memory.

Do not put credentials or private memory content in public issues. Use GitHub's
private vulnerability reporting if it is enabled for this repository. Otherwise,
open a public issue requesting a private reporting channel without including
exploit details, secrets, or personal data. Do not assume a private channel exists.
