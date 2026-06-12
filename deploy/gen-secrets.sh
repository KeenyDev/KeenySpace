#!/usr/bin/env bash
set -euo pipefail
umask 077
ENV_FILE="$(dirname "$0")/.env"
if [[ -f "$ENV_FILE" ]]; then
  echo ".env already exists - delete it first to regenerate"
  exit 1
fi
gen() { openssl rand -hex "$1"; }
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(gen 32)
AUTHENTIK_DB_PASSWORD=$(gen 32)
AUTHENTIK_SECRET_KEY=$(gen 50)
AUTHENTIK_BOOTSTRAP_PASSWORD=$(gen 16)
AUTHENTIK_BOOTSTRAP_TOKEN=$(gen 32)
KEENYSPACE_OIDC_CLIENT_SECRET=$(gen 32)
KEENYSPACE_SESSION_SECRET_KEY=$(gen 32)
KEENYSPACE_API_KEY_PEPPER=$(gen 32)
EOF
chmod 600 "$ENV_FILE"
echo ".env written (mode 600)"
