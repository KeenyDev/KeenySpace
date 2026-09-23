---
name: authentik-facts
description: Verified Authentik 2026.2 behaviours (token shapes, scope mappings, blueprint semantics) and the live-apply trap of the bind-mounted blueprint dir
metadata:
  type: reference
---

Verified 2026-09-22 by reading source inside the running `deploy-authentik-1` container (read-only `docker exec ... cat /authentik/...`; context7 was not available to the agent):

- Access token = ID token claims + `azp`, `uid`, `scope` (setdefault) — `IDToken.to_access_token` in providers/oauth2/id_token.py. The ID token (`to_jwt`) has no `scope`; nonce is present in BOTH for the code flow. No `typ: at+jwt` header. KeenySpace rejects bearer JWTs without a string `scope` claim on that basis.
- Scope mappings are applied only for scopes the client requested (`UserInfoView.get_claims` filters by `token.scope`), for every grant incl. client_credentials. The groups claim needs `groups` in the requested scope.
- Blueprint `state: present` on an existing object = partial serializer update (only listed attrs), but an M2M attr like user `groups` replaces the whole set. `!Find [model, [field, value], [field2, value2]]` ANDs Django lookups (e.g. `groups__name`) and returns pk or None; usable in `conditions:`.
- Recreating a password-less `akadmin` would reopen the OOBE initial-setup flow — never let a blueprint create it.

**Trap:** `deploy/authentik/blueprints/` is bind-mounted into the dev Authentik (worker watches it), so editing a blueprint file applies it to the user's live dev IdP within seconds. Say so in the report whenever those files change.

Settings quirk: `Settings` uses `env_ignore_empty=True`, so a setting cannot be set to `""` via env — the default wins.

**real_idp lane is unsafe to run on this machine while the dev stack is up:** `tests/integration/test_real_authentik_e2e.py` builds `DockerCompose(context=deploy/, ...)` with no project name, so compose uses project `deploy` — the same project as the user's live `deploy-*` containers — and pins host port 9000 (held by the dev Authentik). Running it would reconfigure and then `down` the dev stack. Only run it with the dev stack stopped or after the fixture sets an isolated project name.
