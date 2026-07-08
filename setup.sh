#!/usr/bin/env bash
# Bootstrap the core stack. Run from the repo root (intended location: /opt/matrix).
# Interactive bits (DNS, user creation, bridge logins, MCP token) are printed at the end.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Matrix server setup"
command -v docker >/dev/null || { echo "ERROR: install Docker first"; exit 1; }
docker compose version >/dev/null || { echo "ERROR: need docker compose v2"; exit 1; }

# 1. Caddy config with a fresh secret path for the MCP endpoint
if [ ! -f caddy/Caddyfile ]; then
  SP=$(openssl rand -hex 16)
  sed "s/CHANGEME_SECRET_PATH/${SP}/" caddy/Caddyfile.example > caddy/Caddyfile
  echo "==> caddy/Caddyfile created. MCP secret path: ${SP}"
fi

# 2. MCP env with a freshly generated bearer token
if [ ! -f mcp/.env ]; then
  BEARER=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-43)
  sed "s#CHANGEME_GENERATE_A_LONG_RANDOM_TOKEN#${BEARER}#" mcp/.env.example > mcp/.env
  chmod 600 mcp/.env
  echo "==> mcp/.env created with a fresh MCP_BEARER (chmod 600). Bearer: ${BEARER}"
fi

# 3. Bring up the homeserver + reverse proxy (NOT the MCP container yet — it needs a token)
echo "==> starting tuwunel + caddy"
docker compose up -d tuwunel caddy

cat <<'NOTE'

==> Core stack is up. Remaining MANUAL steps (can't be scripted):

  1. DNS      : point your matrix.<domain> A record at this server's public IP.
  2. User     : registration is disabled — create your @user via the tuwunel admin
                console (docker compose run with --execute "users create-user ...").
  3. MCP token: bash mcp/mint-token.sh          # logs in as you (device_id=mcp-bot)
                docker compose up -d mcp-matrix  # start MCP once the token exists
  4. Poke pin : after the bot's first request, grab X-Poke-User-Id from
                `docker logs matrix-mcp-matrix-1`, set POKE_USER_ID in mcp/.env, then
                docker compose up -d mcp-matrix
  5. Bridges  : start each mautrix-* container; it generates its own config.yaml +
                registration.yaml. Mirror the settings shown in bridges/*/*.example,
                put the registration in tuwunel's appservice dir, then log in
                (Signal = QR device-link, Discord/LinkedIn = token/cookies).

NOTE
