# Managed Linux deployment

This project ships no production deployment diary. Deploying a long-lived Linux
service requires an operator-owned data directory, a least-privilege supervisor,
private backups, and direct runtime acceptance checks.

Keep the service loopback-only unless an existing HTTPS ingress is deliberately
configured. Record its host-specific configuration and rollback procedure outside
the public repository. A successful source build does not prove host lifecycle,
remote OAuth, or client behavior.
