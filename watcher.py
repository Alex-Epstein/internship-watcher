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
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests

CONFIG_FILE = "config.json"
SEEN_FILE = "seen_jobs.json"
TIMEOUT = 15
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
    offset, limit = 0, 20
    total = None
    max_pages = int(firm.get("max_pages", 25))
    for _ in range(max_pages):
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
        if not postings or (total is not None and offset >= total):
            break
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
            "sponsorship": (j.get("sponsorship") or ""),
            "year_text": f"{title} {season_text} {cycle_year}",
        })
    return out


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


def fetch_github_md(firm):
    """
    Parse a tracker repo whose data lives in a markdown TABLE (not listings.json).
    Handles both common shapes:
      | Company | Role | Location | [apply](url) | Added |          (sndsh404)
      | <a href=co><b>Co</b></a> | Position | Loc | $/hr | <a href=url><img></a> | Age |  (speedyapply)
    Config: url (raw README). Optional: cycle_year (injected so year-less titles
    still pass the year filter, since the whole repo is one cycle).
    """
    r = requests.get(firm["url"], headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    cycle_year = str(firm.get("cycle_year", ""))

    def clean(cell):
        cell = re.sub(r"<[^>]+>", " ", cell)                 # strip html tags
        cell = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", cell)  # md links -> text
        cell = re.sub(r"[*`|]", " ", cell)
        return re.sub(r"\s+", " ", cell).strip()

    out, last_company = [], ""
    for line in r.text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.count("|") < 4:
            continue
        if re.match(r"^\|[\s\-:|]+\|$", line):               # separator row
            continue
        cells = [c for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue

        company = clean(cells[0])
        title = clean(cells[1])
        location = clean(cells[2]) if len(cells) > 2 else ""
        if not title or company.lower() in ("company",) or title.lower() in ("position", "role"):
            continue                                          # header row
        if company in ("↳", "->", "") and last_company:       # "same as above" marker
            company = last_company
        last_company = company or last_company

        # apply link = a URL from the later cells (cell 0 is the company homepage)
        urls = []
        for c in cells[1:]:
            urls += re.findall(r"https?://[^\s\"')<>]+", c)
        if not urls:
            continue
        link = urls[0].rstrip(").,")

        out.append({
            "id": link,
            "title": title,
            "company": company,
            "location": location,
            "url": link,
            "content": "",
            "year_text": f"{title} {cycle_year}",
        })
    return out


def fetch_smartrecruiters(firm):
    token = firm["token"]
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location", {}) or {}
        out.append({
            "id": str(j.get("id", "")),
            "title": j.get("name", "") or "",
            "location": ", ".join(x for x in [loc.get("city"), loc.get("region"),
                                              loc.get("country")] if x),
            "url": f"https://jobs.smartrecruiters.com/{token}/{j.get('id','')}",
            "content": "",
        })
    return out


def fetch_workable(firm):
    token = firm["token"]
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": str(j.get("shortcode") or j.get("id") or ""),
            "title": j.get("title", "") or "",
            "location": ", ".join(x for x in [j.get("city"), j.get("state"),
                                              j.get("country")] if x),
            "url": j.get("url") or j.get("application_url") or "",
            "content": (j.get("description", "") or "").lower(),
        })
    return out


def _classify_board_url(u):
    """Turn any apply URL into a pollable board spec, or None."""
    if "jobs.lever.co/" in u:
        m = re.search(r"lever\.co/([A-Za-z0-9\-_.]+)", u)
        return {"ats": "lever", "token": m.group(1)} if m else None
    if "greenhouse.io" in u:
        m = (re.search(r"[?&]for=([A-Za-z0-9\-_.]+)", u)
             or re.search(r"greenhouse\.io/([A-Za-z0-9\-_.]+)", u))
        if m and m.group(1) not in ("embed", "job_board", "v1", "boards"):
            return {"ats": "greenhouse", "token": m.group(1)}
        return None
    if "jobs.ashbyhq.com/" in u:
        m = re.search(r"jobs\.ashbyhq\.com/([A-Za-z0-9\-_.]+)", u)
        return {"ats": "ashby", "token": m.group(1)} if m else None
    if "myworkdayjobs.com" in u:
        m = re.search(r"https?://([^/]*myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", u)
        if m:
            host = m.group(1)
            site = m.group(2)
            if site.lower() in ("job", "jobs"):
                return None
            return {"ats": "workday", "host": host, "tenant": host.split(".")[0],
                    "site": site, "locale": "en-US", "search_text": "intern"}
        return None
    if "smartrecruiters.com/" in u and "/api" not in u:
        m = re.search(r"smartrecruiters\.com/([A-Za-z0-9\-_.]+)", u)
        if m and m.group(1) not in ("api",):
            return {"ats": "smartrecruiters", "token": m.group(1)}
        return None
    if "apply.workable.com/" in u:
        m = re.search(r"apply\.workable\.com/([A-Za-z0-9\-_.]+)", u)
        if m and m.group(1) not in ("api", "j"):
            return {"ats": "workable", "token": m.group(1)}
    return None


def fetch_autodiscover(firm):
    """
    THE self-expanding source. Reads the tracker repos, harvests every apply URL,
    works out which ATS board each one belongs to, then polls that company's FULL
    board directly. Two big wins over reading the trackers alone:
      1. you see ALL of a company's intern roles, not just the one row a tracker listed
      2. you see them the hour they post, instead of waiting for a maintainer
    It grows by itself: any company a tracker ever adds gets polled from then on.
    """
    boards, out = {}, []
    for src in firm.get("sources", []):
        try:
            r = requests.get(src, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            text = r.text
        except Exception as e:  # noqa: BLE001
            print(f"    autodiscover: source failed ({e}) {src[:60]}")
            continue
        for u in re.findall(r"https?://[^\s\"'<>)\]\\]+", text):
            c = _classify_board_url(u)
            if not c:
                continue
            key = (c["ats"], c.get("token") or c.get("host"))
            if key not in boards:
                c["name"] = (c.get("token") or c.get("tenant") or "board")
                boards[key] = c

    print(f"    autodiscover: {len(boards)} boards found across trackers")

    # Poll boards in PARALLEL with a hard time budget -- sequentially this would
    # take hours (Workday tenants paginate), and a single slow board must never
    # be able to hang the whole run.
    budget = float(firm.get("budget_seconds", 600))
    deadline = time.time() + budget
    max_workers = int(firm.get("max_workers", 10))

    def poll(b):
        if time.time() > deadline:
            return []
        sub = FETCHERS.get(b["ats"])
        if not sub:
            return []
        if b["ats"] == "workday":
            b.setdefault("max_pages", 3)      # searchText=intern -> 60 hits is plenty
        try:
            jobs = sub(b)
        except Exception:  # noqa: BLE001 -- dead/renamed/blocked boards are expected
            return []
        for j in jobs:
            j.setdefault("company", b["name"])
        return jobs

    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(poll, b) for b in boards.values()]
        for fut in as_completed(futures):
            try:
                jobs = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if jobs:
                ok += 1
                out.extend(jobs)

    elapsed = int(budget - max(0, deadline - time.time()))
    print(f"    autodiscover: {ok}/{len(boards)} boards returned postings, "
          f"{len(out)} raw, {elapsed}s")
    return out


def fetch_usajobs(firm):
    """
    USAJOBS = every federal internship & research opening in one API: NASA, DOE
    national labs, NSA, Army/Navy research labs, Pathways. Needs a FREE API key
    (https://developer.usajobs.gov/apirequest/), stored as repo secrets
    USAJOBS_API_KEY and USAJOBS_EMAIL. Skipped with a note if unset.
    """
    key = os.environ.get("USAJOBS_API_KEY")
    email = os.environ.get("USAJOBS_EMAIL")
    if not (key and email):
        raise RuntimeError(
            "no USAJOBS_API_KEY/USAJOBS_EMAIL secret set -- get a free key at "
            "developer.usajobs.gov/apirequest to enable federal + NASA/DOE roles")
    h = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    out, seen_ids = [], set()
    for kw in firm.get("keywords", ["student intern software"]):
        try:
            r = requests.get("https://data.usajobs.gov/api/search",
                             params={"Keyword": kw, "ResultsPerPage": 250},
                             headers=h, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"    usajobs '{kw}' failed: {e}")
            continue
        for it in r.json().get("SearchResult", {}).get("SearchResultItems", []):
            d = it.get("MatchedObjectDescriptor", {}) or {}
            jid = str(it.get("MatchedObjectId") or d.get("PositionID") or "")
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            locs = d.get("PositionLocation", []) or []
            out.append({
                "id": jid,
                "title": d.get("PositionTitle", "") or "",
                "company": (d.get("OrganizationName") or "Federal"),
                "location": "; ".join(l.get("LocationName", "") for l in locs[:3]),
                "url": d.get("PositionURI", "") or "",
                "content": (d.get("QualificationSummary", "") or "").lower(),
            })
        time.sleep(0.3)
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "workday": fetch_workday,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "amazon": fetch_amazon,
    "usajobs": fetch_usajobs,
    "github_json": fetch_github_json,
    "github_md": fetch_github_md,
    "autodiscover": fetch_autodiscover,
    "nuft": fetch_nuft,
    "pagewatch": fetch_pagewatch,
}


US_STATE_RE = re.compile(
    r",\s*(al|ak|az|ar|ca|co|ct|dc|de|fl|ga|hi|ia|id|il|in|ks|ky|la|ma|md|me|mi|mn|"
    r"mo|ms|mt|nc|nd|ne|nh|nj|nm|nv|ny|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|va|vt|wa|wi|wv|wy)\b"
)


# ----------------------------- filtering ----------------------------------- #
# Every drop is counted (and sampled) so filters can never silently eat roles
# again -- see gotcha #2 in CLAUDE.md. Printed at the end of each run.
DROP_COUNTS = {}
DROP_SAMPLES = {}


def _drop(reason, title):
    DROP_COUNTS[reason] = DROP_COUNTS.get(reason, 0) + 1
    samples = DROP_SAMPLES.setdefault(reason, [])
    if len(samples) < 3:
        samples.append(title)
    return False


_KW_RES = {}


def _title_is_internship(title, keywords):
    """Word-boundary match: 'intern' matches Intern / Interns / Internship(s)
    but NOT Internal / International / Internals / Internet. Plain substring
    matching here once flooded an email with 'Internal Audit' directors and
    'International Sales' managers."""
    for k in keywords:
        k = k.lower()
        rx = _KW_RES.get(k)
        if rx is None:
            rx = re.compile(r"\b" + re.escape(k) + r"(s|ship|ships)?\b")
            _KW_RES[k] = rx
        if rx.search(title):
            return True
    return False


def _has_term(title, term):
    """Whole-word match for short abbreviations (ai, ml, cv) so they don't match
    inside words like 'training' or 'email'. Longer terms must START at a word
    boundary -- prefix matching keeps 'develop'->development and
    'quant'->quantitative, while 'systems' no longer matches 'ecosystems'."""
    term = term.lower()
    if len(term) <= 3:
        return re.search(r"\b" + re.escape(term) + r"\b", title) is not None
    return re.search(r"\b" + re.escape(term), title) is not None


def is_relevant(job, filters):
    title = job["title"].lower()

    # 1) must look like an internship (word-boundary: 'intern' != 'internal')
    keywords = filters.get("title_keywords", [])
    if keywords and not _title_is_internship(title, keywords):
        return _drop("no-intern-word", title)

    # 2) must be in a domain you care about (skip this gate if the list is empty)
    require = filters.get("title_require_any", [])
    if require and not any(_has_term(title, t) for t in require):
        return _drop("no-domain-match", title)

    # 3) drop anything explicitly excluded (PhD / Masters / etc.)
    for bad in filters.get("title_exclude", []):
        if bad.lower() in title:
            return _drop(f"excluded:{bad}", title)

    # 4) CYCLE CHECK.
    #    Recruiting runs ~a year ahead, so a LIVE intern posting that states no year
    #    is almost always the current (2027) cycle -- most companies never put the
    #    year in the title (e.g. Palantir's "... - Internship - Intel"). So:
    #      a) if the TITLE names any year(s), one of them must be ours
    #      b) otherwise check the description/tracker text; if it names another
    #         cycle, drop -- if it names nothing, keep.
    years = [str(y) for y in filters.get("years", [])]
    title_years = set(re.findall(r"\b(20\d{2})\b", title))
    if years and title_years:
        if not (title_years & set(years)):
            return _drop("wrong-year-in-title", title)
    elif years:
        hay = " ".join([
            (job.get("year_text") or ""),
            title,
            (job.get("content") or "")[:4000],
        ]).lower()
        if not any(y in hay for y in years):
            if any(p.lower() in hay for p in filters.get("reject_cycle_phrases", [])):
                return _drop("wrong-cycle-phrase", title)

    # 5) location: drop foreign-only postings, but KEEP anything that also lists a
    #    US location (e.g. "Chicago; London" stays, "Amsterdam; Mumbai" goes)
    location = (job.get("location") or "").lower()
    excl = filters.get("location_exclude", [])
    if location and excl and any(b.lower() in location for b in excl):
        us = filters.get("location_us_markers", [])
        has_us = any(m.lower() in location for m in us) or bool(US_STATE_RE.search(location))
        if not has_us:
            return _drop("excluded-location", title)

    return True


def is_clearance(job, filters):
    """
    True if a role requires U.S. citizenship or a security clearance -- i.e. roles
    most applicants are ineligible for. These get their own priority section in
    the email. Checks the tracker's sponsorship field, the title, and (where the
    ATS gives us one) the job description.
    """
    kws = [k.lower() for k in filters.get("clearance_keywords", [])]
    if not kws:
        return False
    hay = " ".join([
        job.get("title", "") or "",
        job.get("sponsorship", "") or "",
        (job.get("content", "") or "")[:6000],
    ]).lower()
    return any(k in hay for k in kws)


# ----------------------------- email --------------------------------------- #
def _collapse_locations(jobs):
    """One line per role: the same company+title posted in N locations becomes
    a single entry ('New York, Palo Alto +2 more') linking to the first URL.
    Display-only -- every posting is still tracked individually in seen state."""
    merged, order = {}, []
    for j in jobs:
        key = ((j.get("company") or "").lower(), j["title"].strip().lower())
        if key not in merged:
            m = dict(j)
            m["_locs"] = []
            merged[key] = m
            order.append(key)
        loc = (j.get("location") or "").strip()
        if loc and loc not in merged[key]["_locs"]:
            merged[key]["_locs"].append(loc)
    out = []
    for key in order:
        m = merged[key]
        locs = m.pop("_locs")
        if len(locs) > 3:
            m["location"] = " · ".join(locs[:3]) + f" +{len(locs) - 3} more"
        else:
            m["location"] = " · ".join(locs)
        out.append(m)
    return out


def _job_li(job, with_company=True):
    company = job.get("company") if with_company else None
    pin = "&#128205; " if job.get("_ploc") else ""
    label = pin + (f"{escape(company)} &mdash; {escape(job['title'])}"
             if company else escape(job["title"]))
    loc = f" &mdash; {escape(job['location'])}" if job["location"] else ""
    return f"<li><a href='{escape(job['url'])}'>{label}</a>{loc}</li>"


def _mark_and_sort_priority(jobs, filters):
    """Pin-mark and float roles in Alex's priority cities (NYC/SF/SD/Boston/
    Miami/Philly) to the top of each group. Display-only."""
    plocs = [p.lower() for p in (filters or {}).get("priority_locations", [])]
    for j in jobs:
        j["_ploc"] = bool(plocs) and any(p in (j.get("location") or "").lower() for p in plocs)
    return sorted(jobs, key=lambda j: not j.get("_ploc"))


def build_email_html(grouped, baseline=False, filters=None):
    intro = (
        "Baseline of currently-open roles. Future emails will contain only "
        "<b>newly opened</b> postings."
        if baseline
        else "These internship postings just opened:"
    )
    parts = [f"<p>{intro}</p>"]

    # Split out clearance / US-citizen-required roles -- fewer people can apply
    # to these, so they lead the email.
    cleared, rest = [], {}
    for firm in grouped:
        for j in grouped[firm]:
            if j.get("clearance"):
                j = dict(j)
                j.setdefault("company", firm)
                cleared.append(j)
            else:
                rest.setdefault(firm, []).append(j)

    if cleared:
        cleared = _mark_and_sort_priority(_collapse_locations(cleared), filters)
        parts.append(
            "<div style='border-left:4px solid #b7791f;padding:6px 12px;margin:14px 0;"
            "background:#fffbeb'>"
            f"<h3 style='margin:4px 0'>US Citizen / Clearance required &mdash; "
            f"{len(cleared)} role(s)</h3>"
            "<p style='margin:2px 0;color:#666;font-size:12px'>Most applicants are "
            "ineligible for these. You are not.</p><ul>"
        )
        for j in cleared:
            parts.append(_job_li(j))
        parts.append("</ul></div>")

    for firm in sorted(rest):
        parts.append(f"<h3 style='margin:16px 0 4px'>{escape(firm)}</h3><ul>")
        for job in _mark_and_sort_priority(_collapse_locations(rest[firm]), filters):
            parts.append(_job_li(job))
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


# ----------------------------- open-roles report --------------------------- #
OPEN_ROLES_FILE = "OPEN_ROLES.md"


def write_open_roles(current):
    """Regenerate OPEN_ROLES.md every run: a browsable snapshot of every
    relevant role open right now (not just the new ones that get emailed).
    Committed alongside seen_jobs.json, so it's always live on GitHub."""
    cleared, by_src = [], {}
    for rec in current.values():
        j = dict(rec["job"])
        j.setdefault("company", rec["src"])
        (cleared if j.get("clearance") else by_src.setdefault(rec["src"], [])).append(j)

    def md_line(j):
        title = j["title"].replace("[", "(").replace("]", ")")
        company = (j.get("company") or "").replace("[", "(").replace("]", ")")
        loc = f" — {j['location']}" if j.get("location") else ""
        return f"- [{company} — {title}]({j.get('url', '')}){loc}"

    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [
        "# Open roles right now",
        "",
        f"_Auto-generated each run; do not hand-edit. Last update: {stamp}. "
        f"{len(current)} posting(s) currently open and matching filters._",
        "",
    ]
    if cleared:
        collapsed = _collapse_locations(cleared)
        lines += [f"## US Citizen / Clearance required ({len(collapsed)})", ""]
        lines += [md_line(j) for j in collapsed]
        lines.append("")
    for src in sorted(by_src):
        collapsed = _collapse_locations(by_src[src])
        lines += [f"## {src} ({len(collapsed)})", ""]
        lines += [md_line(j) for j in collapsed]
        lines.append("")

    with open(OPEN_ROLES_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"{OPEN_ROLES_FILE} written: {len(current)} open role(s).")


# ----------------------------- top picks ----------------------------------- #
TOP_PICKS_FILE = "TOP_PICKS.md"

# Cities Alex actually wants. Whitelist, so anything not listed (India, China,
# SE Asia, LatAm, etc.) is excluded automatically.
GOOD_LOC_RE = re.compile(
    r"new york|nyc|manhattan|brooklyn|"
    r"san francisco|\bsf\b|bay area|palo alto|menlo|mountain view|sunnyvale|"
    r"santa clara|san jose|redwood|cupertino|"
    r"boston|cambridge, ma|somerville|"
    r"chicago|evanston|"
    r"austin|dallas|houston|"
    r"seattle|bellevue|redmond|kirkland|"
    r"los angeles|santa monica|el segundo|pasadena|culver city|"
    r"miami|tampa|jupiter, fl|west palm|"
    r"philadelphia|bala cynwyd|"
    r"san diego|la jolla|"
    r"washington|arlington|mclean|reston|chantilly|bethesda|d\.c\.|\bdc\b|"
    r"atlanta|denver|boulder|"
    r"stamford|greenwich|"
    r"remote - us|remote, us|remote \(us|us remote|remote-us|"
    r"dublin|london|amsterdam|zurich|"
    r"\b(ny|ca|ma|il|tx|wa|fl|pa|va|md|ga|co|ct|nj)\b",
    re.I,
)
BAD_LOC_RE = re.compile(
    r"india|china|bangalore|hyderabad|pune|mumbai|delhi|chennai|gurgaon|noida|"
    r"shanghai|beijing|shenzhen|guangzhou|suzhou|hangzhou|wuhan|xiamen|hefei|"
    r"chengdu|zhongshan|malaysia|penang|kuala lumpur|philippines|manila|"
    r"vietnam|hanoi|ho chi minh|indonesia|jakarta|thailand|bangkok|taiwan|"
    r"taipei|hsinchu|tainan|korea|seoul|japan|tokyo|brazil|sao paulo|mexico|"
    r"guadalajara|monterrey|poland|krakow|warsaw|romania|bucharest|bulgaria|"
    r"sofia|egypt|cairo|turkey|israel|argentina|cordoba|belarus|minsk|"
    r"sri lanka|africa|dubai|riyadh|saudi|new zealand|auckland|australia|"
    r"sydney|melbourne|canada|toronto|vancouver|ottawa|montreal",
    re.I,
)
# Roles he wants: quant + SWE + ML/AI. Not hardware/mech/test/validation.
WANT_RE = re.compile(
    r"quant|trading|trader|software eng|software dev|swe\b|backend|back-end|"
    r"full.?stack|infrastructure|platform|systems eng|distributed|devops|sre\b|"
    r"site reliability|machine learning|deep learning|\bml\b|\bai\b|artificial "
    r"intelligence|computer vision|\bcv\b|\bnlp\b|\bllm\b|research eng|"
    r"applied scien|research scien|data eng|data scien|algorithm|forward deployed",
    re.I,
)
SKIP_RE = re.compile(
    r"hardware|mechanical|electrical|\bfpga\b|asic|analog|circuit|rf eng|"
    r"manufactur|process eng|quality|test eng|validation|verification|"
    r"industrial|chemical|materials|thermal|packaging|supply chain|"
    r"technician|field eng|sales|marketing|business|recruit|hr\b|people ops",
    re.I,
)
SWEET_SPOT = [
    "transmarket", "akuna", "virtu", "gts", "old mission", "wolverine",
    "belvedere", "geneva", "peak6", "group one", "allston", "3red",
    "dv trading", "chicago trading", "ctc", "voloridge", "schonfeld",
    "exoduspoint", "voleon", "worldquant", "weiss", "flow traders", "ice ",
    "intercontinental", "walleye", "capula", "arrowstreet", "aquatic",
]
ELITE = [
    "jane street", "hudson river", "hrt", "citadel", "imc", "optiver",
    "two sigma", "jump trading", "drw", "point72", "cubist", "susquehanna",
    "sig ", "tower research", "five rings", "xtx", "radix", "pdt", "headlands",
    "d. e. shaw", "d.e. shaw", "deshaw",
]


def _tier(company):
    c = (company or "").lower()
    if any(s in c for s in SWEET_SPOT):
        return 0
    if any(e in c for e in ELITE):
        return 2
    return 1


def write_top_picks(current):
    """Curated subset of OPEN_ROLES: quant/SWE/ML only, target cities only,
    sweet-spot firms first. Regenerated every full sweep like OPEN_ROLES.md."""
    picks = []
    for rec in current.values():
        j = rec["job"]
        title = j.get("title", "")
        loc = j.get("location", "") or ""
        comp = j.get("company") or rec["src"]
        if not WANT_RE.search(title) or SKIP_RE.search(title):
            continue
        if BAD_LOC_RE.search(loc):
            continue
        if loc and not GOOD_LOC_RE.search(loc):
            continue
        picks.append((_tier(comp), comp.lower(), title, comp, loc, j.get("url", ""),
                      bool(j.get("clearance"))))
    picks.sort(key=lambda p: (p[0], p[1], p[2]))

    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [
        "# Top picks (auto-generated)",
        "",
        f"_Quant / SWE / ML roles in target cities only. {len(picks)} of "
        f"{len(current)} open roles. Rebuilt every sweep: {stamp}._",
        "",
        "Sections: sweet-spot firms first, then everything else, elite last.",
        "",
    ]
    headers = {0: "## 🎯 Sweet spot (apply first)",
               1: "## Other relevant roles",
               2: "## Elite tier (low odds, cheap to apply)"}
    seen_tier = None
    for tier, _, title, comp, loc, url, clr in picks:
        if tier != seen_tier:
            lines += ["", headers[tier], ""]
            seen_tier = tier
        flag = " 🇺🇸" if clr else ""
        t = title.replace("[", "(").replace("]", ")")
        c = comp.replace("[", "(").replace("]", ")")
        lines.append(f"- [{c} — {t}]({url}){flag}" + (f" — {loc}" if loc else ""))

    with open(TOP_PICKS_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"{TOP_PICKS_FILE} written: {len(picks)} pick(s).")


# ----------------------------- weekly digest -------------------------------- #
def send_weekly_digest():
    """Sunday email: PROGRAMS.md content (scholarships/fellowships/REU deadlines)
    plus a reminder link to the live OPEN_ROLES.md. Separate from the hourly
    new-role alerts -- this is the 'don't forget the whole calendar' nudge."""
    try:
        programs = open("PROGRAMS.md", encoding="utf-8").read()
    except FileNotFoundError:
        print("PROGRAMS.md not found; skipping digest.")
        return
    html_body = re.sub(r"^# ", "<h2>", programs, flags=re.M)
    html_body = re.sub(r"^## (.*)$", r"<h3>\1</h3>", html_body, flags=re.M)
    html_body = re.sub(r"^- (.*)$", r"<li>\1</li>", html_body, flags=re.M)
    html_body = html_body.replace("\n\n", "<br><br>")
    html = (
        "<p>Weekly reminder: scholarships, fellowships, REUs, competitions, "
        "abroad programs -- everything with a deadline that isn't a normal "
        "internship posting.</p>"
        "<p>Live open-roles snapshot: see OPEN_ROLES.md in the repo.</p>"
        f"<hr>{html_body}"
    )
    send_email("[Internship Watcher] Weekly programs & deadlines digest", html)


# ----------------------------- main ---------------------------------------- #
def main():
    if os.environ.get("DIGEST_MODE") == "1":
        send_weekly_digest()
        return

    config = load_json(CONFIG_FILE, None)
    if not config:
        print(f"ERROR: {CONFIG_FILE} is missing or invalid.", file=sys.stderr)
        sys.exit(1)

    filters = config.get("filters", {})
    seen = load_json(SEEN_FILE, {}) or {}
    first_run = len(seen) == 0

    current = {}        # key -> job (everything relevant right now)
    grouped_new = {}    # firm -> [jobs] (relevant AND not seen before)
    sigs_this_run = set()  # company|title|location, for cross-source dedup

    # Hard ceiling on the whole sweep. If we blow through it, stop polling and
    # send what we have -- an email with most of the roles beats no email.
    run_budget = int(os.environ.get("RUN_BUDGET_SECONDS", "1500"))
    started = time.time()
    sweep_complete = True

    for firm in config.get("firms", []):
        if not firm.get("enabled", True):
            continue
        if time.time() - started > run_budget:
            print("  ! run budget hit -- skipping remaining sources this run")
            sweep_complete = False
            break
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
        for j in relevant:
            j["clearance"] = is_clearance(j, filters)
        n_clear = sum(1 for j in relevant if j["clearance"])
        print(f"  ok {name}: {len(jobs)} jobs, {len(relevant)} relevant"
              + (f" ({n_clear} clearance/US-citizen)" if n_clear else ""))
        for j in relevant:
            url = (j.get("url") or "").strip().lower()
            gkey = url if url else f"{name}:{j['id']}"
            # secondary dedup: same company+title+location from a different URL
            sig = "|".join([
                (j.get("company") or name).lower().strip(),
                (j.get("title") or "").lower().strip(),
                (j.get("location") or "").lower().strip(),
            ])
            if gkey in current or (not j.get("bypass_filters") and sig in sigs_this_run):
                continue
            sigs_this_run.add(sig)
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
                build_email_html(grouped, baseline=True, filters=filters),
            )
        else:
            print("Baseline run: no relevant roles open right now.")
    else:
        total_new = sum(len(v) for v in grouped_new.values())
        if total_new:
            send_email(
                f"[Internship Watcher] {total_new} new role(s) just opened",
                build_email_html(grouped_new, filters=filters),
            )
        else:
            print("No new roles this run.")

    # Only rewrite the snapshot after a FULL sweep -- a budget-truncated run
    # would shrink the file to just the sources it reached.
    if sweep_complete:
        write_open_roles(current)
        write_top_picks(current)
    else:
        print(f"{OPEN_ROLES_FILE} not rewritten (partial sweep).")

    # Weekly programs digest: the 13:00 UTC Sunday run (9am ET) mails
    # PROGRAMS.md so upcoming windows (SMART, SULI, NREIP...) never slip.
    now = time.gmtime()
    if now.tm_wday == 6 and now.tm_hour == 13:
        try:
            prog = open("PROGRAMS.md", encoding="utf-8").read()
            send_email(
                "[Internship Watcher] Weekly programs digest -- apply early",
                "<pre style='font-family:monospace;font-size:13px'>"
                + escape(prog) + "</pre>",
            )
        except Exception as e:  # noqa: BLE001
            print(f"  x weekly digest skipped: {e}")

    if DROP_COUNTS:
        top = sorted(DROP_COUNTS.items(), key=lambda kv: -kv[1])
        print("Filter drops this run: "
              + ", ".join(f"{k}={v}" for k, v in top[:12]))
        for k, _ in top[:5]:
            print(f"    e.g. {k}: " + " | ".join(DROP_SAMPLES[k]))

    save_json(SEEN_FILE, new_seen)
    print(f"State saved: {len(new_seen)} known role(s).")


if __name__ == "__main__":
    main()
