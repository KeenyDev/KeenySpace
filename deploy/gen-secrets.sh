#!/usr/bin/env bash
# Generate the secrets docker-compose.yml requires into deploy/.env.
# Idempotent: keys already present in .env are left untouched; only missing
# keys are appended. Never overwrites existing values.
#
# Existing installs: POSTGRES_PASSWORD and AUTHENTIK_DB_PASSWORD / AUTHENTIK_SECRET_KEY
# are baked into the existing volumes. Put your current values into .env BEFORE
# running this script, otherwise freshly generated values will not match the data.
set -euo pipefail
umask 077

ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"

KEYS=(
  "POSTGRES_PASSWORD 32"
  "AUTHENTIK_DB_PASSWORD 32"
  "AUTHENTIK_SECRET_KEY 50"
  "AUTHENTIK_BOOTSTRAP_PASSWORD 16"
  "AUTHENTIK_BOOTSTRAP_TOKEN 32"
  "KEENYSPACE_OIDC_CLIENT_SECRET 32"
  "KEENYSPACE_SESSION_SECRET_KEY 32"
  "KEENYSPACE_API_KEY_PEPPER 32"
)

command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

if [[ ! -e "$ENV_FILE" ]]; then
  : > "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

if [[ -s "$ENV_FILE" && -n "$(tail -c1 "$ENV_FILE")" ]]; then
  printf '\n' >> "$ENV_FILE"
fi

added=()
for entry in "${KEYS[@]}"; do
  read -r key bytes <<< "$entry"
  if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE"; then
    continue
  fi
  printf '%s=%s\n' "$key" "$(openssl rand -hex "$bytes")" >> "$ENV_FILE"
  added+=("$key")
done

if [[ ${#added[@]} -eq 0 ]]; then
  echo "$ENV_FILE already has all required secrets; nothing changed"
else
  echo "Added to $ENV_FILE (mode 600): ${added[*]}"
fi
