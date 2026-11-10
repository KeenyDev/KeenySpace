# Deploying on a single host

This guide runs the standard compose stack (see [install.md](install.md)) on one Linux
server behind a public domain. It works on any VPS, cloud VM, or bare-metal machine with
a public IP; nothing in it depends on a particular provider. It adds:

- an optional **dedicated data disk** holding Docker's data-root, so the canon markdown,
  both Postgres databases, and the TLS certificates can outlive the machine;
- **Caddy** terminating TLS for `DOMAIN` (KeenySpace) and `auth.DOMAIN` (Authentik);
- a **daily backup** shipped off-host to any [rclone](https://rclone.org/) destination.

KeenySpace v1 is single-instance by design: one host, one server container. Platforms
without a persistent POSIX filesystem (most PaaS offerings) do not fit, and there is no
Helm chart for Kubernetes in v1.

The files this layout adds are `deploy/docker-compose.prod.yml`,
`deploy/reverse-proxy/Caddyfile.prod`, `deploy/host/`, and
`deploy/scripts/scheduled-backup.sh`.

## 1. Requirements

- A Debian or Ubuntu server with at least 2 vCPU / 4 GB RAM (Authentik alone needs about
  2 GB) and a public IP. Other distributions work if you install Docker Engine with the
  compose plugin (v2.24 or newer) yourself before running `provision.sh`.
- A domain you control, with two names pointing at the server: `DOMAIN` (e.g.
  `ks.example.com`) and `auth.DOMAIN` (e.g. `auth.ks.example.com`). A static IP keeps
  these records valid across rebuilds.
- An API key for the compile provider (`ANTHROPIC_API_KEY` by default). The provider
  must be reachable from where the server runs.
- Off-host storage for backups that rclone can write to: S3-compatible object storage,
  SFTP to another machine, or any other rclone backend.
- The `keenyspace` CLI on your workstation (`uv tool install --from ./packages/client
  keenyspace` from a checkout).

Recommended, if your provider offers them:

- **A separate block-storage disk** for the data, attached to the server. Treat the
  system disk as disposable.
- **A network firewall** in front of the server: inbound TCP 22 from your own address
  only, TCP 80 and 443 from anywhere. The stack itself publishes only Caddy on public
  interfaces; Postgres, Authentik, and the direct API port listen on `127.0.0.1`.

## 2. Provision the host

Either paste [`deploy/host/cloud-init.yaml`](../deploy/host/cloud-init.yaml) as user data
when you create the VM (review its `runcmd` lines first), or run the same steps by hand
as root:

```bash
apt-get update && apt-get install -y git curl jq rclone
git clone --branch main https://github.com/KeenyDev/KeenySpace.git /opt/keenyspace
DATA_DEVICE=/dev/disk/by-id/<your-data-disk> bash /opt/keenyspace/deploy/host/provision.sh
```

`provision.sh`:

- formats `DATA_DEVICE` with ext4 only if it carries no filesystem yet, mounts it at
  `/mnt/keenyspace` (override with `DATA_MOUNT`), and adds it to `/etc/fstab`;
- installs Docker Engine from Docker's apt repository with `data-root` on that disk;
- installs the backup units (`keenyspace-backup.service` and `.timer`) without enabling
  them.

Use a stable device path such as `/dev/disk/by-id/...`, not `/dev/sdX`, which can change
between boots. Omit `DATA_DEVICE` to keep Docker's data on the system disk.

Check the result:

```bash
cloud-init status --wait                    # only when provisioned via cloud-init
docker info --format '{{.DockerRootDir}}'   # /mnt/keenyspace/docker with a data disk
```

## 3. Configure

```bash
cd /opt/keenyspace
./deploy/gen-secrets.sh
cat >> deploy/.env <<'EOF'
DOMAIN=ks.example.com
KEENYSPACE_IMAGE=ghcr.io/keenydev/keenyspace:0.2.0-alpha.1
EOF
```

Add the compile provider key with an editor rather than `echo`, so it does not land in
the shell history:

```bash
nano deploy/.env   # add ANTHROPIC_API_KEY=sk-ant-...
```

`DOMAIN` is a bare hostname: no scheme, no port. `docker-compose.prod.yml` derives the
public URL, the OIDC issuer (`https://auth.DOMAIN/application/o/keenyspace/`), and the
redirect URIs from it, and runs `KEENYSPACE_IMAGE` instead of building from source.

## 4. Start

Both DNS names must resolve to the server first: Caddy requests the certificates on
start.

```bash
alias ks-compose='docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.prod.yml'
ks-compose pull
ks-compose up -d
ks-compose ps          # wait until every service is healthy (Authentik takes 60-90 s)
curl -fsS https://ks.example.com/healthz
```

## 5. First login

From your workstation:

```bash
keenyspace init        # server URL: https://ks.example.com
```

The device-code login opens `https://auth.ks.example.com`. Log in as `akadmin` with the
`AUTHENTIK_BOOTSTRAP_PASSWORD` from `deploy/.env` on the server, then change that
password in the Authentik admin UI and add your users to `keenyspace-users` (see the
production hardening section of [oidc-authentik-setup.md](oidc-authentik-setup.md)).

To connect Claude Code or another MCP client, follow [mcp-setup.md](mcp-setup.md) with
`https://ks.example.com` as the base URL.

## 6. Scheduled backups

`deploy/scripts/scheduled-backup.sh`, run daily by `keenyspace-backup.timer`:

1. recreates the server container with `KEENYSPACE_ADMIN_API_ENABLED=1`;
2. streams `POST /v1/admin/backup`: the canon markdown and the KeenySpace database in one
   consistent tarball;
3. dumps the Authentik database, which that tarball does not cover;
4. recreates the container without the flag, also when any step fails, so the admin API
   is mounted only while the backup runs;
5. copies both files to `BACKUP_REMOTE` with rclone and prunes old copies locally and
   remotely.

Each recreate is an outage of a few seconds and interrupts a compile in progress; the
compile backstop picks it up again. The default schedule is 03:30 UTC.

**Storage.** Configure an rclone remote on the server interactively (`rclone config`).
Keep the destination outside the server's failure domain: another region, another
provider, or another machine.

**API key.** The backup authenticates with an API key whose owner is in the admin group
(`keenyspace-admins`; the blueprint puts `akadmin` there). Mint one from your workstation
after logging in as `akadmin`:

```bash
TOKEN=$(jq -r .access_token ~/.config/keenyspace/auth.json)
curl -sS -X POST https://ks.example.com/v1/api/auth/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "scheduled-backup"}'
```

Then fill in `/etc/keenyspace/backup.env` (root-only, mode 600) on the server:

```bash
KEENYSPACE_BACKUP_API_KEY=ks_live_...
BACKUP_REMOTE=offsite:my-bucket/keenyspace
```

`BACKUP_REMOTE` must be a dedicated path: everything under it older than
`REMOTE_RETENTION_DAYS` (default 30) is deleted.

This key can export and overwrite the whole dataset while the admin API is mounted, and
the server checks its owner's group snapshot from the owner's last OIDC login. If you set
`KEENYSPACE_AUTH__API_KEY_GROUP_SNAPSHOT_MAX_AGE_DAYS`, `akadmin` must log in via OIDC
within that window or backups start failing with 401/403.

**Run it once by hand, then enable the timer:**

```bash
systemctl start keenyspace-backup.service
journalctl -u keenyspace-backup.service -n 50
systemctl enable --now keenyspace-backup.timer
systemctl list-timers keenyspace-backup.timer
```

A backup you have never restored is a hope, not a backup. Restore one into a throwaway
stack periodically, following [backup-restore.md](backup-restore.md).

Provider-level machine backups often exclude attached data disks. If you want a second
safety net beyond the tarballs, snapshot the data disk on a schedule as well.

## 7. Upgrading

Follow [upgrade.md](upgrade.md). On this layout that means: run
`systemctl start keenyspace-backup.service`, check out the release tag in
`/opt/keenyspace`, set the matching `KEENYSPACE_IMAGE` in `deploy/.env`, then
`ks-compose pull && ks-compose up -d`.

## Limitations

- **Browser login route.** The Authentik blueprint allows the redirect URI
  `http://localhost:8000/v1/api/auth/callback` only, so the server's browser login
  (`/v1/api/auth/login`) fails on a public domain. The CLI device-code login and MCP
  OAuth (loopback redirect) are unaffected.
- **Single node.** Do not run a second server container against the same data, and do
  not put the data on network storage: WAL locking is in-process, and atomic page writes
  rely on a local filesystem.
- **Untested end to end.** The overlay and the scripts are linted and the compose
  configuration renders, but this layout has not yet been exercised on a real server.
  Run the backup once and restore it into a throwaway stack before trusting it with real
  data.
