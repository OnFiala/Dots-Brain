# Linux deployment template

The files in this directory are examples for a single Linux host. They are not a
record of any existing installation. Replace `memory.example.com`, the private
addresses, executable path, service user, and data directory before use.

## What the template provides

- `systemd/dots-brain.service` runs a loopback HTTP MCP server with
  `--public-gateway`: only OAuth grants are accepted on this listener.
- `systemd/dots-brain-ingress.service` starts a small dedicated nginx gateway.
- `systemd/dots-brain-onboarding.service` opens the gateway pairing gate for ten
  minutes when explicitly started.
- `nginx/dots-brain.conf` is an example gateway configuration. It uses
  `memory.example.com` and TEST-NET-1 placeholders `192.0.2.1` and `192.0.2.2`.
  Replace both addresses before use; they are documentation-only values.

The nginx configuration permits MCP bodies up to 1 MiB and limits OAuth endpoint
bodies to 16 KiB. It accepts only the expected methods, removes forwarded-header
input, and sends unknown paths to 404. The backend's command-line flag owns the
authentication policy. No request header can enable or disable public mode.

## Prepare an instance

1. Create a dedicated unprivileged service account and a private data directory.
2. Check out a reviewed version and create its environment with `uv sync --locked
   --extra semantic` if semantic search is wanted.
3. Initialize the store and prepare the model if selected. Before exposing it,
   run a private managed service with `up` and verify a read with its generated
   `probe.connection.json`. `up` itself checks a real MCP read before reporting
   readiness. Then run `down`; the public service cannot share its writer lease.
4. With writers stopped, configure OAuth for the intended HTTPS origin as
   described in [OAuth](../docs/oauth.md). Record the private backup and rollback.
5. Copy and edit the unit and nginx files to the host's system locations. Preserve
   `--public-gateway` in the memory unit. Validate both with the host tools.
6. Start the memory service, then the gateway and the existing TLS route. A request
   without a grant must return 401. This check proves access is closed, not that a
   client is connected. Complete the onboarding flow below and verify a real MCP
   read through that client's OAuth grant before declaring the connection ready.

Do not run both `dots-brain up` and the systemd memory service for one data
directory. The systemd unit owns the long-running HTTP process. Keep the service
loopback-only; an existing TLS terminator or tunnel is a separate operator choice.

For a reviewed checkout already installed at `/opt/dots-brain/current` with its
`.venv`, a typical systemd host uses the following commands. Create the account
only if it does not already exist. Edit the example addresses and domain before
installing the templates:

```sh
sudo useradd --system --home-dir /var/lib/dots-brain --shell /usr/sbin/nologin dots-brain
sudo install -d -o dots-brain -g dots-brain -m 700 /var/lib/dots-brain
sudo -u dots-brain /opt/dots-brain/current/.venv/bin/dots-brain --data-dir /var/lib/dots-brain setup
sudo -u dots-brain /opt/dots-brain/current/.venv/bin/dots-brain --data-dir /var/lib/dots-brain model prepare
sudo -u dots-brain /opt/dots-brain/current/.venv/bin/dots-brain --data-dir /var/lib/dots-brain oauth configure --issuer https://memory.example.com
python3 scripts/validate_deploy.py
sudo install -m 644 deploy/systemd/dots-brain*.service /etc/systemd/system/
sudo install -d -m 755 /etc/dots-brain
sudo install -m 644 deploy/nginx/dots-brain.conf /etc/dots-brain/ingress.conf
sudo systemctl daemon-reload
sudo systemctl start dots-brain.service dots-brain-ingress.service
```

The service account needs read and execute access to the reviewed installation;
the database directory is its writable location. `validate_deploy.py` checks
template syntax in temporary paths. It does not install units or validate the
host's TLS certificates. Use `systemctl enable` only after local acceptance if
boot startup is wanted.

For example, an existing TLS terminator on the same host can forward
`https://memory.example.com` to the dedicated gateway at `127.0.0.1:8788`.
In that topology, set the template's `listen` and `allow` addresses to loopback.
For a tunnel connector in another network namespace, use its actual reachable
private address and peer allowlist instead. Forward to the gateway, retain the
original expected Host, and keep port 8787 private. Certificate issuance, DNS and
the public tunnel remain the operator's separately approved configuration.

## OAuth onboarding

Opening the CLI onboarding window alone is insufficient when the gateway example
is used. Start the short gateway unit for the same time window, then approve the
exact client request with its callback hostname and intended projects. Close the
window after the flow and check the resulting grant. See [OAuth](../docs/oauth.md).

To roll back before accepting writes, stop ingress and the memory unit, restore
the recorded installation and unit versions together, and follow
[database rollback](../docs/upgrading.md). A binary rollback alone cannot open
a newer schema or restore the prior public authentication policy.

This template needs host-specific review before use. It does not verify TLS,
public reachability, client UI activation, reboot recovery, or backup recovery.
