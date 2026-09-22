# Upgrading KeenySpace

Three rules govern every upgrade:

1. Images are pinned to exact versions — you upgrade by editing a tag, deliberately.
2. Database migrations only move forward — never downgrade across a migration without a restore.
3. Back up before every upgrade. No exceptions.

## Version-pin policy

`deploy/docker-compose.yml` pins every image to an exact tag:

| Component | Pin | Notes |
|-----------|-----|-------|
| KeenySpace | SemVer release tag (`ghcr.io/keenydev/keenyspace:0.1.0-alpha.1`, or built from a tagged source checkout) | Pre-release tags are never published as `latest` |
| Authentik | `ghcr.io/goauthentik/server:2026.2.7` | Server and worker must always run the same tag |
| Postgres (KeenySpace) | `postgres:17.2-alpine` | Major Postgres upgrades require a dump/restore cycle, not just a tag bump |
| Postgres (Authentik) | `postgres:16.15-alpine` | Same caveat |
| Caddy | `caddy:2.11.4-alpine` | |

Never switch any of these to `:latest`. An unattended `docker compose pull` against
`latest` is how self-hosted stacks break overnight.

To upgrade a component:

1. Read the component's release notes for every version between your pin and the target,
   looking for breaking changes (config renames, volume path changes, migration notes).
2. Back up (see the gate below).
3. Edit the tag in `deploy/docker-compose.yml` (or `git pull` the new KeenySpace release,
   which updates the pins for you).
4. `docker compose -f deploy/docker-compose.yml up -d --build` and watch the
   healthchecks come back green.

## Migration ordering

KeenySpace runs Alembic migrations automatically on boot (`KEENYSPACE_AUTO_MIGRATE: "true"`
in the compose file). The correct upgrade order is therefore:

1. **Back up** while the old version is still running.
2. Bring the new image up. Alembic migrates the schema forward before the server starts
   serving traffic.
3. Verify `curl http://localhost:8000/healthz` returns 200 and check the logs for
   migration errors.

**Never roll back across a migration by just re-pinning the old image.** Once Alembic
has migrated the schema forward, the old code may not understand the new schema. The
only supported downgrade path is: restore the pre-upgrade backup
([docs/backup-restore.md](backup-restore.md)), then start the old image against the
restored state.

### Worked example: the Authentik 2025.12 media path change

Authentik 2025.12 moved its media directory from `/media` to `/data/media`. An operator
upgrading from 2024.x by only bumping the tag would silently lose uploaded media (logos,
icons) because the volume was still mounted at the old path. The shipped compose file is
already on 2026.2 with the correct mounts:

```yaml
volumes:
  - authentik-media:/data/media
```

on both `authentik` and `authentik-worker`. This is exactly the class of breaking change
the release-notes-first rule exists for: the tag bump itself succeeds, the healthcheck
passes, and the breakage only shows up later.

## Backup-before-upgrade gate

Before ANY image tag change:

```bash
keenyspace backup --output pre-upgrade-$(date +%Y%m%d).tar.gz
```

This requires the admin API flag and a logged-in session — full procedure in
[docs/backup-restore.md](backup-restore.md). Store the tarball off-host before
proceeding. If the upgrade goes wrong, this tarball is your only way back across a
schema migration.

If you want the gate rehearsed end-to-end (backup, full wipe, restore, assert),
run the drill described in [docs/backup-restore.md](backup-restore.md) against a
throwaway environment.
