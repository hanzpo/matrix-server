#!/usr/bin/env bash
# Mint a dedicated, revocable Matrix access token for the MCP server.
# Run interactively so your password is never stored or echoed:
#   sudo bash /opt/matrix/mcp/mint-token.sh
set -euo pipefail

ENV_FILE="/opt/matrix/mcp/.env"
HS="http://127.0.0.1:8008"
LOCALPART="hanz"

read -rsp "Password for @${LOCALPART}:hanzpo.com: " PW; echo
PAYLOAD=$(PW="$PW" python3 -c 'import json,os;print(json.dumps({"type":"m.login.password","identifier":{"type":"m.id.user","user":"'"$LOCALPART"'"},"password":os.environ["PW"],"device_id":"mcp-bot","initial_device_display_name":"MCP bridge (bot)"}))')
unset PW

RESP=$(curl -s -X POST "$HS/_matrix/client/v3/login" -H 'Content-Type: application/json' -d "$PAYLOAD")
TOKEN=$(printf '%s' "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("access_token",""))')
[ -n "$TOKEN" ] || { echo "LOGIN FAILED: $RESP"; exit 1; }

touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
sed -i '/^MATRIX_TOKEN=/d' "$ENV_FILE"
printf 'MATRIX_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "OK: token stored in $ENV_FILE (device_id=mcp-bot), length ${#TOKEN}"
