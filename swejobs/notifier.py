#!/usr/bin/env python3
"""Notify a Matrix room about new postings in speedyapply/2027-SWE-College-Jobs.

State lives next to this script's config in ~/swejobs/. First run per file
seeds state without spamming; later runs post only added/removed rows.
Rows are keyed by apply URL, so edits to age/formatting never notify.
"""
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO = "speedyapply/2027-SWE-College-Jobs"
BRANCH = "main"
# (path in repo, human label). Add e.g. ("INTERN_INTL.md", "Intl Internships")
# or ("NEW_GRAD_USA.md", "USA New Grad") to track more lists.
FILES = [
    ("README.md", "USA Internships"),
]

HS = "http://127.0.0.1:8008"
BASE = Path.home() / "swejobs"
STATE_PATH = BASE / "state.json"
TOKEN = (BASE / "bot_token").read_text().strip()
ROOM = (BASE / "room_id").read_text().strip()

ROW_RE = re.compile(r"^\|.*\|$")
STRONG_RE = re.compile(r"<strong>(.*?)</strong>")
HREF_RE = re.compile(r'href="([^"]+)"')


def log(msg):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(BASE / "notifier.log", "a") as f:
        f.write(f"{stamp} {msg}\n")


def fetch(path):
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "swejobs-notifier"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def parse_rows(markdown):
    """Return {apply_url: {company, position, location, salary}}."""
    rows = {}
    for line in markdown.splitlines():
        line = line.strip()
        if not ROW_RE.match(line):
            continue
        cols = [c.strip() for c in line.split("|")][1:-1]
        if len(cols) < 5:
            continue
        company_m = STRONG_RE.search(cols[0])
        # Some tables omit the Salary column, so find the apply-button
        # column by its image rather than assuming a fixed position.
        apply_idx = next(
            (i for i, c in enumerate(cols[1:], 1) if 'alt="Apply"' in c), None
        )
        if not company_m or apply_idx is None:
            continue  # header/divider rows
        apply_m = HREF_RE.search(cols[apply_idx])
        if not apply_m:
            continue
        salary = cols[3] if apply_idx == 4 else ""
        rows[apply_m.group(1)] = {
            "company": html.unescape(company_m.group(1)),
            "position": html.unescape(re.sub(r"<[^>]+>", "", cols[1])).strip(),
            "location": html.unescape(re.sub(r"<[^>]+>", "", cols[2])).strip(),
            "salary": html.unescape(re.sub(r"<[^>]+>", "", salary)).strip(),
        }
    return rows


def send(text, formatted):
    body = json.dumps({
        "msgtype": "m.text",
        "body": text,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted,
    }).encode()
    txn = f"swejobs{int(time.time() * 1000)}"
    req = urllib.request.Request(
        f"{HS}/_matrix/client/v3/rooms/{ROOM}/send/m.room.message/{txn}",
        data=body, method="PUT",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        json.load(resp)


def fmt_item(info):
    parts = [f"<b>{html.escape(info['company'])}</b>"]
    parts.append(html.escape(info["position"]))
    if info["location"]:
        parts.append(html.escape(info["location"]))
    if info["salary"]:
        parts.append(html.escape(info["salary"]))
    return " — ".join(parts)


def main():
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    changed = False

    for path, label in FILES:
        try:
            markdown = fetch(path)
        except Exception as e:
            log(f"fetch failed for {path}: {e}")
            continue
        rows = parse_rows(markdown)
        if not rows:
            log(f"parsed 0 rows from {path}; skipping (format change?)")
            continue

        if path not in state:
            state[path] = rows
            changed = True
            log(f"seeded {path} with {len(rows)} listings")
            send(
                f"Now tracking {label}: {len(rows)} current listings. "
                "You'll be notified of new ones from here on.",
                f"Now tracking <b>{html.escape(label)}</b>: {len(rows)} current "
                "listings. You'll be notified of new ones from here on.",
            )
            continue

        old = state[path]
        added = {u: r for u, r in rows.items() if u not in old}
        removed = {u: r for u, r in old.items() if u not in rows}
        if not added and not removed:
            continue

        state[path] = rows
        changed = True
        log(f"{path}: +{len(added)} -{len(removed)}")

        text_lines, html_lines = [], []
        if added:
            text_lines.append(f"{len(added)} new posting(s) — {label}:")
            html_lines.append(f"<b>{len(added)} new posting(s)</b> — {html.escape(label)}<ul>")
            for url, info in added.items():
                text_lines.append(
                    f"• {info['company']} — {info['position']} — "
                    f"{info['location']} — {info['salary']} — {url}"
                )
                html_lines.append(
                    f'<li>{fmt_item(info)} — <a href="{html.escape(url)}">apply</a></li>'
                )
            html_lines.append("</ul>")
        if removed:
            names = ", ".join(
                f"{r['company']} ({r['position']})" for r in list(removed.values())[:15]
            )
            extra = f" and {len(removed) - 15} more" if len(removed) > 15 else ""
            text_lines.append(f"{len(removed)} listing(s) removed: {names}{extra}")
            html_lines.append(
                f"<i>{len(removed)} listing(s) removed: {html.escape(names)}{extra}</i>"
            )
        send("\n".join(text_lines), "".join(html_lines))

    if changed:
        STATE_PATH.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
