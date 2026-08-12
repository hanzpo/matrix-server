#!/usr/bin/env python3
"""discord_backfill — slowly page Discord history older than what mautrix bridged.

Runs ON the Matrix homeserver box (that's where the bridge DB + user token live).
For every Discord portal that has a Matrix room, it walks message history
backwards from the oldest already-bridged message, at a randomized human scroll
pace, honouring 429 Retry-After. Output is appended as JSONL in the exact event
schema imsg-tui's parse_matrix_cache() consumes, keyed to the Matrix room id with
synthetic @discord_<id> ghost senders — so it merges cleanly with live data.

Resumable: per-channel progress is checkpointed, so killing/rerunning continues
from where it left off. Read-only against Discord; mirrors the bridge's own API
use, just for older history.

Env knobs (all optional):
  BF_MIN_DELAY / BF_MAX_DELAY   per-request jitter seconds (default 1.4 / 4.5)
  BF_LONG_MIN / BF_LONG_MAX     occasional "human pauses" seconds (default 18/50)
  BF_PER_CHANNEL_MAX            stop a channel after N backfilled msgs (default 100000)
  BF_MAX_REQUESTS              global request cap this run (default 200000)
  BF_UNTIL                     absolute floor YYYY-MM-DD; stop at this date
  BF_AGE_FLOOR_DAYS            relative floor: stop once msgs get older than N days
  BF_ONLY_CHANNELS            comma-separated Discord channel ids to restrict to
"""
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DB_SRC = "/opt/matrix/bridges/discord/mautrix-discord.db"
BF_DIR = "/opt/matrix/backfill"
OUT = os.path.join(BF_DIR, "discord_backfill.jsonl")
STATE = os.path.join(BF_DIR, "state.json")
PROGRESS = os.path.join(BF_DIR, "progress.json")

API = "https://discord.com/api/v10"
KEEP_TYPES = {0, 19}          # DEFAULT, REPLY — the message types that carry content
DISCORD_EPOCH = 1420070400000

MIN_DELAY = float(os.environ.get("BF_MIN_DELAY", "1.4"))
MAX_DELAY = float(os.environ.get("BF_MAX_DELAY", "4.5"))
LONG_MIN = float(os.environ.get("BF_LONG_MIN", "18"))
LONG_MAX = float(os.environ.get("BF_LONG_MAX", "50"))
PER_CHANNEL_MAX = int(os.environ.get("BF_PER_CHANNEL_MAX", "100000"))
MAX_REQUESTS = int(os.environ.get("BF_MAX_REQUESTS", "200000"))
AGE_FLOOR_DAYS = os.environ.get("BF_AGE_FLOOR_DAYS")
UNTIL = os.environ.get("BF_UNTIL")   # absolute floor YYYY-MM-DD; keep msgs on/after it
ONLY = set(x for x in os.environ.get("BF_ONLY_CHANNELS", "").split(",") if x)

age_floor_ms = None
if UNTIL:
    age_floor_ms = datetime.strptime(UNTIL, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp() * 1000
elif AGE_FLOOR_DAYS:
    age_floor_ms = (time.time() - float(AGE_FLOOR_DAYS) * 86400) * 1000


def snowflake_ms(i):
    return (int(i) >> 22) + DISCORD_EPOCH


def human_dt(i):
    return datetime.fromtimestamp(snowflake_ms(i) / 1000, timezone.utc).strftime("%Y-%m-%d")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def open_db():
    tmp = tempfile.mktemp(suffix=".db")
    shutil.copyfile(DB_SRC, tmp)
    for s in ("-wal", "-shm"):
        if os.path.exists(DB_SRC + s):
            shutil.copyfile(DB_SRC + s, tmp + s)
    return sqlite3.connect(tmp)


def discord_get(url, token):
    """GET with retry: 429 honours Retry-After; 5xx backs off; returns json list."""
    attempt = 0
    while True:
        req = urllib.request.Request(url, headers={
            "Authorization": token, "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                body = {}
                try:
                    body = json.load(e)
                except Exception:
                    pass
                wait = float(e.headers.get("Retry-After")
                             or body.get("retry_after") or 5) + random.uniform(0.5, 2.0)
                log(f"  429 rate-limited — sleeping {wait:.1f}s")
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503, 504):
                attempt += 1
                if attempt > 6:
                    raise
                back = min(60, 2 ** attempt) + random.uniform(0, 3)
                log(f"  {e.code} server error — backoff {back:.1f}s")
                time.sleep(back)
                continue
            if e.code in (401, 403):
                raise SystemExit(f"auth failure {e.code}: check the Discord token")
            if e.code == 404:
                return []          # channel gone / no access
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            attempt += 1
            if attempt > 6:
                raise
            back = min(30, 2 ** attempt) + random.uniform(0, 2)
            log(f"  network error ({e}) — backoff {back:.1f}s")
            time.sleep(back)


def nap(req_count):
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    # every ~40-70 requests, take a longer "put the phone down" pause
    if req_count % random.randint(40, 70) == 0:
        pause = random.uniform(LONG_MIN, LONG_MAX)
        log(f"  … human pause {pause:.0f}s")
        time.sleep(pause)


def main():
    os.makedirs(BF_DIR, exist_ok=True)
    db = open_db()
    token = db.execute(
        "SELECT discord_token FROM user WHERE discord_token IS NOT NULL").fetchone()[0]
    my_id = db.execute(
        "SELECT dcid FROM user WHERE dcid IS NOT NULL").fetchone()[0]
    dom_row = db.execute(
        "SELECT sender_mxid FROM message WHERE sender_mxid LIKE '@discord_%' LIMIT 1").fetchone()
    domain = dom_row[0].split(":", 1)[1] if dom_row else "hanzpo.com"
    my_ghost = f"@discord_{my_id}:{domain}"

    channels = db.execute("""
        SELECT p.dcid, p.mxid, p.name, MIN(CAST(m.dcid AS INTEGER)) AS oldest, COUNT(*) AS n
        FROM portal p JOIN message m ON m.dc_chan_id = p.dcid
        WHERE p.mxid IS NOT NULL
        GROUP BY p.dcid ORDER BY n DESC""").fetchall()
    db.close()

    state = load_json(STATE, {})          # dcid -> {"cursor": earliest_fetched_id, "done": bool, "got": n}
    total_written = sum(c.get("got", 0) for c in state.values())
    req_count = 0
    out = open(OUT, "a", encoding="utf-8")
    seen_members = set()                   # (room, ghost) already name-registered

    def emit(obj):
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def register(room, ghost, name):
        k = (room, ghost)
        if k not in seen_members:
            seen_members.add(k)
            emit({"type": "meta_add", "room": room, "members": {ghost: name}})

    emit({"type": "self_ghost", "mxid": my_ghost})
    out.flush()
    log(f"backfilling {len(channels)} channels as {my_ghost}")
    for dcid, mxid, name, oldest, bridged_n in channels:
        if ONLY and str(dcid) not in ONLY:
            continue
        st = state.setdefault(str(dcid), {"cursor": None, "done": False, "got": 0})
        if st["done"]:
            continue
        cursor = st["cursor"] or oldest   # page strictly before the oldest we have
        label = (name or str(dcid))[:32]
        log(f"channel «{label}» (bridged {bridged_n}, oldest {human_dt(oldest)}) "
            f"resuming from {human_dt(cursor)}")
        while True:
            if req_count >= MAX_REQUESTS:
                log("hit global request cap — stopping")
                break
            if st["got"] >= PER_CHANNEL_MAX:
                log(f"  reached per-channel cap {PER_CHANNEL_MAX}")
                st["done"] = True
                break
            url = f"{API}/channels/{dcid}/messages?before={cursor}&limit=100"
            batch = discord_get(url, token)
            req_count += 1
            if not batch:
                st["done"] = True
                break
            batch.sort(key=lambda m: int(m["id"]), reverse=True)  # newest first
            oldest_in_batch = int(batch[-1]["id"])
            wrote = 0
            hit_floor = False
            for m in batch:
                mid = m["id"]
                ts = snowflake_ms(mid)
                if age_floor_ms and ts < age_floor_ms:
                    hit_floor = True
                    continue
                author = m.get("author") or {}
                aid = author.get("id")
                if not aid:
                    continue
                ghost = f"@discord_{aid}:{domain}"
                disp = author.get("global_name") or author.get("username") or aid
                register(mxid, ghost, disp)
                mtype = m.get("type", 0)
                content = m.get("content") or ""
                if mtype in KEEP_TYPES and content:
                    emit({"type": "ev", "id": f"dcbf:{mid}", "room": mxid,
                          "t": ts, "s": ghost, "ty": "m.room.message",
                          "mt": "m.text", "body": content[:4000]})
                    wrote += 1
                if m.get("attachments") or m.get("sticker_items"):
                    emit({"type": "ev", "id": f"dcbf:{mid}:a", "room": mxid,
                          "t": ts, "s": ghost, "ty": "m.room.message",
                          "mt": "m.image"})
                    wrote += 1
                for rc in (m.get("reactions") or []):
                    if rc.get("me"):
                        key = (rc.get("emoji") or {}).get("name") or "?"
                        emit({"type": "ev", "id": f"dcbf:{mid}:r:{key}", "room": mxid,
                              "t": ts, "s": my_ghost, "ty": "m.reaction", "key": key})
            st["got"] += wrote
            total_written += wrote
            cursor = oldest_in_batch
            st["cursor"] = cursor
            out.flush()
            save_state(state, req_count, total_written, label, human_dt(cursor))
            log(f"  +{wrote} (channel {st['got']}, total {total_written}) "
                f"→ back to {human_dt(cursor)}")
            if len(batch) < 100 or hit_floor:
                st["done"] = True
                break
            nap(req_count)
        save_state(state, req_count, total_written, label, human_dt(cursor))
        nap(req_count)

    out.close()
    log(f"DONE — {total_written} messages backfilled across "
        f"{sum(1 for c in state.values() if c.get('got'))} channels, {req_count} requests")


def save_state(state, req_count, total, label, cursor_dt):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)
    with open(PROGRESS, "w") as f:
        json.dump({"updated": datetime.now().isoformat(timespec="seconds"),
                   "requests": req_count, "total_backfilled": total,
                   "current_channel": label, "current_cursor": cursor_dt,
                   "channels_done": sum(1 for c in state.values() if c.get("done"))}, f)


if __name__ == "__main__":
    main()
