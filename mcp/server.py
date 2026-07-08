"""
Minimal, auditable Matrix MCP server.

Exposes read/send access to Matrix rooms as MCP tools over Streamable HTTP.
Security model:
  - Every request must carry `Authorization: Bearer <MCP_BEARER>` (constant-time check).
  - Acts on Matrix using a dedicated, revocable device token (device_id=mcp-bot).
  - Simple global rate limit + audit logging (actions + room ids, never message bodies).
  - Binds only to the internal docker network; Caddy is the sole TLS ingress.
"""
import os
import hmac
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mcp-matrix")

HS = os.environ["MATRIX_HS"].rstrip("/")
MATRIX_TOKEN = os.environ["MATRIX_TOKEN"]
MCP_BEARER = os.environ["MCP_BEARER"]
EXPECTED_AUTH = f"Bearer {MCP_BEARER}"

POKE_USER_ID = os.environ.get("POKE_USER_ID", "").strip()
RL_MAX = int(os.environ.get("RATE_LIMIT_MAX", "120"))
RL_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))
_hits: deque = deque()

mx = httpx.AsyncClient(
    base_url=HS,
    headers={"Authorization": f"Bearer {MATRIX_TOKEN}"},
    timeout=30.0,
)

PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "matrix.hanzpo.com")
_sec = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[PUBLIC_HOST],
    allowed_origins=[f"https://{PUBLIC_HOST}"],
)
mcp = FastMCP("matrix", transport_security=_sec)


def enc(s: str) -> str:
    return quote(s, safe="")


def _txn() -> str:
    return f"mcp{int(time.time() * 1000)}"


def _localpart(mxid: str) -> str:
    return mxid[1:].split(":", 1)[0] if mxid.startswith("@") else mxid


def _iso(ts) -> str | None:
    """Epoch-ms -> human-readable UTC ISO timestamp."""
    if not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_reply_fallback(body: str) -> str:
    """Matrix prefixes reply bodies with quoted `> ...` lines; drop them."""
    if not body:
        return body
    lines = body.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    return "\n".join(lines[i:]) if i else body


_self_id: str | None = None


async def get_self_id() -> str:
    global _self_id
    if _self_id is None:
        r = await mx.get("/_matrix/client/v3/account/whoami")
        r.raise_for_status()
        _self_id = r.json().get("user_id", "")
    return _self_id


async def members_map(room_id: str) -> dict:
    """Return {mxid: display_name} for a room's joined members."""
    try:
        r = await mx.get(f"/_matrix/client/v3/rooms/{enc(room_id)}/joined_members")
        if r.status_code == 200:
            return {
                mxid: (info.get("display_name") or _localpart(mxid))
                for mxid, info in r.json().get("joined", {}).items()
            }
    except Exception:
        pass
    return {}


@mcp.tool()
async def whoami() -> dict:
    """Return the Matrix user id and display name this server acts as ('me')."""
    r = await mx.get("/_matrix/client/v3/account/whoami")
    r.raise_for_status()
    uid = r.json().get("user_id", "")
    name = None
    try:
        pr = await mx.get(f"/_matrix/client/v3/profile/{enc(uid)}/displayname")
        if pr.status_code == 200:
            name = pr.json().get("displayname")
    except Exception:
        pass
    return {"user_id": uid, "display_name": name or _localpart(uid)}


@mcp.tool()
async def list_rooms() -> dict:
    """
    List every Matrix room the account is in. Returns {count, rooms}.
    Each room: room_id, name, is_dm (true if a 1:1 chat), other_members (display
    names besides you). Use room_id with get_messages / send_message.
    """
    r = await mx.get("/_matrix/client/v3/joined_rooms")
    r.raise_for_status()
    ids = r.json().get("joined_rooms", [])[:300]
    me = await get_self_id()

    async def describe(rid: str):
        name = None
        try:
            nr = await mx.get(f"/_matrix/client/v3/rooms/{enc(rid)}/state/m.room.name")
            if nr.status_code == 200:
                name = nr.json().get("name")
        except Exception:
            pass
        members = await members_map(rid)
        others = [n for mx_id, n in members.items() if mx_id != me]
        return {
            "room_id": rid,
            "name": name or (others[0] if len(others) == 1 else None),
            "is_dm": len(members) == 2,
            "other_members": others[:20],
        }

    rooms = await asyncio.gather(*[describe(r) for r in ids])
    log.info("list_rooms n=%d", len(ids))
    return {"count": len(rooms), "rooms": list(rooms)}


@mcp.tool()
async def get_messages(room_id: str, limit: int = 30) -> dict:
    """
    Read recent messages in a room, oldest->newest. Returns {room_id, count, messages}. limit 1-100.
    Each message includes:
      event_id, time (UTC ISO), sender (display name), sender_id (matrix id),
      is_me (true if sent by you), text (clean body), type,
      reply_to {event_id, sender, text} when it replies to another message,
      thread_root when it belongs to a thread.
    Use event_id as reply_to_event_id in send_message to reply to a message.
    """
    limit = max(1, min(int(limit), 100))
    r = await mx.get(
        f"/_matrix/client/v3/rooms/{enc(room_id)}/messages",
        params={"dir": "b", "limit": limit},
    )
    r.raise_for_status()
    chunk = r.json().get("chunk", [])
    me = await get_self_id()
    names = await members_map(room_id)
    by_id = {ev.get("event_id"): ev for ev in chunk}

    def disp(mxid: str) -> str:
        return names.get(mxid) or _localpart(mxid or "")

    async def resolve_reply(target_id: str):
        ev = by_id.get(target_id)
        if ev is None:
            try:
                er = await mx.get(f"/_matrix/client/v3/rooms/{enc(room_id)}/event/{enc(target_id)}")
                if er.status_code == 200:
                    ev = er.json()
            except Exception:
                ev = None
        if not ev:
            return {"event_id": target_id}
        c = ev.get("content", {})
        body = (c.get("m.new_content") or c).get("body", "")
        text = _strip_reply_fallback(body)
        return {
            "event_id": target_id,
            "sender": disp(ev.get("sender")),
            "text": (text[:200] + "…") if len(text) > 200 else text,
        }

    out = []
    for ev in chunk:
        if ev.get("type") != "m.room.message":
            continue
        c = ev.get("content", {})
        rel = c.get("m.relates_to", {}) or {}
        new_content = c.get("m.new_content")
        src = new_content if new_content else c
        text = _strip_reply_fallback(src.get("body", "") or "")
        sender = ev.get("sender", "")
        msg = {
            "event_id": ev.get("event_id"),
            "time": _iso(ev.get("origin_server_ts")),
            "sender": disp(sender),
            "sender_id": sender,
            "is_me": sender == me,
            "text": text,
            "type": src.get("msgtype"),
        }
        if new_content:
            msg["edited"] = True
        in_reply = (rel.get("m.in_reply_to") or {}).get("event_id")
        if in_reply and rel.get("rel_type") != "m.replace":
            msg["reply_to"] = await resolve_reply(in_reply)
        if rel.get("rel_type") == "m.thread" and rel.get("event_id"):
            msg["thread_root"] = rel["event_id"]
        out.append(msg)

    out.reverse()
    log.info("get_messages room=%s n=%d", room_id, len(out))
    return {"room_id": room_id, "count": len(out), "messages": out}


@mcp.tool()
async def send_message(room_id: str, body: str, reply_to_event_id: str | None = None) -> dict:
    """
    Send a plain-text message to a room. Returns {event_id}.
    Pass reply_to_event_id (an event_id from get_messages) to reply to a
    specific message so the thread of conversation stays clear.
    """
    content = {"msgtype": "m.text", "body": body}
    if reply_to_event_id:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to_event_id}}
    r = await mx.put(
        f"/_matrix/client/v3/rooms/{enc(room_id)}/send/m.room.message/{_txn()}",
        json=content,
    )
    r.raise_for_status()
    log.info("send_message room=%s bytes=%d reply=%s", room_id, len(body), bool(reply_to_event_id))
    return r.json()


class Guard(BaseHTTPMiddleware):
    """Bearer-token auth + global rate limit on every request before it reaches MCP."""

    async def dispatch(self, request, call_next):
        now = time.time()
        while _hits and now - _hits[0] > RL_WINDOW:
            _hits.popleft()
        if len(_hits) >= RL_MAX:
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        _hits.append(now)

        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth, EXPECTED_AUTH):
            client = request.client.host if request.client else "?"
            log.warning("unauthorized request from %s path=%s", client, request.url.path)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Second factor: requests must come from the pinned Poke account.
        if POKE_USER_ID:
            poke_uid = request.headers.get("x-poke-user-id", "")
            if not hmac.compare_digest(poke_uid, POKE_USER_ID):
                client = request.client.host if request.client else "?"
                log.warning("poke-id mismatch from %s uid=%r", client, poke_uid)
                return JSONResponse({"error": "forbidden"}, status_code=403)
        else:
            poke_uid = request.headers.get("x-poke-user-id")
            if poke_uid:
                log.info("POKE_USER_ID=%s path=%s", poke_uid, request.url.path)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(Guard)
