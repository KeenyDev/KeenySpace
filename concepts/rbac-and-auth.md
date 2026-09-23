# Auth

Authentication для KeenySpace вдохновлён Anthropic API: OIDC для людей + scoped API keys для программного доступа. **Authorization** (роли, ACL, permissions) — делегирована выбранному IdP; финальный design фиксируется после выбора провайдера в отдельной итерации спеки. В этом проходе фиксируем только authentication.

## Authentication surfaces

### OIDC (interactive)

User flow:
1. Client запускает `keenyspace login` → открывается локальный browser tab.
2. Browser → server `/v1/api/auth/login` → redirect к OIDC IdP.
3. IdP → server `/v1/api/auth/callback?code=...` → server валидирует с IdP, получает JWT.
4. Server создаёт session, возвращает session token клиенту.
5. Client кладёт token в platform-appropriate config dir с restricted permissions (mode 0600).

Refresh: standard OAuth refresh; client решает когда обновляться, server валидирует.

Поддерживается любой standards-compliant OIDC provider (Ory, Keycloak, Authentik, Zitadel, Auth0 — выбор провайдера ближе к делу).

### API keys (programmatic)

Format: prefix + entropy (например `ks_live_<base64-random>`, аналог Anthropic). Generated на сервере, показывается один раз в момент создания.

Headers: `Authorization: Bearer ks_live_...`.

Каждый key привязан к user (или service identity) и имеет scope. Конкретные scopes — после выбора authz layer.

### MCP authentication

MCP tools используют тот же server, тот же authn middleware. MCP клиент шлёт JWT или API key в header.

## Authorization (deferred)

Authorization — роли, ACL, group mappings — делегирована выбранному IdP. Конкретный design (mapping IdP groups → workspace permissions, custom roles, audit) фиксируется после выбора провайдера.

В рамках текущей спеки достаточно знать:
- Auth flow существует (см. выше).
- Каждый запрос (HTTP, MCP) проходит через authn middleware и проверяется на authz перед попаданием в FS-write слой (defense-in-depth).
- ACL хранятся в Postgres вместе с workspace registry, не в yaml-файлах workspace.

## Trust model

- **Self-hosted = server admin доверенный**. SSH-доступ к диску = bypass authz. Норма для self-hosted софта (Postgres, Outline, GitLab).
- **Server-to-IdP**: KeenySpace доверяет JWT signed правильным IdP. Конфиг IdP — admin-only.
- **Client-to-server**: TLS managed снаружи (nginx, Caddy, Traefik). Server сам не терминирует TLS в default config.

## Open questions

См. [open-questions.md](../open-questions.md) — authorization design в полном объёме отложен.
