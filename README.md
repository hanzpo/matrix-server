# matrix-server

Self-hosted [tuwunel](https://github.com/matrix-construct/tuwunel) Matrix homeserver as a
personal Beeper alternative — single user, federation disabled, purpose is chat **bridges**
(Signal / Discord) plus a hardened **MCP server** that lets an AI bot (Poke)
read and send messages across all rooms.

Deployed as a Docker Compose stack, intended to live at `/opt/matrix` on the server.

> **This repo is config-as-code, not a backup.** All secrets and runtime state
> (tokens, signing keys, databases, TLS certs) are excluded or redacted to
> `.example` templates. Real values are generated/filled in at deploy time and
> are `.gitignore`d.

## Stack

| Service | Role |
|---|---|
| `tuwunel` | Matrix homeserver (Rust) |
| `caddy` | TLS reverse proxy (auto Let's Encrypt); also routes the MCP secret path |
| `mautrix-signal` / `-discord` | Chat bridges |
| `grindstone` | Interview-prep app ([hanzpo/grindstone](https://github.com/hanzpo/grindstone)), served at `study.hanzpo.com` behind Caddy basic auth |
| `mcp-matrix` | Custom MCP server exposing Matrix read/send to a remote AI bot |

## Layout

```
docker-compose.yml
tuwunel/tuwunel.toml
caddy/Caddyfile.example          # secret path + basic_auth placeholders
mcp/                             # the MCP server (server.py, Dockerfile, mint-token.sh, .env.example)
bridges/<name>/*.example         # redacted bridge config + registration templates
setup.sh
```

## Grindstone

The `grindstone` service runs the image built by the
[grindstone](https://github.com/hanzpo/grindstone) repo's `deploy/` scripts.
On the box it expects `/opt/grindstone/env` (holds `ANTHROPIC_API_KEY`) and
persists its SQLite DB in `/opt/grindstone/data`. Build + load the image on the
server, then `docker compose up -d grindstone`.

## Quick start

```bash
git clone <this repo> /opt/matrix && cd /opt/matrix
./setup.sh            # generates caddy secret path + mcp bearer, starts tuwunel + caddy
```

Then follow the manual steps `setup.sh` prints (DNS, create your user, mint the MCP
token, start the bridges and log in). Bridges are interactive by nature (Signal QR link,
Discord token) so they can't be fully scripted.

## The MCP server (`mcp/`)

A ~200-line Python server (FastMCP + httpx) exposing four tools —
`whoami`, `list_rooms`, `get_messages`, `send_message` — that act as your Matrix
account via a dedicated, revocable device token.

**Security model (defense in depth):**
- TLS (Caddy) + strict `Authorization: Bearer <MCP_BEARER>` (constant-time check)
- Unguessable secret URL path (Caddy strips it before proxying)
- Pinned `X-Poke-User-Id` second factor (`POKE_USER_ID`)
- Dedicated `mcp-bot` device token — revoke it alone without touching your password
- Container has **no internet egress** (internal-only docker network; can reach only tuwunel),
  runs non-root / read-only-fs / all caps dropped / memory-capped
- Global rate limit + audit logging (`docker logs matrix-mcp-matrix-1`)

Point the bot at `https://<PUBLIC_HOST>/<secret-path>/mcp` with the bearer token as its API key.

### Handy alias

```bash
mcp() {
  local C="sudo docker compose -f /opt/matrix/docker-compose.yml"
  case "$1" in
    start) $C start mcp-matrix ;; stop) $C stop mcp-matrix ;;
    restart) $C restart mcp-matrix ;;
    status) sudo docker ps -a --filter name=mcp-matrix --format "{{.Names}}: {{.Status}}" ;;
    logs) sudo docker logs --tail 50 -f matrix-mcp-matrix-1 ;;
    *) echo "usage: mcp {start|stop|restart|status|logs}" ;;
  esac
}
```

### Kill switch

```bash
docker compose stop mcp-matrix     # take the endpoint offline
# or revoke Matrix access alone by deleting the `mcp-bot` device in a Matrix client
```

## Rotating MCP secrets

Regenerate `MCP_BEARER` in `mcp/.env` and the `handle_path` secret in `caddy/Caddyfile`,
then `docker compose up -d mcp-matrix && docker exec matrix-caddy-1 caddy reload --config /etc/caddy/Caddyfile`.
