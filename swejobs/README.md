# swejobs

Notifies a Matrix room about new SWE/quant internship postings and mirrors
everything to a Google Sheet. Runs hourly via cron (12:00–20:00 ET, see
`crontab`), deduped across all sources, grouped into "Shopify level & above"
vs "Below" (see `tiers.py`).

## Sources

| name | what |
|---|---|
| `speedyapply-usa` / `-intl` / `-newgrad` | speedyapply/2027-SWE-College-Jobs tables |
| `simplify` | SimplifyJobs/Summer2027-Internships `listings.json` (active, 2027 terms) |
| `vanshb03` | vanshb03/Summer2027-Internships README table |
| `nuft-quant` | northwesternfintech/2027QuantInternships README (covers Citadel, Two Sigma etc.) |
| `ats:<slug>` | direct polls of ~70 company boards in `companies.json` (Greenhouse/Lever/Ashby), intern/new-grad titles only |

Add a company: append `{"name", "ats", "slug"}` to `companies.json` (find the
slug in the company's careers-page URL or its Simplify listing URL). Its
current postings seed silently on the next run. Remove a company only after
its jobs are gone, or they'll linger as "active" (removal requires every
source that listed a job to report back).

## How it dedupes

Postings are keyed by ATS job id extracted from the URL (`gh_jid`, Greenhouse
path id, Lever/Ashby UUID, Workday req) or the URL minus tracking params;
fallback match on normalized company+title. A job seen by 4 sources = 1 row,
1 notification.

## Files on the box (`~/swejobs/`, not in git)

`bot_token`, `room_id` — Matrix creds. `state.json` — v2 state: all jobs ever
seen, per-source key cache, ETags, sheet queue (auto-migrates v1).
`sheet_webhook` — optional: Apps Script URL (see `sheets.gs` header for the
3-minute setup). Until it exists, sheet rows queue up in state and flush on
the first run after setup. `notifier.log` — one line per eventful run.

## Costs / performance

Everything polled is a free public endpoint; no API keys. Bandwidth is kept
small via gzip + ETag conditional requests (Simplify's 11 MB file is ~1 MB
gzipped and usually a 304). A run makes ~75 requests with a 0.2 s stagger,
~60–90 s wall clock, well inside the hourly `flock`.

## Testing

`SWEJOBS_HOME=/tmp/testhome python3 notifier.py --dry-run` — fetches and
diffs for real, prints what it would send, writes no state.
