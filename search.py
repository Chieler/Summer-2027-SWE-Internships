#!/usr/bin/env python3
"""
Internship tracker: searches reliable sources for 2027 SWE / software-adjacent
internships and maintains ONE consolidated, de-duplicated list.

Aggregate model:
  Rather than rebuilding the list every run, results accumulate in a persistent
  store (aggregate.json). Each run pulls all sources, dedupes, and MERGES the
  fresh pull into the store (new roles appended, known roles refreshed). The
  README is rendered as a single consolidated table from the store, with a
  Location column (and a Most Influential highlight view on top).

Reliability note:
  LinkedIn blocks datacenter IPs (GitHub Actions runners), so scraping it
  directly is unreliable. Instead we pull from sources that are stable and
  scraper-friendly:
    1. GitHub's own "Summer 2027 Internships" community list (Markdown table)
    2. Greenhouse / Lever public job board APIs (clean JSON, no blocking)
    3. A LinkedIn *search URL* is included as a clickable link (not scraped),
       so you still get a one-tap way to view live LinkedIn results.
"""

import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

YEAR = "2027"
KEYWORDS = ("software", "swe", "engineer", "developer", "data", "ml", "infra", "backend", "frontend", "full stack", "full-stack")

# Hide postings older than this many days (auto-filter for likely-filled roles).
# Set to 0 to disable the age cutoff entirely.
MAX_AGE_DAYS = 45

# PRIORITY.md highlights explicit Summer-2027 roles posted within this window.
PRIORITY_DAYS = 14

UA = "Mozilla/5.0 (compatible; internship-tracker/1.0)"

# ---------------------------------------------------------------------------
# "Most Influential Tech Companies" highlight (see TOP_COMPANIES.md).
# A curated list combining TIME's 100 Most Influential Companies (2025, tech
# subset) with the largest technology companies by market cap, plus the most
# prominent AI labs, fintech/quant firms, dev-infra companies, and space/
# defense tech. Any pulled role at one of these companies is surfaced in a
# dedicated section at the very top of the README.
#
# Tokens are matched as whole words/phrases against the normalized company
# name, so "amazon" matches "Amazon", "Amazon Web Services" and "Amazon.com"
# but not unrelated substrings.
INFLUENTIAL_COMPANIES = {
    # Mega-cap / Big Tech
    "apple", "microsoft", "google", "alphabet", "deepmind", "amazon", "aws",
    "meta", "facebook", "instagram", "nvidia", "tesla", "broadcom", "oracle",
    "salesforce", "adobe", "netflix", "amd", "advanced micro devices", "intel",
    "ibm", "qualcomm", "cisco", "sap", "servicenow", "dell", "hp", "hewlett",
    # Semiconductors / hardware
    "tsmc", "taiwan semiconductor", "asml", "micron", "texas instruments",
    "applied materials", "analog devices", "arm", "samsung", "sony",
    # AI labs
    "openai", "anthropic", "scale ai", "databricks", "hugging face", "cohere",
    "mistral", "perplexity", "xai", "deepseek",
    # Fintech / payments
    "stripe", "paypal", "block", "square", "coinbase", "robinhood", "plaid",
    "ramp", "brex", "chime", "shopify",
    # Quant / trading
    "citadel", "jane street", "two sigma", "jump trading", "hudson river",
    "de shaw", "d e shaw", "optiver", "imc", "imc trading", "drw",
    "susquehanna", "sig", "point72", "tower research", "akuna",
    # Developer infra / SaaS
    "snowflake", "palantir", "crowdstrike", "datadog", "mongodb", "workday",
    "atlassian", "zoom", "cloudflare", "hashicorp", "gitlab", "github",
    "confluent", "elastic", "twilio", "okta", "rippling", "notion", "figma",
    "canva",
    # Consumer internet
    "uber", "lyft", "airbnb", "booking", "doordash", "instacart", "spotify",
    "discord", "reddit", "pinterest", "snap", "linkedin", "tiktok", "bytedance",
    "roblox", "unity", "electronic arts", "activision", "roku", "alibaba",
    "tencent", "baidu", "huawei", "nintendo", "coursera", "substack", "deepl",
    # Space / defense / autonomy
    "spacex", "anduril", "rivian", "waymo", "cruise", "zoox", "boston dynamics",
    "northrop grumman", "lockheed", "raytheon",
}


def is_influential(company):
    """True if a company name matches the curated influential-companies list.
    Matching is whole-word/phrase on the normalized name to avoid spurious
    substring hits (e.g. 'arm' must not match 'pharma')."""
    c = re.sub(r'[^a-z0-9 ]', ' ', (company or "").lower())
    c = " " + re.sub(r'\s+', ' ', c).strip() + " "
    return any(f" {tok} " in c for tok in INFLUENTIAL_COMPANIES)


def fetch(url, is_json=False, timeout=30):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json" if is_json else "*/*"})
    try:
        with urlopen(req, timeout=timeout) as r:
            data = r.read().decode("utf-8", errors="replace")
        return json.loads(data) if is_json else data
    except (URLError, HTTPError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  ! fetch failed for {url}: {e}", file=sys.stderr)
        return None


# "intern" as a whole word only — so full-time roles like "...Internal
# Applications" or "Software Engineer, International" don't slip through.
_INTERN_RE = re.compile(r"\bintern(ship)?s?\b")


def matches(title):
    t = title.lower()
    has_kw = any(k in t for k in KEYWORDS)
    is_intern = bool(_INTERN_RE.search(t))
    return has_kw and is_intern


def load_blacklist(path="blacklist.txt"):
    """
    Read URLs (or any URL fragment) you've manually marked as filled/dead.
    One entry per line. Blank lines and lines starting with # are ignored.
    Matching is substring-based, so you can paste a full URL or just a unique
    chunk of it (e.g. a job ID).
    """
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append(line.lower())
    except FileNotFoundError:
        pass  # no blacklist yet — that's fine
    return entries


def is_blacklisted(url, blacklist):
    u = (url or "").lower()
    return any(entry in u for entry in blacklist)


def load_applied(path="applied.txt"):
    """
    URLs (or unique fragments) of jobs you've already applied to. Same format
    as blacklist.txt. These aren't hidden — they're marked 'Applied' in the
    README so you can tell them apart at a glance. Default for everything not
    listed here is 'unapplied'.
    """
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append(line.lower())
    except FileNotFoundError:
        pass
    return entries


def is_applied(url, applied):
    u = (url or "").lower()
    return any(entry in u for entry in applied)


def too_old(posted, max_age_days=MAX_AGE_DAYS):
    """True if a YYYY-MM-DD posted date is older than the cutoff. Undated rows
    are never dropped (we can't prove they're old)."""
    if max_age_days <= 0 or not posted:
        return False
    try:
        d = datetime.strptime(posted, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - d) > timedelta(days=max_age_days)


def fmt_date(ts):
    """Turn a Unix timestamp (int or numeric str) into YYYY-MM-DD. '' if missing."""
    if not ts:
        return ""
    try:
        ts = float(ts)
        # some feeds give milliseconds; normalize anything implausibly large
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def _loc(val):
    """Coerce a location value from any source into a display string. Handles
    plain strings, dicts ({name} or {city,region/state,country}), and lists."""
    if not val:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        if val.get("name"):
            return str(val["name"]).strip()
        parts = [val.get("city"), val.get("region") or val.get("state"), val.get("country")]
        return ", ".join(str(p).strip() for p in parts if p)
    if isinstance(val, (list, tuple)):
        return ", ".join(filter(None, (_loc(v) for v in val)))
    return str(val).strip()


# ---------- Source 1: community GitHub lists (Simplify-schema JSON) ----------
# Several popular internship repos share the same listings.json schema
# (company_name / title / url / date_posted / active / is_visible). We pull
# each one with the same parser. Order matters only for the "source" label;
# dedupe later collapses the same role appearing in multiple repos.
SIMPLIFY_SCHEMA_SOURCES = [
    # (label, [url candidates tried in order until one returns data])
    ("vanshb03 2027", [
        "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
        "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/main/.github/scripts/listings.json",
    ]),
    ("Simplify/pittcsc", [
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    ]),
]


def detect_season(season_field, terms_field, title):
    """Return a normalized season label like 'Summer 2027' when we can tell,
    else ''. These repos are all 2027-cycle, so a bare 'Summer' means
    Summer 2027. We check the structured season field first, then terms,
    then the title text as a fallback."""
    blob = " ".join(filter(None, [
        str(season_field or ""),
        " ".join(terms_field) if isinstance(terms_field, list) else str(terms_field or ""),
        title or "",
    ])).lower()
    for season in ("summer", "fall", "winter", "spring"):
        if season in blob:
            # capture an explicit year if present, else assume 2027 cycle
            m = re.search(r'20(2[6-9]|3\d)', blob)
            year = m.group(0) if m else "2027"
            return f"{season.capitalize()} {year}"
    return ""


def parse_simplify_schema(data):
    """Extract matching rows from a Simplify-schema listings.json blob."""
    rows = []
    for job in data:
        if not job.get("active", True) or not job.get("is_visible", True):
            continue
        title = job.get("title", "")
        company = job.get("company_name", "")
        if not matches(title):
            continue
        url = job.get("url", "")
        if url:
            posted = fmt_date(job.get("date_posted"))
            season = detect_season(job.get("season"), job.get("terms"), title)
            location = _loc(job.get("locations")) or _loc(job.get("location"))
            rows.append((company, title, url, posted, season, location))
    return rows


def source_simplify_schema(label, candidates):
    """Fetch the first working URL for a Simplify-schema repo and parse it."""
    for url in candidates:
        data = fetch(url, is_json=True)
        if data:
            rows = parse_simplify_schema(data)
            print(f"  [{label}] {len(rows)} matches from {url.split('/')[4]}")
            return rows
    print(f"  [{label}] no data (all URLs failed)")
    return []


# ---------- Source: markdown-table repos (e.g. sndsh404) ----------
# Some lists are hand-maintained markdown tables, not JSON. Columns:
#   | Company | Role | Location | Apply |
# where Apply holds a [apply](url) link. No posted dates available.
MARKDOWN_TABLE_SOURCES = [
    ("sndsh404 2027", "https://raw.githubusercontent.com/sndsh404/summer-2027-internships/main/README.md"),
]

_MD_LINK = re.compile(r'\[[^\]]*\]\((https?://[^)\s]+)\)')


def source_markdown_table(label, url):
    raw = fetch(url)
    if not raw:
        print(f"  [{label}] no data")
        return []
    rows = []
    in_list = False
    for line in raw.splitlines():
        # Only parse the main "the list" table: rows with a Company|Role|Loc|Apply
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 4:
            continue
        company, role = cells[0], cells[1]
        low = (company + role).lower()
        if "company" in company.lower() and "role" in role.lower():
            in_list = True   # header row of the target table
            continue
        if set(company) <= {"-", ":", " "}:  # separator row |---|---|
            continue
        if not in_list:
            continue
        # stop if we've wandered into a different table (e.g. "org"/"program")
        if not matches(role) and not matches(company + " " + role):
            continue
        m = _MD_LINK.search(cells[3])
        if not m:
            continue
        link = m.group(1)
        # strip emoji/flags and markdown from the visible fields
        role = re.sub(r'[🔒🛂🇺🇸]', '', role).strip()
        company = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', company).strip()
        season = detect_season("", "", role)  # season is embedded in role text
        # 3rd column is Location in this table shape
        location = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', cells[2]).strip()
        rows.append((company, role, link, "", season, location))  # no posted date
    print(f"  [{label}] {len(rows)} matches")
    return rows


# ---------- Source: jobs-object repos (e.g. zshah101 automated list) ----------
# Some trackers publish a clean JSON API shaped as {"jobs": [ {...}, ... ]}
# where each job has company / title / url / posted_at (ISO 8601) / season /
# category. We consume that feed directly.
JOBS_OBJECT_SOURCES = [
    ("zshah101 (2027 + Fall 2026)", [
        "https://raw.githubusercontent.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/main/docs/api/jobs.json",
        "https://raw.githubusercontent.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/main/data/jobs.json",
    ]),
]


def _iso_to_date(s):
    """'2026-07-16T00:00:00Z' -> '2026-07-16'. '' if missing/unparseable."""
    s = s or ""
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else ""


def source_jobs_object(label, candidates):
    """Fetch the first working URL for a {'jobs': [...]} feed and parse it."""
    for url in candidates:
        data = fetch(url, is_json=True)
        if not data:
            continue
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        rows = []
        for job in jobs:
            title = job.get("title", "")
            if not matches(title):
                continue
            u = job.get("url", "")
            if not u:
                continue
            posted = _iso_to_date(job.get("posted_at") or job.get("date_posted"))
            season = job.get("season") or detect_season("", "", title)
            location = _loc(job.get("location"))
            rows.append((job.get("company", ""), title, u, posted, season, location))
        print(f"  [{label}] {len(rows)} matches")
        return rows
    print(f"  [{label}] no data (all URLs failed)")
    return []


# ---------- Source: Ashby public job boards ----------
# Ashby exposes a clean public posting API per org:
#   https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=false
# -> {"jobs": [{"title","location","team","employmentType","jobUrl","applyUrl",
#               "publishedDate"?, ...}]}
# Popular with AI labs and modern startups. Unknown/private orgs just 404 and
# are skipped. Keys are org slugs; values are display names.
ASHBY_BOARDS = {
    "openai": "OpenAI", "ramp": "Ramp", "notion": "Notion", "linear": "Linear",
    "cursor": "Cursor", "anysphere": "Cursor", "perplexity-ai": "Perplexity",
    "vercel": "Vercel", "clay": "Clay", "sierra": "Sierra", "mercor": "Mercor",
    "runwayml": "Runway", "cohere": "Cohere", "huggingface": "Hugging Face",
    "scaleai": "Scale AI", "deel": "Deel", "gong": "Gong", "ashby": "Ashby",
    "watershed": "Watershed", "modal": "Modal", "baseten": "Baseten",
    "together-ai": "Together AI", "harvey": "Harvey", "glean": "Glean",
    "rox": "Rox", "decagon": "Decagon", "hebbia": "Hebbia", "sardine": "Sardine",
    "openstore": "OpenStore", "eightsleep": "Eight Sleep", "ironclad": "Ironclad",
    "applied-intuition": "Applied Intuition", "physical-intelligence": "Physical Intelligence",
    "skild-ai": "Skild AI", "figure": "Figure", "suno": "Suno", "elevenlabs": "ElevenLabs",
}


def source_ashby():
    results = []
    for org, display in ASHBY_BOARDS.items():
        data = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=false", is_json=True)
        if not data or "jobs" not in data:
            continue
        for job in data["jobs"]:
            title = job.get("title", "")
            if not matches(title):
                continue
            url = job.get("jobUrl") or job.get("applyUrl") or ""
            if not url:
                continue
            posted = _iso_to_date(job.get("publishedDate") or job.get("publishedAt") or "")
            company = job.get("organizationName") or display
            location = _loc(job.get("location")) or _loc(job.get("locationName")) or _loc(job.get("address"))
            results.append((company, title, url, posted, detect_season("", "", title), location))
        time.sleep(0.3)
    return results


# ---------- Source: SmartRecruiters public postings ----------
# https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100
# -> {"content": [{"id","name","releasedDate","company":{"name"},
#                  "location":{...}}]}
SMARTRECRUITERS_BOARDS = ["Verkada", "Square", "Blizzard", "Ubisoft", "Zalando", "Nianticinc"]


def source_smartrecruiters():
    results = []
    for company in SMARTRECRUITERS_BOARDS:
        data = fetch(f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100", is_json=True)
        if not data or "content" not in data:
            continue
        for job in data["content"]:
            title = job.get("name", "")
            if not matches(title):
                continue
            jid = job.get("id", "")
            if not jid:
                continue
            url = f"https://jobs.smartrecruiters.com/{company}/{jid}"
            posted = _iso_to_date(job.get("releasedDate") or "")
            display = (job.get("company") or {}).get("name") or company
            location = _loc(job.get("location"))
            results.append((display, title, url, posted, detect_season("", "", title), location))
        time.sleep(0.3)
    return results


# ---------- Source: Workable public widget API ----------
# https://apply.workable.com/api/v1/widget/accounts/{account}?details=true
# -> {"name":..., "jobs":[{"title","shortcode","url","application_url",
#                          "created_at","country","city"}]}
WORKABLE_BOARDS = ["mistral", "helsing"]


def source_workable():
    results = []
    for account in WORKABLE_BOARDS:
        data = fetch(f"https://apply.workable.com/api/v1/widget/accounts/{account}?details=true", is_json=True)
        if not data or "jobs" not in data:
            continue
        name = data.get("name") or account.capitalize()
        for job in data["jobs"]:
            title = job.get("title", "")
            if not matches(title):
                continue
            url = job.get("url") or job.get("application_url") or ""
            if not url:
                continue
            posted = _iso_to_date(job.get("created_at") or "")
            location = _loc(job.get("location")) or _loc({"city": job.get("city"), "country": job.get("country")})
            results.append((name, title, url, posted, detect_season("", "", title), location))
        time.sleep(0.3)
    return results


# NOTE: Workday and Oracle Cloud recruiting have no universal public job API —
# each employer is a separate tenant with its own host and site path, so a
# generic connector isn't possible. Those roles still reach us via the
# community JSON lists (which resolve the per-tenant URLs upstream).


# ---------- Source 2: Greenhouse public boards ----------
GREENHOUSE_BOARDS = [
    "stripe", "databricks", "robinhood", "coinbase", "airbnb", "doordash",
    "plaid", "rippling", "cloudflare", "discord", "reddit", "instacart",
    "gitlab", "samsara", "flexport", "benchling", "affirm", "airtable",
    "twitch", "gusto", "sofi", "brex",
]

def source_greenhouse():
    results = []
    for board in GREENHOUSE_BOARDS:
        data = fetch(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", is_json=True)
        if not data or "jobs" not in data:
            continue
        for job in data["jobs"]:
            title = job.get("title", "")
            if matches(title):
                # Greenhouse gives an ISO date string in updated_at / first_published
                posted = ""
                raw = job.get("first_published") or job.get("updated_at") or ""
                if raw:
                    posted = raw[:10]  # 'YYYY-MM-DD...' -> 'YYYY-MM-DD'
                location = _loc(job.get("location"))
                results.append((board.capitalize(), title, job.get("absolute_url", ""), posted, detect_season("", "", title), location))
        time.sleep(0.3)
    return results


# ---------- Source 3: Lever public boards ----------
LEVER_BOARDS = ["ramp", "anduril", "scale", "figma", "palantir", "attentive", "kraken"]

def source_lever():
    results = []
    for board in LEVER_BOARDS:
        data = fetch(f"https://api.lever.co/v0/postings/{board}?mode=json", is_json=True)
        if not data or not isinstance(data, list):
            continue
        for job in data:
            title = job.get("text", "")
            if matches(title):
                # Lever gives createdAt as Unix milliseconds
                posted = fmt_date(job.get("createdAt"))
                location = _loc((job.get("categories") or {}).get("location"))
                results.append((board.capitalize(), title, job.get("hostedUrl", ""), posted, detect_season("", "", title), location))
        time.sleep(0.3)
    return results


# ---------- LinkedIn: link only, not scraped ----------
def linkedin_search_link():
    q = urllib.parse.quote(f"software engineer intern {YEAR}")
    return f"https://www.linkedin.com/jobs/search/?keywords={q}&f_E=1&f_JT=I"


def _norm(s):
    """Normalize a string for fuzzy matching: lowercase, strip punctuation/extra
    whitespace, drop common noise words."""
    s = (s or "").lower()
    s = re.sub(r'\(.*?\)', '', s)            # drop parenthetical notes
    s = re.sub(r'[^a-z0-9 ]', ' ', s)         # punctuation -> space
    s = re.sub(r'\b(20\d\d|summer|fall|winter|spring|intern|internship|the)\b', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def dedupe(rows):
    """Collapse duplicate listings across sources.

    Two rows are duplicates if they share the same apply URL, OR the same
    normalized (company, role) pair. When duplicates collide we keep the most
    informative version (prefers one that has a posted date and/or a detected
    season). Rows are (company, role, url, posted, season) tuples.
    """
    def score(r):
        # higher = more informative
        return (1 if r[3] else 0) + (1 if len(r) > 4 and r[4] else 0)

    seen_url = {}
    seen_pair = {}
    out = []
    for row in rows:
        company, role, url, posted = row[0], row[1], row[2], row[3]
        url_key = (url or "").strip().lower().rstrip("/")
        pair_key = (_norm(company), _norm(role))

        prior_idx = None
        if url_key and url_key in seen_url:
            prior_idx = seen_url[url_key]
        elif pair_key in seen_pair:
            prior_idx = seen_pair[pair_key]

        if prior_idx is None:
            idx = len(out)
            out.append(row)
            if url_key:
                seen_url[url_key] = idx
            seen_pair[pair_key] = idx
        else:
            if score(row) > score(out[prior_idx]):
                out[prior_idx] = row
                if url_key:
                    seen_url[url_key] = prior_idx
                seen_pair[pair_key] = prior_idx
    return out


def _neg_date_key(posted):
    """Sort key so newer dates come first in an ascending sort, and undated
    rows land at the bottom. Returns a tuple: (has_no_date, negated_ordinal)."""
    if not posted:
        return (1, 0)  # undated -> after all dated rows
    try:
        d = datetime.strptime(posted, "%Y-%m-%d")
        return (0, -d.toordinal())  # negate so most recent sorts first
    except ValueError:
        return (1, 0)


# ---------------------------------------------------------------------------
# Persistent aggregate store. Instead of rebuilding the list from scratch each
# run, we keep one de-duplicated JSON file that ACCUMULATES every role we've
# ever seen. Each run merges the fresh pull into it (new roles appended, known
# roles get their last_seen bumped and any missing fields filled). The README
# is rendered as a single consolidated table from this store.
AGGREGATE_PATH = "aggregate.json"


def load_aggregate(path=AGGREGATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_aggregate(records, path=AGGREGATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)
        f.write("\n")


def merge_into_aggregate(aggregate, rows, today):
    """Add fresh rows into `aggregate` in place. A row matches an existing
    record by apply URL or by normalized (company, role) pair — same identity
    rule as dedupe(). New roles are appended with first_seen=today; matched
    roles get last_seen=today and any blank fields filled in. Returns the count
    of newly-added roles. Rows are (company, role, url, posted, season, location)."""
    by_url, by_pair = {}, {}
    for i, rec in enumerate(aggregate):
        uk = (rec.get("url") or "").strip().lower().rstrip("/")
        pk = (_norm(rec.get("company")), _norm(rec.get("role")))
        if uk:
            by_url[uk] = i
        by_pair.setdefault(pk, i)

    added = 0
    for row in rows:
        company, role, url, posted = row[0], row[1], row[2], row[3]
        season = row[4] if len(row) > 4 else ""
        location = row[5] if len(row) > 5 else ""
        uk = (url or "").strip().lower().rstrip("/")
        pk = (_norm(company), _norm(role))
        idx = by_url.get(uk) if uk else None
        if idx is None:
            idx = by_pair.get(pk)
        if idx is None:
            aggregate.append({
                "company": company, "role": role, "url": url,
                "posted": posted, "season": season, "location": location,
                "first_seen": today, "last_seen": today,
            })
            j = len(aggregate) - 1
            if uk:
                by_url[uk] = j
            by_pair.setdefault(pk, j)
            added += 1
        else:
            rec = aggregate[idx]
            rec["last_seen"] = today
            if not rec.get("posted") and posted:
                rec["posted"] = posted
            if not rec.get("location") and location:
                rec["location"] = location
            if not rec.get("season") and season:
                rec["season"] = season
    return added


def _render_agg_table(records, applied):
    """Render a consolidated job table (Company | Role | Location | Posted |
    Link) from aggregate records, newest-first, deduped by (company, role)."""
    lines = ["| Company | Role | Location | Posted | Link |", "|---|---|---|---|---|"]
    ordered = sorted(
        records,
        key=lambda r: (is_applied(r.get("url", ""), applied), _neg_date_key(r.get("posted", ""))),
    )
    seen = set()
    for rec in ordered:
        company = (rec.get("company") or "").replace("|", "\\|")
        role = (rec.get("role") or "").replace("|", "\\|")
        key = (company, role)
        if key in seen:
            continue
        seen.add(key)
        location = (rec.get("location") or "").replace("|", "\\|") or "—"
        posted = rec.get("posted") or "—"
        url = rec.get("url") or ""
        lines.append(f"| {company} | {role} | {location} | {posted} | [Apply]({url}) |")
    lines.append("")
    return lines


def build_aggregate_readme(aggregate, linkedin_url, applied, today):
    """Render the README as ONE consolidated, deduped list from the aggregate
    store, with a Most Influential highlight view on top. The Location column
    replaces the old Applied column."""
    applied = applied or []
    if MAX_AGE_DAYS > 0:
        visible = [r for r in aggregate
                   if is_applied(r.get("url", ""), applied) or not too_old(r.get("posted", ""))]
    else:
        visible = list(aggregate)
    influential = [r for r in visible if is_influential(r.get("company", ""))]
    n_comp = len({_norm(r.get("company")) for r in influential})

    lines = [f"# {YEAR} SWE / Software-Adjacent Internships — Aggregate List", ""]
    if MAX_AGE_DAYS > 0:
        lines.append(
            f"_**Updated:** {today} · **{len(aggregate)}** unique roles in the aggregate "
            f"(all-time) · **{len(visible)}** shown after the {MAX_AGE_DAYS}-day freshness filter._"
        )
    else:
        lines.append(f"_**Updated:** {today} · **{len(aggregate)}** unique roles in the aggregate._")
    lines.append("")
    lines.append(f"**[Open live LinkedIn search]({linkedin_url})** (LinkedIn can't be scraped reliably from CI, so this is a one-tap live link instead.)")
    lines.append("")
    lines.append(
        "_One consolidated, de-duplicated list built from every source (community "
        "lists + company boards). Each run **adds onto** this aggregate rather than "
        "rebuilding it — the persistent store is [`aggregate.json`](aggregate.json). "
        "The **Location** column replaces the old Applied column._"
    )
    lines.append("")

    # All Internships first (the full consolidated list), then the Most
    # Influential highlight second.
    lines.append(f"## 📋 All Internships ({len(visible)})")
    lines.append("")
    lines += _render_agg_table(visible, applied)

    lines.append(f"## 🏆 Most Influential Tech Companies ({len(influential)})")
    lines.append("")
    lines.append(
        f"_Roles at companies on the curated influential-companies list "
        f"({n_comp} companies represented; see [`TOP_COMPANIES.md`](TOP_COMPANIES.md)). "
        f"A highlight view of the list above._"
    )
    lines.append("")
    if influential:
        lines += _render_agg_table(influential, applied)
    else:
        lines += ["> None of the influential companies have a role in the current window.", ""]

    lines.append("---")
    cutoff_note = (
        f"Roles older than {MAX_AGE_DAYS} days are hidden from this view but kept in "
        f"`aggregate.json`. " if MAX_AGE_DAYS > 0 else ""
    )
    lines.append(
        f"_Single consolidated aggregate, deduplicated across all sources. {cutoff_note}"
        f"Add filled roles to `blacklist.txt` to purge them permanently. "
        f"Generated automatically by GitHub Actions._"
    )
    return "\n".join(lines)


def is_summer_2027(season, role):
    """True if this role is specifically a Summer 2027 posting."""
    text = f"{season or ''} {role or ''}".lower()
    if "summer" not in text:
        return False
    # exclude if some other year is explicitly named and it isn't 2027
    years = re.findall(r'20(2[0-9]|3\d)', text)
    if years and "27" not in years:
        return False
    return True


def recent_within(posted, days):
    if not posted:
        return False
    try:
        d = datetime.strptime(posted, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - d) <= timedelta(days=days)


def build_priority_agg(aggregate, applied, today):
    """Render PRIORITY.md from the aggregate store: explicit Summer 2027 roles
    posted within the last PRIORITY_DAYS, newest first."""
    applied = applied or []
    hits = [
        r for r in aggregate
        if is_summer_2027(r.get("season", ""), r.get("role", ""))
        and recent_within(r.get("posted", ""), PRIORITY_DAYS)
    ]
    hits.sort(key=lambda r: _neg_date_key(r.get("posted", "")))

    lines = [
        "# 🔥 Priority — Fresh Summer 2027 Roles",
        "",
        f"_**Updated:** {today}  —  {len(hits)} role(s) explicitly tagged Summer 2027 "
        f"and posted within the last {PRIORITY_DAYS} days._",
        "",
    ]
    if not hits:
        lines += [
            "> Nothing here yet. The Summer 2027 cycle mostly opens from ~August 2026, "
            "so explicit Summer-2027 postings are scarce until then. This page fills "
            "automatically as fresh roles land. See `README.md` for the full list "
            "(including off-season and pipeline roles open now).",
            "",
        ]
    else:
        lines += _render_agg_table(hits, applied)
    lines.append("---")
    lines.append(f"_Auto-generated from `aggregate.json`. Window: {PRIORITY_DAYS} days (edit `PRIORITY_DAYS` in `search.py`). See `README.md` for everything._")
    return "\n".join(lines)


def collect_fresh():
    """Pull every source and return a deduped list of fresh rows
    (company, role, url, posted, season, location)."""
    fresh = []

    print("Community lists (JSON):")
    for label, candidates in SIMPLIFY_SCHEMA_SOURCES:
        fresh += source_simplify_schema(label, candidates)

    print("Community lists (markdown):")
    for label, url in MARKDOWN_TABLE_SOURCES:
        fresh += source_markdown_table(label, url)

    print("Community lists (jobs-object JSON):")
    for label, candidates in JOBS_OBJECT_SOURCES:
        fresh += source_jobs_object(label, candidates)

    print("Company boards:")
    fresh += source_ashby()
    print("  [Ashby] done")
    fresh += source_greenhouse()
    print("  [Greenhouse] done")
    fresh += source_lever()
    print("  [Lever] done")
    fresh += source_smartrecruiters()
    print("  [SmartRecruiters] done")
    fresh += source_workable()
    print("  [Workable] done")

    raw_total = len(fresh)
    fresh = dedupe(fresh)
    print(f"Fresh pull: {raw_total} rows -> {len(fresh)} unique this run")
    return fresh


def main():
    blacklist = load_blacklist()
    applied = load_applied()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if blacklist:
        print(f"Loaded {len(blacklist)} blacklist entries")
    if applied:
        print(f"Loaded {len(applied)} applied entries")

    # 1. Pull fresh rows from every source, dropping blacklisted ones up front
    #    so they never enter the store (avoids add-then-purge churn each run).
    fresh = collect_fresh()
    if blacklist:
        before = len(fresh)
        fresh = [r for r in fresh if not is_blacklisted(r[2], blacklist)]
        if before != len(fresh):
            print(f"Blacklist skipped {before - len(fresh)} fresh row(s)")

    # 2. Merge into the persistent aggregate (accumulate, don't rebuild).
    aggregate = load_aggregate()
    print(f"Loaded aggregate: {len(aggregate)} existing roles")
    added = merge_into_aggregate(aggregate, fresh, today)
    print(f"Merged fresh pull: +{added} new roles (aggregate now {len(aggregate)})")

    # 3. Also purge any roles blacklisted AFTER they were added (retroactive).
    before = len(aggregate)
    aggregate = [r for r in aggregate if not is_blacklisted(r.get("url", ""), blacklist)]
    if before != len(aggregate):
        print(f"Blacklist purged {before - len(aggregate)} already-stored role(s)")

    # 4. Persist the aggregate.
    save_aggregate(aggregate)
    print(f"Wrote {AGGREGATE_PATH} ({len(aggregate)} roles)")

    # 5. Render the consolidated README + PRIORITY from the aggregate.
    readme = build_aggregate_readme(aggregate, linkedin_search_link(), applied, today)
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(readme + "\n")
    print("Wrote README.md")

    priority = build_priority_agg(aggregate, applied, today)
    with open("PRIORITY.md", "w", encoding="utf-8") as f:
        f.write(priority + "\n")
    print("Wrote PRIORITY.md")


if __name__ == "__main__":
    main()
