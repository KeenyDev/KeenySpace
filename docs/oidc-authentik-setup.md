# OIDC Authentik Setup

Status: Placeholder for v0.1.0 alpha (Phase 7 DEP-06 will finalize).

## What this doc covers (Phase 7 scope)

- Authentik OAuth2 provider configuration:
  - Application + Provider creation
  - `redirect_uri` = `https://<keenyspace-host>/v1/api/auth/callback`
  - `post_logout_redirect_uri` = `https://<keenyspace-host>/`
  - Scopes claim mapping: `openid` + `profile` + `email` + `groups`
- Device-code provider (AUTH-05; required by Phase 5 CLI `keenyspace login` headless flow):
  - Separate provider config in Authentik (distinct from interactive OAuth2 provider)
  - Same audience + scope set as the interactive provider — without this CLI tokens
    will NOT validate against KeenySpace middleware
  - URL: `/application/o/device/`
  - Reference: https://docs.goauthentik.io/add-secure-apps/providers/oauth2/device_code/
- KeenySpace env vars (`KEENYSPACE_AUTH__OIDC_*`):
  - `OIDC_ISSUER_URL` — Authentik application discovery URL prefix
  - `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`
  - `OIDC_REDIRECT_URI`, `OIDC_POST_LOGOUT_REDIRECT_URI`

## v1 Notes

- KeenySpace is OIDC-protocol-neutral; Authentik is the reference IdP in v0.1.0 alpha.
- Device-code path is delegated to Authentik entirely; KeenySpace ships zero new
  endpoints for device-code in v1 (CONTEXT D-14). Phase 5 CLI calls Authentik
  `/application/o/device/` directly per RFC 8628.
- Authentik device-code provider must emit tokens with the same `audience` and
  `scope` set as the interactive provider — otherwise CLI-minted tokens will
  not validate against KeenySpace middleware.

## TODO (Phase 7)

- [ ] Step-by-step screenshots for Authentik admin UI
- [ ] Group-claim property mapping verification (`groups` scope) — Phase 4/v1.5
      forward-compat
- [ ] Backup / restore drill for Authentik config alongside KeenySpace backup
