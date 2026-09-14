#!/usr/bin/env python3
"""Notify a Matrix room about new SWE/quant internship postings, deduped
across sources and grouped by company tier.

Sources: speedyapply lists, Simplify listings.json, vanshb03 list, the NUFT
quant list, and direct polling of ~70 company job boards (companies.json).

Config/state in ~/swejobs/: bot_token, room_id, state.json, notifier.log.

Usage: notifier.py [--dry-run]   (dry-run: fetch + diff, no sends, no state write)
"""
import html
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources
from tiers import is_tier1

import os

HS = "http://127.0.0.1:8008"
BASE = Path(os.environ.get("SWEJOBS_HOME", Path.home() / "swejobs"))
STATE_PATH = BASE / "state.json"
COMPANIES_PATH = Path(__file__).resolve().parent / "companies.json"
MAX_ITEMS_PER_MSG = 40
RENOTIFY_DAYS = 14   # a listing that flaps off/on within this window is silent
DIGEST_ET_HOUR = 20  # last run of the cron window flushes the daily digest

DRY = "--dry-run" in sys.argv

TRACKING_PARAMS_PREFIXES = ("utm_", "lever-", "gh_src", "ref", "src", "source")


def log(msg):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {msg}"
    if DRY:
        print(line)
    else:
        with open(BASE / "notifier.log", "a") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------- dedupe keys

def canonical_key(url):
    """Stable identity for a posting URL. Prefers ATS job ids (the same job
    linked from N lists always shares them); falls back to the URL with
    tracking params stripped."""
    try:
        p = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(p.query)
        if "gh_jid" in q:
            return f"gh:{q['gh_jid'][0]}"
        host = p.netloc.lower()
        parts = [s for s in p.path.split("/") if s]
        if "greenhouse.io" in host and "jobs" in parts:
            i = parts.index("jobs")
            if i + 1 < len(parts) and parts[i + 1].isdigit():
                return f"gh:{parts[i + 1]}"
        if "lever.co" in host and len(parts) >= 2:
            return f"lever:{parts[1]}"
        if "ashbyhq.com" in host and parts:
            return f"ashby:{parts[-1]}"
        if "myworkdayjobs.com" in host and parts:
            return f"wd:{host.split('.')[0]}:{parts[-1]}"
        kept = {k: v for k, v in q.items()
                if not any(k.lower().startswith(x) for x in TRACKING_PARAMS_PREFIXES)}
        query = urllib.parse.urlencode(sorted(kept.items()), doseq=True)
        return f"url:{host}{p.path.rstrip('/')}" + (f"?{query}" if query else "")
    except ValueError:
        return f"url:{url}"


def identity_key(posting):
    from tiers import norm_company
    title = " ".join(posting["title"].lower().split())
    return f"{norm_company(posting['company'])}|{title}"


# ------------------------------------------------------------------- fetching

def collect_sources(state):
    """Fetch every source. Returns (current, fetched_ok, postings_by_key):
    current maps source name -> set of keys it lists right now; a source that
    failed this run is absent from fetched_ok and keeps its cached keys."""
    etags = state.setdefault("etags", {})
    cached = state.setdefault("source_keys", {})
    known_sources = set(cached)  # sources seen on a previous run
    current, fetched_ok, postings = {}, set(), {}

    def ingest(name, rows):
        keys = set()
        for r in rows:
            k = canonical_key(r["url"])
            keys.add(k)
            postings.setdefault(k, r)
        current[name] = sorted(keys)
        fetched_ok.add(name)

    repo_raw = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/"
    list_specs = [
        (s, repo_raw + path, sources.parse_speedyapply, s)
        for path, s in sources.SPEEDYAPPLY_FILES
    ] + [
        ("simplify", sources.SIMPLIFY_URL, sources.parse_simplify, None),
        ("vanshb03", sources.VANSH_URL, sources.parse_vansh, None),
        ("nuft-quant", sources.NUFT_URL, sources.parse_nuft, None),
    ]
    for name, url, parser, parser_arg in list_specs:
        try:
            text, etag = sources.fetch(url, etags.get(name))
            if text is None:  # 304: content unchanged since last parse
                current[name] = cached.get(name, [])
                fetched_ok.add(name)
                continue
            rows = parser(text, parser_arg) if parser_arg else parser(text)
            if not rows:
                log(f"{name}: parsed 0 rows; skipping (format change?)")
                continue
            etags[name] = etag
            ingest(name, rows)
        except Exception as e:
            log(f"{name}: fetch failed: {e}")

    try:
        companies = json.loads(COMPANIES_PATH.read_text())
    except Exception as e:
        log(f"companies.json unreadable: {e}")
        companies = []
    for c in companies:
        name = f"ats:{c['slug']}"
        try:
            rows, etag = sources.fetch_ats_board(
                c["name"], c["ats"], c["slug"], etags.get(name))
            if rows is None:
                current[name] = cached.get(name, [])
                fetched_ok.add(name)
            else:
                etags[name] = etag
                ingest(name, rows)  # 0 intern rows is a valid answer here
            time.sleep(0.2)
        except Exception as e:
            log(f"{name}: fetch failed: {e}")

    for name in fetched_ok:
        cached[name] = current[name]
    new_sources = fetched_ok - known_sources
    return current, fetched_ok, new_sources, postings


# ------------------------------------------------------------------ state I/O

def load_state():
    if not STATE_PATH.exists():
        return {"version": 2, "jobs": {}, "etags": {}, "source_keys": {}}
    state = json.loads(STATE_PATH.read_text())
    if "version" not in state:  # v1: {file_path: {url: row}}
        jobs = {}
        now = time.strftime("%Y-%m-%d")
        for path, by_url in state.items():
            src = {"README.md": "speedyapply-usa"}.get(path, f"speedyapply:{path}")
            for url, row in by_url.items():
                k = canonical_key(url)
                jobs.setdefault(k, {
                    "company": row["company"], "title": row["position"],
                    "location": row["location"], "salary": row["salary"],
                    "url": url, "sources": [src], "status": "active",
                    "first_seen": now, "removed_at": "",
                })
        state = {"version": 2, "jobs": jobs, "etags": {}, "source_keys": {}}
        log(f"migrated v1 state: {len(jobs)} jobs")
    state.pop("sheet_queue", None)  # sheet sync removed
    return state


# ------------------------------------------------------------------- diffing

def _days_since(datestr):
    try:
        return (time.time() - time.mktime(time.strptime(datestr, "%Y-%m-%d"))) / 86400
    except (ValueError, OverflowError):
        return 10 ** 6


def apply_run(state, current, fetched_ok, new_sources, postings):
    """Merge this run's observations into state. Returns (added_keys,
    removed_keys, seeded): keys of jobs to notify about; seeded counts jobs
    introduced silently because every source listing them is new this run.
    A job that reappears within RENOTIFY_DAYS of removal (same URL or same
    company+title) is reactivated silently — flapping, not news."""
    jobs = state["jobs"]
    now = time.strftime("%Y-%m-%d")
    listed_by = {}  # key -> [source names listing it now]
    for src, keys in current.items():
        for k in keys:
            listed_by.setdefault(k, []).append(src)

    ident_index = {}  # identity -> key, for active and recently removed jobs
    for k, j in jobs.items():
        if j["status"] == "active" or _days_since(j["removed_at"]) <= RENOTIFY_DAYS:
            ident_index.setdefault(
                identity_key({"company": j["company"], "title": j["title"]}), k)

    def reactivate(k, j):
        fresh_news = _days_since(j["removed_at"]) > RENOTIFY_DAYS
        j["status"], j["removed_at"] = "active", ""
        return fresh_news

    added, seeded = [], 0
    for k, srcs in listed_by.items():
        p = postings.get(k)
        if k in jobs:
            j = jobs[k]
            j["sources"] = sorted(set(j["sources"]) | set(srcs))
            if p:
                for field in ("location", "salary"):
                    if p.get(field) and not j.get(field):
                        j[field] = p[field]
            if j["status"] == "removed" and reactivate(k, j):
                added.append(k)
            continue
        if p is None:
            continue  # key known only from a 304 cache; already in jobs or noise
        ik = identity_key(p)
        alias = ident_index.get(ik)
        if alias:  # same company+title under a different URL: merge, don't renotify
            j = jobs[alias]
            j["sources"] = sorted(set(j["sources"]) | set(srcs))
            if j["status"] == "removed":
                reactivate(alias, j)  # recently removed: silent by definition
            continue
        j = {"company": p["company"], "title": p["title"],
             "location": p["location"], "salary": p["salary"], "url": p["url"],
             "sources": sorted(set(srcs)), "status": "active",
             "first_seen": now, "removed_at": ""}
        jobs[k] = j
        ident_index[ik] = k
        if set(srcs) <= new_sources:
            seeded += 1  # first sync of a new source: record, don't notify
        else:
            added.append(k)

    removed = []
    for k, j in jobs.items():
        if j["status"] != "active" or k in listed_by:
            continue
        # Only declare it gone if every source that carried it reported in.
        if all(s in fetched_ok for s in j["sources"]):
            j["status"], j["removed_at"] = "removed", now
            removed.append(k)
    return added, removed, seeded


# ---------------------------------------------------------------- matrix send

def send_matrix(text, formatted):
    if DRY:
        print("--- would send ---")
        print(text)
        return
    token = (BASE / "bot_token").read_text().strip()
    room = (BASE / "room_id").read_text().strip()
    body = json.dumps({
        "msgtype": "m.text", "body": text,
        "format": "org.matrix.custom.html", "formatted_body": formatted,
    }).encode()
    txn = f"swejobs{int(time.time() * 1000)}"
    req = urllib.request.Request(
        f"{HS}/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}",
        data=body, method="PUT",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        json.load(resp)


def fmt_item(j):
    parts = [f"<b>{html.escape(j['company'])}</b>", html.escape(j["title"])]
    for f in ("location", "salary"):
        if j.get(f):
            parts.append(html.escape(j[f]))
    return " — ".join(parts) + f' — <a href="{html.escape(j["url"])}">apply</a>'


def fmt_item_text(j):
    parts = [j["company"], j["title"]]
    parts += [j[f] for f in ("location", "salary") if j.get(f)]
    return "• " + " — ".join(parts) + f" — {j['url']}"


def _send_job_list(header, jobs_list):
    for i in range(0, len(jobs_list), MAX_ITEMS_PER_MSG):
        group = jobs_list[i:i + MAX_ITEMS_PER_MSG]
        text = [f"{header}:"]
        htm = [f"<b>{html.escape(header)}</b><ul>"]
        for j in group:
            text.append(fmt_item_text(j))
            htm.append(f"<li>{fmt_item(j)}</li>")
        htm.append("</ul>")
        send_matrix("\n".join(text), "".join(htm))


def notify_tier1(jobs_list):
    """Instant alert, top-tier companies only. Everything else waits for
    the daily digest; removals are never announced (the digest counts them)."""
    if jobs_list:
        _send_job_list(f"{len(jobs_list)} new top-tier posting(s)", jobs_list)


def flush_digest(state):
    """Once a day (the DIGEST_ET_HOUR run), summarize the below-tier adds
    and the count of closed listings, then clear the queue."""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    if datetime.now(ZoneInfo("America/New_York")).hour < DIGEST_ET_HOUR:
        return
    jobs = state["jobs"]
    items = [jobs[k] for k in dict.fromkeys(state.get("digest_queue", []))
             if k in jobs and jobs[k]["status"] == "active"]
    closed = state.get("digest_removed", 0)
    if items or closed:
        closed_note = f" · {closed} listing(s) closed" if closed else ""
        _send_job_list(
            f"Daily digest — {len(items)} new posting(s) below tier 1{closed_note}",
            items)
        log(f"digest flushed: {len(items)} below-tier, {closed} closed")
    state["digest_queue"], state["digest_removed"] = [], 0


# ---------------------------------------------------------------------- main

def main():
    state = load_state()
    current, fetched_ok, new_sources, postings = collect_sources(state)
    if not fetched_ok:
        log("no source fetched successfully; aborting run")
        return 1
    added, removed, seeded = apply_run(
        state, current, fetched_ok, new_sources, postings)

    if seeded:
        log(f"seeded {seeded} jobs silently from new sources: "
            f"{', '.join(sorted(new_sources))[:200]}")
        send_matrix(
            f"Now tracking {len(new_sources)} new source(s): {seeded} existing "
            "listings added quietly. New ones will be announced from here on.",
            f"Now tracking <b>{len(new_sources)}</b> new source(s): {seeded} "
            "existing listings added quietly. New ones will be announced "
            "from here on.",
        )
    jobs = state["jobs"]
    tier1_now = [jobs[k] for k in added if is_tier1(jobs[k]["company"])]
    state.setdefault("digest_queue", [])
    state["digest_queue"] += [k for k in added
                              if not is_tier1(jobs[k]["company"])]
    state["digest_removed"] = state.get("digest_removed", 0) + len(removed)
    if added or removed:
        log(f"+{len(added)} ({len(tier1_now)} tier1) -{len(removed)} "
            f"(sources ok: {len(fetched_ok)})")
        notify_tier1(tier1_now)
    flush_digest(state)

    if not DRY:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATE_PATH)
    else:
        print(f"[dry-run] +{len(added)} -{len(removed)}, "
              f"{len(state['jobs'])} jobs tracked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
