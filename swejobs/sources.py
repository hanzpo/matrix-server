"""Source adapters. Each fetch_* returns a list of normalized postings:

    {company, title, location, salary, url, source}

Adapters raise on fetch errors (caller isolates failures per source) and
return [] only when the fetched content genuinely holds no matching rows.
HTTP responses are cached by ETag: fetch() returns None body on 304 and the
caller reuses the previously parsed keys from state.
"""
import gzip
import html
import json
import re
import urllib.request

TIMEOUT = 25
UA = "swejobs-notifier (github.com/hanzpo)"

SPEEDYAPPLY_FILES = [
    ("README.md", "speedyapply-usa"),
    ("INTERN_INTL.md", "speedyapply-intl"),
    ("NEW_GRAD_USA.md", "speedyapply-newgrad"),
]
SIMPLIFY_URL = ("https://raw.githubusercontent.com/SimplifyJobs/"
                "Summer2027-Internships/dev/.github/scripts/listings.json")
VANSH_URL = ("https://raw.githubusercontent.com/vanshb03/"
             "Summer2027-Internships/main/README.md")
NUFT_URL = ("https://raw.githubusercontent.com/northwesternfintech/"
            "2027QuantInternships/main/README.md")

ROW_RE = re.compile(r"^\|.*\|$")
STRONG_RE = re.compile(r"<strong>(.*?)</strong>")
HREF_RE = re.compile(r'href="([^"]+)"')
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
TAG_RE = re.compile(r"<[^>]+>")
# Matches "Intern", "Internship", "Co-op" etc. as whole words ("internal"
# does not match), plus new-grad phrasings, for filtering ATS board titles.
INTERN_TITLE_RE = re.compile(
    r"\b(intern(ship)?s?|co-?op|new\s+grad(uate)?|university\s+grad|campus\s+hire)\b",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(20\d\d)\b")


def fetch(url, etag=None, timeout=TIMEOUT):
    """GET with gzip + ETag support. Returns (text_or_None, new_etag);
    text is None when the server answered 304 Not Modified."""
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body.decode("utf-8"), resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


def _strip_tags(s):
    return html.unescape(TAG_RE.sub("", s)).strip()


def _clean_title(s):
    # Drop decoration emojis the lists use (visa/citizenship/closed markers).
    return re.sub(r"[\U0001F100-\U0001FAFF☀-➿️]", "", s).strip()


def parse_speedyapply(markdown, source):
    rows = []
    for line in markdown.splitlines():
        line = line.strip()
        if not ROW_RE.match(line):
            continue
        cols = [c.strip() for c in line.split("|")][1:-1]
        if len(cols) < 5:
            continue
        company_m = STRONG_RE.search(cols[0])
        apply_idx = next(
            (i for i, c in enumerate(cols[1:], 1) if 'alt="Apply"' in c), None
        )
        if not company_m or apply_idx is None:
            continue
        apply_m = HREF_RE.search(cols[apply_idx])
        if not apply_m:
            continue
        salary = cols[3] if apply_idx == 4 else ""
        rows.append({
            "company": html.unescape(company_m.group(1)),
            "title": _clean_title(_strip_tags(cols[1])),
            "location": _strip_tags(cols[2]),
            "salary": _strip_tags(salary),
            "url": apply_m.group(1),
            "source": source,
        })
    return rows


def parse_simplify(text):
    rows = []
    for x in json.loads(text):
        if not (x.get("active") and x.get("is_visible")):
            continue
        if not any("2027" in t for t in x.get("terms", [])):
            continue
        rows.append({
            "company": x["company_name"].strip(),
            "title": _clean_title(x["title"]),
            "location": "; ".join(x.get("locations", []))[:120],
            "salary": "",
            "url": x["url"],
            "source": "simplify",
        })
    return rows


def parse_vansh(markdown):
    rows, last_company = [], None
    for line in markdown.splitlines():
        line = line.strip()
        if not ROW_RE.match(line):
            continue
        cols = [c.strip() for c in line.split("|")][1:-1]
        if len(cols) < 4 or "🔒" in line:
            continue
        company = _strip_tags(cols[0])
        if company in ("Company", "-------") or set(company) <= {"-", " "}:
            continue
        if company == "↳" and last_company:
            company = last_company
        last_company = company
        m = HREF_RE.search(cols[3])
        url = m.group(1) if m else None
        if not url:
            m = MD_LINK_RE.search(cols[3])
            url = m.group(2) if m else None
        if not url:
            continue
        rows.append({
            "company": company,
            "title": _clean_title(_strip_tags(cols[1])),
            "location": _strip_tags(cols[2]),
            "salary": "",
            "url": url,
            "source": "vanshb03",
        })
    return rows


NUFT_ROLE_NAMES = {
    "QR": "Quant Research Intern",
    "QD": "Quant Developer Intern",
    "QT": "Quant Trading Intern",
    "SWE": "Software Engineering Intern",
    "HW": "Hardware Engineering Intern",
}


def parse_nuft(markdown):
    """The NUFT README is generated from YAML: '## Firm' sections, each with
    a |Role|Links| table whose cells hold [✅ label](url) links (❌ = closed)."""
    rows = []
    company = ""
    for line in markdown.splitlines():
        line = line.strip()
        if line.startswith("## ") and line != "## Using This Repository":
            company = line[3:].strip()
            continue
        if company in ("", "Contributing") or not ROW_RE.match(line):
            continue
        cols = [c.strip() for c in line.split("|")][1:-1]
        if len(cols) != 2 or cols[0] in ("Role", "-------"):
            continue
        role = cols[0].strip()
        for label, url in MD_LINK_RE.findall(cols[1]):
            if "❌" in label:
                continue
            label = label.replace("✅", "").strip()
            title = NUFT_ROLE_NAMES.get(role, f"{role} Intern")
            if label:
                title = f"{title} ({label})"
            rows.append({
                "company": company,
                "title": title,
                "location": "",
                "salary": "",
                "url": url.strip(),
                "source": "nuft-quant",
            })
    return rows


def _title_wanted(title):
    if not INTERN_TITLE_RE.search(title):
        return False
    years = [int(y) for y in YEAR_RE.findall(title)]
    return not years or max(years) >= 2027


def fetch_ats_board(name, ats, slug, etag=None):
    """Poll one company's public job board. Returns (rows_or_None, etag);
    rows is None on 304 (unchanged)."""
    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    elif ats == "lever":
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    elif ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    else:
        raise ValueError(f"unknown ats {ats}")
    text, new_etag = fetch(url, etag, timeout=15)
    if text is None:
        return None, new_etag
    data = json.loads(text)
    rows = []
    if ats == "greenhouse":
        for j in data.get("jobs", []):
            if _title_wanted(j.get("title", "")):
                rows.append({
                    "company": name, "title": _clean_title(j["title"]),
                    "location": (j.get("location") or {}).get("name", ""),
                    "salary": "", "url": j["absolute_url"],
                    "source": f"ats:{slug}",
                })
    elif ats == "lever":
        for j in data:
            if _title_wanted(j.get("text", "")):
                rows.append({
                    "company": name, "title": _clean_title(j["text"]),
                    "location": (j.get("categories") or {}).get("location", ""),
                    "salary": "", "url": j["hostedUrl"],
                    "source": f"ats:{slug}",
                })
    elif ats == "ashby":
        for j in data.get("jobs", []):
            if _title_wanted(j.get("title", "")):
                rows.append({
                    "company": name, "title": _clean_title(j["title"]),
                    "location": j.get("location", ""),
                    "salary": "", "url": j.get("jobUrl") or j.get("applyUrl", ""),
                    "source": f"ats:{slug}",
                })
    return rows, new_etag
