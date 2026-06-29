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
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

YEAR = "2027"
KEYWORDS = ("software", "swe", "engineer", "developer", "data", "ml", "infra", "backend", "frontend", "full stack", "full-stack")

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


def build_readme(buckets, linkedin_url):
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
        lines.append("> No matches this run. Sources may be rate-limited or the 2027 lists may not be populated yet — it will retry on the next scheduled run.")
        lines.append("")
    for source_name, rows in buckets.items():
        if not rows:
            continue
        lines.append(f"## {source_name} ({len(rows)})")
        lines.append("")
        lines.append("| Company | Role | Posted | Link |")
        lines.append("|---|---|---|---|")
        # newest first; rows with no date sink to the bottom
        rows_sorted = sorted(rows, key=lambda r: r[3] or "", reverse=True)
        seen = set()
        for company, role, link, posted in rows_sorted:
            key = (company, role)
            if key in seen:
                continue
            seen.add(key)
            role = role.replace("|", "\\|")
            company = company.replace("|", "\\|")
            posted = posted or "—"
            lines.append(f"| {company} | {role} | {posted} | [Apply]({link}) |")
        lines.append("")
    lines.append("---")
    lines.append("_Generated automatically by GitHub Actions. Edit `search.py` to add sources or change keywords._")
    return "\n".join(lines)


def main():
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
    readme = build_readme(buckets, linkedin_search_link())
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"Wrote README.md ({sum(len(v) for v in buckets.values())} total)")


if __name__ == "__main__":
    main()
