#!/usr/bin/env python3
"""
Internship tracker: searches reliable sources for 2027 SWE / software-adjacent
internships and writes the results into README.md.

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
MAX_AGE_DAYS = 90

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


def matches(title):
    t = title.lower()
    has_kw = any(k in t for k in KEYWORDS)
    is_intern = "intern" in t
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
            rows.append((company, title, url, posted, season))
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
        rows.append((company, role, link, "", season))  # no posted date
    print(f"  [{label}] {len(rows)} matches")
    return rows


# ---------- Source 2: Greenhouse public boards ----------
GREENHOUSE_BOARDS = ["stripe", "databricks", "robinhood", "coinbase", "airbnb", "doordash", "plaid", "rippling"]

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
                results.append((board.capitalize(), title, job.get("absolute_url", ""), posted, detect_season("", "", title)))
        time.sleep(0.3)
    return results


# ---------- Source 3: Lever public boards ----------
LEVER_BOARDS = ["ramp", "anduril", "scale", "figma"]

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
                results.append((board.capitalize(), title, job.get("hostedUrl", ""), posted, detect_season("", "", title)))
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


def build_influential_section(rows, applied=None):
    """Render the 'Most Influential Tech Companies' highlight table: every
    pulled role at a company on INFLUENTIAL_COMPANIES, unapplied-first then
    newest-first. `rows` is the deduped, already-filtered pool. Also appears
    in the per-source lists further down the README."""
    applied = applied or []
    hits = [r for r in rows if is_influential(r[0])]
    hits.sort(key=lambda r: (is_applied(r[2], applied), _neg_date_key(r[3])))

    n_companies = len({_norm(r[0]) for r in hits})
    lines = [f"## 🏆 Most Influential Tech Companies — {YEAR} Internships ({len(hits)})", ""]
    lines.append(
        "_Open roles at companies on our curated **Most Influential Tech "
        "Companies** list (TIME100 Most Influential Companies 2025 — tech "
        "subset — plus the largest tech companies by market cap; see "
        "[`TOP_COMPANIES.md`](TOP_COMPANIES.md)). These roles also appear in "
        "the per-source lists below._"
    )
    lines.append("")
    if not hits:
        lines.append(
            "> No influential-company roles matched in this run. This section "
            "fills in automatically as fresh roles are pulled."
        )
        lines.append("")
        return "\n".join(lines)
    lines.append(f"_{len(hits)} role(s) across {n_companies} influential companies._")
    lines.append("")
    lines.append("| Company | Role | Posted | Applied | Link |")
    lines.append("|---|---|---|---|---|")
    seen = set()
    for row in hits:
        company, role, link, posted = row[0], row[1], row[2], row[3]
        key = (company, role)
        if key in seen:
            continue
        seen.add(key)
        role = role.replace("|", "\\|")
        company = company.replace("|", "\\|")
        mark = "✅" if is_applied(link, applied) else "—"
        posted = posted or "—"
        lines.append(f"| {company} | {role} | {posted} | {mark} | [Apply]({link}) |")
    lines.append("")
    return "\n".join(lines)


def build_readme(buckets, linkedin_url, applied=None, influential_rows=None):
    applied = applied or []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = sum(len(v) for v in buckets.values())
    lines = []
    lines.append(f"# {YEAR} SWE / Software-Adjacent Internships")
    lines.append("")
    lines.append(f"_**Pulled:** {now}  —  {total} matching roles found this run._")
    lines.append("")
    lines.append(f"**[Open live LinkedIn search]({linkedin_url})** (LinkedIn can't be scraped reliably from CI, so this is a one-tap live link instead.)")
    lines.append("")
    if total == 0:
        lines.append(
            f"> No matches this run. Likely because the 2027 list isn't live yet "
            f"(the script auto-falls back to the current cycle's list) **and** the "
            f"{MAX_AGE_DAYS}-day age cutoff hides older off-season postings. This "
            f"resolves on its own once fresh 2027 roles appear. To see older roles "
            f"meanwhile, lower `MAX_AGE_DAYS` in `search.py` (set 0 to disable)."
            if MAX_AGE_DAYS > 0 else
            "> No matches this run. Sources may be rate-limited or the 2027 list "
            "may not be populated yet — it will retry on the next scheduled run."
        )
        lines.append("")
    if influential_rows is not None:
        lines.append(build_influential_section(influential_rows, applied=applied))
    for source_name, rows in buckets.items():
        if not rows:
            continue
        lines.append(f"## {source_name} ({len(rows)})")
        lines.append("")
        lines.append("| Company | Role | Posted | Applied | Link |")
        lines.append("|---|---|---|---|---|")
        # sort: unapplied first, then newest-first within each group
        # (undated rows sink to the bottom of their group)
        rows_sorted = sorted(
            rows,
            key=lambda r: (is_applied(r[2], applied), _neg_date_key(r[3])),
        )
        seen = set()
        for row in rows_sorted:
            company, role, link, posted = row[0], row[1], row[2], row[3]
            key = (company, role)
            if key in seen:
                continue
            seen.add(key)
            role = role.replace("|", "\\|")
            company = company.replace("|", "\\|")
            applied_mark = "✅" if is_applied(link, applied) else "—"
            posted = posted or "—"
            lines.append(f"| {company} | {role} | {posted} | {applied_mark} | [Apply]({link}) |")
        lines.append("")
    lines.append("---")
    cutoff_note = (
        f"Postings older than {MAX_AGE_DAYS} days are auto-hidden as likely-filled. "
        if MAX_AGE_DAYS > 0 else ""
    )
    lines.append(
        f"_✅ = applied (tracked in `applied.txt`). {cutoff_note}"
        f"Pulled from multiple community lists and company boards, deduplicated. "
        f"Add filled roles to `blacklist.txt` to hide them. "
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


def build_priority(rows, applied=None):
    """Render PRIORITY.md: explicit Summer 2027 roles posted very recently,
    newest first. `rows` is the deduped, already-filtered pool."""
    applied = applied or []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hits = [
        r for r in rows
        if is_summer_2027(r[4] if len(r) > 4 else "", r[1])
        and recent_within(r[3], PRIORITY_DAYS)
    ]
    hits.sort(key=lambda r: _neg_date_key(r[3]))

    lines = [
        "# 🔥 Priority — Fresh Summer 2027 Roles",
        "",
        f"_**Pulled:** {now}  —  {len(hits)} role(s) explicitly tagged Summer 2027 "
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
        lines += ["| Company | Role | Posted | Applied | Link |", "|---|---|---|---|---|"]
        for row in hits:
            company, role, link, posted = row[0], row[1], row[2], row[3]
            role = role.replace("|", "\\|")
            company = company.replace("|", "\\|")
            mark = "✅" if is_applied(link, applied) else "—"
            lines.append(f"| {company} | {role} | {posted} | {mark} | [Apply]({link}) |")
        lines.append("")
    lines.append("---")
    lines.append(f"_Auto-generated. Window: {PRIORITY_DAYS} days (edit `PRIORITY_DAYS` in `search.py`). See `README.md` for everything._")
    return "\n".join(lines)


def main():
    blacklist = load_blacklist()
    applied = load_applied()
    if blacklist:
        print(f"Loaded {len(blacklist)} blacklist entries")
    if applied:
        print(f"Loaded {len(applied)} applied entries")

    # Collect from every source, tagging each row with the source label it
    # came from so we can show provenance and dedupe across them.
    labeled = []  # list of (label, (company, role, url, posted))

    print("Community lists (JSON):")
    for label, candidates in SIMPLIFY_SCHEMA_SOURCES:
        for row in source_simplify_schema(label, candidates):
            labeled.append((label, row))

    print("Community lists (markdown):")
    for label, url in MARKDOWN_TABLE_SOURCES:
        for row in source_markdown_table(label, url):
            labeled.append((label, row))

    print("Company boards:")
    for row in source_greenhouse():
        labeled.append(("Greenhouse boards", row))
    print(f"  [Greenhouse] done")
    for row in source_lever():
        labeled.append(("Lever boards", row))
    print(f"  [Lever] done")

    raw_total = len(labeled)

    # ---- Global dedupe across ALL sources ----
    # dedupe() works on bare rows, so we run it on the rows while keeping a
    # parallel label map keyed by row identity (url or normalized pair).
    rows_only = [r for _, r in labeled]
    deduped = dedupe(rows_only)
    print(f"Dedupe: {raw_total} -> {len(deduped)} ({raw_total - len(deduped)} duplicates removed)")

    # Re-attach the source label to each surviving row (first source wins,
    # matching dedupe's keep-first behavior).
    label_for = {}
    for label, row in labeled:
        url_key = (row[2] or "").strip().lower().rstrip("/")
        pair_key = (_norm(row[0]), _norm(row[1]))
        label_for.setdefault(url_key, label)
        label_for.setdefault(pair_key, label)

    def lookup_label(row):
        url_key = (row[2] or "").strip().lower().rstrip("/")
        pair_key = (_norm(row[0]), _norm(row[1]))
        return label_for.get(url_key) or label_for.get(pair_key) or "Other"

    # ---- Filters ----
    if blacklist:
        before = len(deduped)
        deduped = [r for r in deduped if not is_blacklisted(r[2], blacklist)]
        print(f"Blacklist removed {before - len(deduped)} listing(s)")

    if MAX_AGE_DAYS > 0:
        before = len(deduped)
        deduped = [
            r for r in deduped
            if is_applied(r[2], applied) or not too_old(r[3])
        ]
        print(f"Age cutoff ({MAX_AGE_DAYS}d) removed {before - len(deduped)} listing(s)")

    # ---- Bucket by source for display ----
    buckets = {}
    for row in deduped:
        buckets.setdefault(lookup_label(row), []).append(row)

    readme = build_readme(buckets, linkedin_search_link(), applied=applied,
                          influential_rows=deduped)
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"Wrote README.md ({sum(len(v) for v in buckets.values())} total across {len(buckets)} sources)")

    priority = build_priority(deduped, applied=applied)
    with open("PRIORITY.md", "w", encoding="utf-8") as f:
        f.write(priority)
    print("Wrote PRIORITY.md")


if __name__ == "__main__":
    main()
