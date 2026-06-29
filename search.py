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

UA = "Mozilla/5.0 (compatible; internship-tracker/1.0)"


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


# ---------- Source 1: community GitHub internship list ----------
def source_github_list():
    """
    The SimplifyJobs / pittcsc Summer-internships repos store every listing as
    structured JSON at .github/scripts/listings.json -- far more reliable than
    parsing the 27k-line README. We try the 2027 repo first; when it doesn't
    exist yet (it's created partway through the prior year) we fall back to the
    current 2026 list automatically.
    """
    results = []
    candidates = [
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    ]
    data = None
    for url in candidates:
        data = fetch(url, is_json=True)
        if data:
            print(f"  using {url.split('/Summer')[1].split('-')[0]} list")
            break
    if not data:
        return results
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
            results.append((company, title, url, posted))
    return results[:60]


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
                results.append((board.capitalize(), title, job.get("absolute_url", ""), posted))
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
                results.append((board.capitalize(), title, job.get("hostedUrl", ""), posted))
        time.sleep(0.3)
    return results


# ---------- LinkedIn: link only, not scraped ----------
def linkedin_search_link():
    q = urllib.parse.quote(f"software engineer intern {YEAR}")
    return f"https://www.linkedin.com/jobs/search/?keywords={q}&f_E=1&f_JT=I"


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


def build_readme(buckets, linkedin_url, applied=None):
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
        for company, role, link, posted in rows_sorted:
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
        f"Add filled roles to `blacklist.txt` to hide them permanently. "
        f"Generated automatically by GitHub Actions._"
    )
    return "\n".join(lines)


def main():
    blacklist = load_blacklist()
    applied = load_applied()
    if blacklist:
        print(f"Loaded {len(blacklist)} blacklist entries")
    if applied:
        print(f"Loaded {len(applied)} applied entries")

    print("Searching GitHub community list...")
    gh = source_github_list()
    print(f"  {len(gh)} matches")

    print("Searching Greenhouse boards...")
    ghouse = source_greenhouse()
    print(f"  {len(ghouse)} matches")

    print("Searching Lever boards...")
    lever = source_lever()
    print(f"  {len(lever)} matches")

    buckets = {
        "Community list (Simplify/pittcsc)": gh,
        "Greenhouse company boards": ghouse,
        "Lever company boards": lever,
    }

    # Filter 1: drop blacklisted listings (link is index 2 in each tuple)
    if blacklist:
        removed = 0
        for name, rows in buckets.items():
            kept = [r for r in rows if not is_blacklisted(r[2], blacklist)]
            removed += len(rows) - len(kept)
            buckets[name] = kept
        print(f"Blacklist removed {removed} listing(s)")

    # Filter 2: drop postings older than the age cutoff (posted is index 3).
    # Applied jobs are kept regardless, so an old-but-applied row stays visible.
    if MAX_AGE_DAYS > 0:
        aged = 0
        for name, rows in buckets.items():
            kept = [
                r for r in rows
                if is_applied(r[2], applied) or not too_old(r[3])
            ]
            aged += len(rows) - len(kept)
            buckets[name] = kept
        print(f"Age cutoff ({MAX_AGE_DAYS}d) removed {aged} listing(s)")

    readme = build_readme(buckets, linkedin_search_link(), applied=applied)
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"Wrote README.md ({sum(len(v) for v in buckets.values())} total)")


if __name__ == "__main__":
    main()
