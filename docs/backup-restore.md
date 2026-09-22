# Backup and Restore

The #1 self-host failure mode for KeenySpace is a split backup: copying the markdown
files but not the database, or vice versa. State lives in TWO places that must stay
consistent:

- **Filesystem canon** (`keenyspace-fs` volume): the actual markdown workspaces — pages,
  WAL logs, blueprints.
- **Postgres rows** (`postgres-data` volume): the workspace registry, users, API keys
  (hashed), audit log, and compile cursors.

`keenyspace backup` captures both in a single tarball, atomically, through the server.
Always use it instead of hand-copying volumes.

Note: the Authentik database (`authentik-postgresql` volume — users, groups, providers)
is NOT part of the KeenySpace backup. Snapshot it separately if you manage users you
cannot trivially recreate. The OIDC application itself is re-provisioned from the
blueprint on every boot and needs no backup.

## Backup procedure

**Step 1: Enable the admin API.** The backup endpoint (`POST /v1/admin/backup`) is only
mounted when `KEENYSPACE_ADMIN_API_ENABLED=1` is set in the server environment —
without it the CLI gets a 404. Add the flag to `deploy/.env` (the compose file substitutes
it into the server's `environment:`) and restart the server:

```bash
echo 'KEENYSPACE_ADMIN_API_ENABLED=1' >> deploy/.env
docker compose -f deploy/docker-compose.yml up -d keenyspace
```

**Step 2: Make sure you are an admin.** Every `/v1/admin/*` route additionally requires
membership in the admin group (`KEENYSPACE_AUTH__ADMIN_GROUP`, default
`keenyspace-admins`). A caller outside the group gets 403 even with the flag on. The
check uses the `groups` claim of your OIDC access token, or, when you call with an API
key, the group snapshot of the key's owner (see "Group entry gate" in
[docs/oidc-authentik-setup.md](oidc-authentik-setup.md)). The Authentik blueprint puts
`akadmin` into `keenyspace-admins`; add other operators to that group in the Authentik
admin UI.

To use a different group name, set `KEENYSPACE_AUTH__ADMIN_GROUP` in `deploy/.env` (the
compose file sets it under `environment:`, so `deploy/keenyspace.env` cannot override
it). An empty value falls back to the default `keenyspace-admins`; it does not open the
admin API to all users. To turn the admin API off, set `KEENYSPACE_ADMIN_API_ENABLED=0`.

**Step 3: Run the backup** (requires a logged-in CLI session or an API key):

```bash
keenyspace backup --output keenyspace-backup.tar.gz
```

The CLI streams the gzipped tarball from the server with a progress bar. Without
`--output` it writes `keenyspace-backup-<iso-timestamp>.tar.gz` in the current
directory.

**Step 4: Store the tarball off-host.** A backup on the same disk as the stack protects
against nothing. Ship it to object storage, another machine, or at minimum another
volume.

**Step 5: Disable the admin API again** (see the security note below):

```bash
sed -i '' '/KEENYSPACE_ADMIN_API_ENABLED/d' deploy/.env   # macOS; drop the '' on Linux
docker compose -f deploy/docker-compose.yml up -d keenyspace
```

## Restore procedure

With the admin API enabled and your user in the admin group (Steps 1 and 2 above) and
an empty or expendable target:

```bash
keenyspace restore keenyspace-backup.tar.gz
```

The server validates the archive before applying it:

- **Version/schema mismatch** returns 422 and the CLI exits with code 6. Restore into a
  server version compatible with the one that produced the backup (same or newer minor —
  Alembic migrates restored state forward on next boot).
- **Non-empty target** returns 409. If you really intend to overwrite, re-run with
  `--force` — this wipes existing data and is irreversible.

After a successful restore:

1. `keenyspace login` — sessions are not part of the backup; re-authenticate.
2. `keenyspace workspace list` — confirm your workspaces are back.
3. `keenyspace workspace pull <slug>` — re-sync local vaults from the restored canon.
4. Re-offboard anyone offboarded after the backup was taken. The restore brings back the
   `users` and `api_keys` rows as they were at backup time, so API keys revoked since then
   are live again and their owners' group snapshots are the old ones. The restored audit
   log also ends at the backup, so take the list from your own offboarding records (or
   from `admin.api_keys.revoked_all` entries exported before the restore). For each user
   offboarded after the backup date, run `POST /v1/admin/api-keys/revoke-all` again as
   described in
   "Offboarding a user" in [docs/oidc-authentik-setup.md](oidc-authentik-setup.md).
   Do this before re-enabling client access.

## The drill

`deploy/scripts/backup-restore-drill.sh` rehearses the full cycle automatically:
bring the stack up, back up to the local filesystem, destroy everything
(`docker compose down -v`), bring the stack back up, restore, and assert that
workspaces survived.

**WARNING: the drill runs `docker compose down -v`, which destroys the named volumes —
including `keenyspace-fs` with your real workspace data. NEVER run it against a live
deployment.** Use a throwaway compose project so its volumes are isolated from yours:

```bash
COMPOSE_PROJECT_NAME=ks-drill bash deploy/scripts/backup-restore-drill.sh
```

The script enables `KEENYSPACE_ADMIN_API_ENABLED=1` for its own run and downloads the
tarball to the host filesystem BEFORE wiping the volumes (a tarball left inside a
container or volume would be destroyed along with it). Expected final output:

```
Backup-restore drill PASSED - 1 workspace(s) intact
```

(The workspace count reflects whatever was seeded before the backup.) The same script
runs in CI as a release gate, so a release cannot ship with a broken backup/restore
path.

## Security note

Keep `KEENYSPACE_ADMIN_API_ENABLED` OFF in normal production operation. The flag is
per-operation: enable it for a backup or restore, then remove it and restart. The
admin endpoints stream your entire dataset to (or overwrite it from) any member of the
admin group, including through that member's API keys — there is no reason to leave
that surface mounted between backups. Keep the admin group small.

The admin API also carries `POST /v1/admin/api-keys/revoke-all`, used when offboarding a
user; see "Offboarding a user" in [docs/oidc-authentik-setup.md](oidc-authentik-setup.md).
