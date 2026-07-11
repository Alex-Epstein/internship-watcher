#!/usr/bin/env python3
"""
Internship watcher
==================
Polls Greenhouse + Lever + Workday job boards for a configurable list of firms,
remembers every relevant posting it has already seen, and emails you the moment
a NEW relevant internship appears.

- First run  -> emails a "baseline" of everything currently open, then remembers it.
- Later runs -> email ONLY postings that weren't there last time.

Config lives in config.json. State lives in seen_jobs.json (created/updated by the
GitHub Action). Email goes over SMTP using credentials in environment variables.
"""

import json
import os
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests

CONFIG_FILE = "config.json"
SEEN_FILE = "seen_jobs.json"
TIMEOUT = 25
# A browser-like User-Agent reduces the chance Workday's bot filter blocks us.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# ----------------------------- small helpers ------------------------------- #
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ----------------------------- board fetchers ------------------------------ #
# Each fetcher takes the firm dict from config and returns a list of normalized
# jobs: {id, title, location, url, content(lowercased)}.

def fetch_greenhouse(firm):
    token = firm["token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": str(j.get("id")),
            "title": j.get("title", "") or "",
            "location": (j.get("location") or {}).get("name", "") or "",
            "url": j.get("absolute_url", "") or "",
            "content": (j.get("content", "") or "").lower(),
        })
    return out


def fetch_lever(firm):
    token = firm["token"]
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories", {}) or {}
        out.append({
            "id": str(j.get("id", "")),
            "title": j.get("text", "") or "",
            "location": cats.get("location", "") or "",
            "url": j.get("hostedUrl", "") or "",
            "content": (j.get("descriptionPlain", "") or "").lower(),
        })
    return out


def fetch_workday(firm):
    """
    Poll a Workday tenant's public CXS feed.
    Required config fields: host, tenant, site. Optional: locale (default en-US).
    Find these in DevTools: the careers page POSTs to
    https://{host}/wday/cxs/{tenant}/{site}/jobs
    where host = {tenant}.wd{N}.myworkdayjobs.com
    """
    host = firm["host"]
    tenant = firm["tenant"]
    site = firm["site"]
    locale = firm.get("locale", "en-US")
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

    out = []
    offset, limit, total = 0, 20, None
    while True:
        r = requests.post(
            api,
            json={"appliedFacets": {}, "limit": limit, "offset": offset,
                  "searchText": firm.get("search_text", "")},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", []) or []
        if total is None:
            total = data.get("total", 0)
        for p in postings:
            path = p.get("externalPath", "") or ""
            link = f"https://{host}/{locale}/{site}{path}" if path else f"https://{host}/{locale}/{site}"
            out.append({
                "id": path or (p.get("title", "") or ""),
                "title": p.get("title", "") or "",
                "location": p.get("locationsText", "") or "",
                "url": link,
                "content": "",  # listing has no description; year must be in title
            })
        offset += limit
        if not postings or (total is not None and offset >= total) or offset > 2000:
            break
        time.sleep(0.3)
    return out


def fetch_github_json(firm):
    """
    Poll a community internship-tracker repo that publishes a machine-readable
    listings.json (the Simplify / Pitt CSC / vanshb03 family format). One source
    can cover hundreds of companies.
    Config fields: url (raw listings.json). Optional: cycle_year (e.g. "2027",
    injected so repo-scoped listings pass the year filter even when the title has
    no year), seasons (e.g. ["Summer"] to drop Winter/Fall entries).
    """
    url = firm["url"]
    cycle_year = str(firm.get("cycle_year", ""))
    seasons = [s.lower() for s in firm.get("seasons", [])]
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        if not isinstance(j, dict):
            continue
        if j.get("active") is False or j.get("is_visible") is False:
            continue
        title = (j.get("title") or "").strip()
        company = (j.get("company_name") or j.get("company") or "").strip()
        locs = j.get("locations") or j.get("location") or []
        location = ", ".join(str(x) for x in locs) if isinstance(locs, list) else str(locs)
        link = j.get("url") or j.get("application_link") or ""
        jid = str(j.get("id") or link or f"{company}|{title}")
        terms = j.get("terms") or []
        season_text = ((" ".join(terms) if isinstance(terms, list) else str(terms))
                       + " " + str(j.get("season") or "")).lower()
        if seasons and not any(s in season_text for s in seasons):
            continue
        out.append({
            "id": jid,
            "title": title,
            "company": company,
            "location": location,
            "url": link,
            "content": "",
            "year_text": f"{title} {season_text} {cycle_year}",
        })
    return out


def _classify_board_url(u):
    """Turn a greenhouse/lever/workday URL into a pollable source dict, or None."""
    if "lever.co/" in u:
        m = re.search(r"lever\.co/([A-Za-z0-9\-_]+)", u)
        return {"ats": "lever", "token": m.group(1)} if m else None
    if "greenhouse.io" in u:
        m = re.search(r"[?&]for=([A-Za-z0-9\-_]+)", u) or re.search(r"greenhouse\.io/([A-Za-z0-9\-_]+)", u)
        if m and m.group(1) not in ("embed", "job_board", "v1"):
            return {"ats": "greenhouse", "token": m.group(1)}
        return None
    if "myworkdayjobs.com" in u:
        m = re.search(r"https?://([^/]*myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", u)
        if m:
            host = m.group(1)
            return {"ats": "workday", "host": host, "tenant": host.split(".")[0],
                    "site": m.group(2), "locale": "en-US"}
    return None


def fetch_nuft(firm):
    """
    Meta-source: read the NUFT quant-internships README (markdown), extract every
    firm's Greenhouse/Lever/Workday board link, and poll each one. As NUFT adds
    apply links when firms open roles, this picks them up automatically.
    Note: firms whose only NUFT link is a plain marketing site (Jane Street, DE
    Shaw, SIG, etc.) can't be polled until a real board link appears for them.
    """
    r = requests.get(firm["url"], headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    text = r.text
    # split into firm sections by markdown headers
    sections, name, buf = [], None, []
    for ln in text.splitlines():
        h = re.match(r"^#{1,4}\s+(.*\S)\s*$", ln)
        if h:
            if name:
                sections.append((name, "\n".join(buf)))
            name = re.sub(r"[#*`]", "", h.group(1)).strip()
            buf = []
        else:
            buf.append(ln)
    if name:
        sections.append((name, "\n".join(buf)))

    skip = {"table of contents", "contributing", "license", "resources", "faq"}
    boards, seen = [], set()
    for sect_name, body in sections:
        if sect_name.lower() in skip:
            continue
        for u in re.findall(r"\((https?://[^)]+)\)", body):
            c = _classify_board_url(u)
            if not c:
                continue
            key = (c["ats"], c.get("token") or c.get("host"))
            if key in seen:
                continue
            seen.add(key)
            c["name"] = sect_name
            boards.append(c)

    out = []
    for b in boards:
        sub = FETCHERS.get(b["ats"])
        if not sub:
            continue
        try:
            jobs = sub(b)
        except Exception as e:  # noqa: BLE001 -- skip a bad board, keep going
            print(f"    NUFT/{b['name']} ({b['ats']}) skipped: {e}")
            continue
        for j in jobs:
            j["company"] = b["name"]
            out.append(j)
    print(f"    NUFT: discovered {len(boards)} pollable boards")
    return out


def fetch_pagewatch(firm):
    """
    Change-detector for feed-less pages (REUs, NASA OSTEM, lab portals). Fetches
    the page, reduces it to text, and alerts when it changes. With watch_keywords
    (e.g. ["2027","apply"]), it alerts specifically when those words appear/change
    on the page -- i.e. "tell me when applications open." Always bypasses the
    intern/domain/year filters.
    """
    import hashlib
    r = requests.get(firm["url"], headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", r.text)
    text = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip().lower()
    kws = [k.lower() for k in firm.get("watch_keywords", [])]
    signal = ",".join(sorted(k for k in kws if k in text)) if kws else text
    digest = hashlib.sha256(signal.encode("utf-8")).hexdigest()[:16]
    return [{
        "id": digest,
        "title": f"Page changed - check {firm.get('name', 'page')} (may mean applications opened)",
        "location": "",
        "url": firm["url"],
        "content": "",
        "bypass_filters": True,
    }]


def fetch_ashby(firm):
    """
    Ashby's public job-board API. Used by many AI labs / top startups (OpenAI etc).
    Token = the slug in jobs.ashbyhq.com/{token}
    """
    token = firm["token"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": str(j.get("id", "")),
            "title": j.get("title", "") or "",
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "content": (j.get("descriptionPlain", "") or "").lower(),
        })
    return out


def fetch_amazon(firm):
    """
    Amazon publishes no official jobs API; this calls the same undocumented
    endpoint amazon.jobs itself uses. Best-effort: if Amazon changes or blocks it,
    this source is simply skipped and logged (never crashes the run).
    Covers AWS, Amazon Robotics, Leo, etc. -- all under one board.
    """
    base = "https://www.amazon.jobs/en/search.json"
    query = firm.get("query", "intern")
    out, offset, limit = [], 0, 100
    for _ in range(8):  # page cap
        r = requests.get(base, params={
            "base_query": query, "offset": offset,
            "result_limit": limit, "sort": "recent",
        }, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        jobs = data.get("jobs", []) or []
        for j in jobs:
            path = j.get("job_path", "") or ""
            out.append({
                "id": str(j.get("id_icims") or j.get("id") or path),
                "title": j.get("title", "") or "",
                "location": (j.get("normalized_location") or j.get("location") or ""),
                "url": f"https://www.amazon.jobs{path}" if path else base,
                "content": (j.get("description", "") or "").lower(),
            })
        total = data.get("hits", 0) or 0
        offset += limit
        if not jobs or offset >= total:
            break
        time.sleep(0.3)
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "workday": fetch_workday,
    "ashby": fetch_ashby,
    "amazon": fetch_amazon,
    "github_json": fetch_github_json,
    "nuft": fetch_nuft,
    "pagewatch": fetch_pagewatch,
}


# ----------------------------- filtering ----------------------------------- #
def _has_term(title, term):
    """Whole-word match for short abbreviations (ai, ml, cv) so they don't match
    inside words like 'training' or 'email'; plain substring for longer terms."""
    term = term.lower()
    if len(term) <= 3:
        return re.search(r"\b" + re.escape(term) + r"\b", title) is not None
    return term in title


def is_relevant(job, filters):
    title = job["title"].lower()

    # 1) must look like an internship
    keywords = filters.get("title_keywords", [])
    if keywords and not any(k.lower() in title for k in keywords):
        return False

    # 2) must be in a domain you care about (skip this gate if the list is empty)
    require = filters.get("title_require_any", [])
    if require and not any(_has_term(title, t) for t in require):
        return False

    # 3) drop anything explicitly excluded (PhD / Masters / etc.)
    for bad in filters.get("title_exclude", []):
        if bad.lower() in title:
            return False

    # 4) year must appear in the title (ATS) or the listing's year text (repos)
    years = filters.get("years", [])
    if years:
        year_hay = (job.get("year_text") or job["title"]).lower()
        if not any(str(y) in year_hay for y in years):
            return False

    # 5) optional location exclusions (e.g. drop overseas offices)
    location = job.get("location", "").lower()
    for bad in filters.get("location_exclude", []):
        if bad.lower() in location:
            return False

    return True


# ----------------------------- email --------------------------------------- #
def build_email_html(grouped, baseline=False):
    intro = (
        "Baseline of currently-open roles. Future emails will contain only "
        "<b>newly opened</b> postings."
        if baseline
        else "These internship postings just opened:"
    )
    parts = [f"<p>{intro}</p>"]
    for firm in sorted(grouped):
        parts.append(f"<h3 style='margin:16px 0 4px'>{escape(firm)}</h3><ul>")
        for job in grouped[firm]:
            company = job.get("company")
            label = (f"{escape(company)} &mdash; {escape(job['title'])}"
                     if company else escape(job["title"]))
            loc = f" &mdash; {escape(job['location'])}" if job["location"] else ""
            parts.append(
                f"<li><a href='{escape(job['url'])}'>{label}</a>{loc}</li>"
            )
        parts.append("</ul>")
    parts.append(
        "<p style='color:#888;font-size:12px'>Sent automatically by your "
        "internship watcher.</p>"
    )
    return "\n".join(parts)


def send_email(subject, html):
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "465")
    user = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    to_addr = os.environ.get("EMAIL_TO") or user

    if not (user and password and to_addr):
        print("ERROR: set SMTP_USERNAME, SMTP_PASSWORD, and EMAIL_TO.", file=sys.stderr)
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as server:
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    print(f"Email sent to {to_addr}: {subject}")


# ----------------------------- main ---------------------------------------- #
def main():
    config = load_json(CONFIG_FILE, None)
    if not config:
        print(f"ERROR: {CONFIG_FILE} is missing or invalid.", file=sys.stderr)
        sys.exit(1)

    filters = config.get("filters", {})
    seen = load_json(SEEN_FILE, {}) or {}
    first_run = len(seen) == 0

    current = {}        # key -> job (everything relevant right now)
    grouped_new = {}    # firm -> [jobs] (relevant AND not seen before)

    for firm in config.get("firms", []):
        if not firm.get("enabled", True):
            continue
        name = firm.get("name", "?")
        fetcher = FETCHERS.get(firm.get("ats"))
        if not fetcher:
            print(f"  - {name}: skipped (unknown ats '{firm.get('ats')}')")
            continue
        try:
            jobs = fetcher(firm)
        except Exception as e:  # noqa: BLE001 -- skip any firm that errors, never crash
            print(f"  x {name} skipped: {e}")
            continue

        relevant = [j for j in jobs if j.get("bypass_filters") or is_relevant(j, filters)]
        print(f"  ok {name}: {len(jobs)} jobs, {len(relevant)} relevant")
        for j in relevant:
            url = (j.get("url") or "").strip().lower()
            gkey = url if url else f"{name}:{j['id']}"
            if gkey in current:   # already claimed by an earlier source this run
                continue
            current[gkey] = {"src": name, "job": j}
            if gkey not in seen:
                grouped_new.setdefault(name, []).append(j)
        time.sleep(0.3)  # be polite between firms

    # Remember everything currently relevant (merge so closed roles stay "seen")
    new_seen = dict(seen)
    for gkey, rec in current.items():
        new_seen[gkey] = {"title": rec["job"]["title"], "url": rec["job"].get("url", "")}

    if first_run:
        grouped = {}
        for gkey, rec in current.items():
            grouped.setdefault(rec["src"], []).append(rec["job"])
        if grouped:
            send_email(
                f"[Internship Watcher] Baseline: {len(current)} open role(s)",
                build_email_html(grouped, baseline=True),
            )
        else:
            print("Baseline run: no relevant roles open right now.")
    else:
        total_new = sum(len(v) for v in grouped_new.values())
        if total_new:
            send_email(
                f"[Internship Watcher] {total_new} new role(s) just opened",
                build_email_html(grouped_new),
            )
        else:
            print("No new roles this run.")

    save_json(SEEN_FILE, new_seen)
    print(f"State saved: {len(new_seen)} known role(s).")


if __name__ == "__main__":
    main()
