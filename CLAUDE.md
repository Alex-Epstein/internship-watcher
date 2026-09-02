# Internship Watcher

**CURRENT MODE (2026-08-07): two crons, both narrow.**
1. **Hourly role sweep — REINSTATED but narrowed to top-tier quant firms and the
   FALL 2027 cycle only.** Alex has his Summer 2027 NYC quant offer and is
   interviewing with Citadel for a Fall 2027 SWE seat. Driven by
   `top_firms_only` / `fall_2027_only` in `config.json → filters`; gates are
   `_is_top_firm()` (the `TOP_FIRMS` list) and `_is_fall_2027()`. Both log their
   drops (`[-N not-top-firm]`, `[-N not-fall-2027]`). Competition/program sources
   are skipped in this mode since Sunday already covers them.
   **Expect a quiet inbox** — as of 2026-08-07 zero Fall-2027 roles were open
   across 36 top-firm boards. Fall recruiting opens later in the year.
2. **Wed + Sun 14:00 UTC opportunities digest** (see "Opportunities digest").

`_is_fall_2027()` gotcha: a title naming another cycle ("Summer 2027 …") is that
cycle no matter what the body says. Without that guard, JDs mentioning a "fall
2027 return offer" were misread as fall roles.

Originally: hourly cron that polls job boards and emails the moment a new
Summer 2027 internship opens in CS / math / quant / AI / ML / CV / defense.

## Opportunities digest — Wed + Sun (the live feature)

Cron `0 14 * * 0,3` → `DIGEST_MODE=1` → `send_weekly_digest()`. Alex's rule
(2026-09-02): **every send is the COMPLETE current list**, not a diff — he pastes
it into an AI and asks what's best, so he never has to open an older email.
Twice weekly because rolling-acceptance events (Cornell Trading Comp etc.) need
pouncing on. Sections, in order:
1. **NEW since last send** — new `opportunities.json` entries, watched pages
   that changed, discovery-feed hits.
2. **Everything currently open** — every entry in `opportunities.json` whose
   deadline/event hasn't passed, sorted by deadline, "closing ≤10d" pulled out.
   **In-person spring entries are hidden** (`filters.hide_seasons=["spring"]`,
   `_hidden_season()`): he's in Madrid Jan–May 2027. Online spring stays.
3. **Discovery feed** — new items from `ats: "rss"` sources (Google News query
   feeds, Reddit r/quant, HN) via `fetch_rss()`, keyword-gated by
   `watch_keywords`. Unverified; he/Claude promote good ones into
   `opportunities.json`. First read of a feed is a silent baseline
   (`rssfeed::<url>` marker) so a new feed can't flood.
4. Index of every watched page.

`opportunities.json` is the structured source of truth (id, dates ISO, format,
season, travel, prizes, perks, eligibility). **Hand-curated** — pagewatch only
says "a page changed"; it can't extract dates. When he finds something (or a
watched page fires), add an entry. What he wants: **fun + travel + merch** —
in-person student trading comps / datathons / firm invitationals with travel
reimbursed. Not heavy quant recruiting; big tech / AI leaning.

A source is in the digest iff `_is_digest_source()`: explicit `"digest": true`
wins, else the `name:` prefix ∈ Event/Competition/Hackathon/Scholarship/
Fellowship/Abroad/Program/NatSec/Lab/GT/Conference/Grant/RSS. `Event:` = the
travel-paid in-person tier (university comps, SIG Discovery Day, IMC Launchpad,
Jane Street ETC, Citadel invitationals, aggregators openquant.co /
quantchallenges.com). Company job boards (`Quant SPA:`, `Page:`, `Firm SPA:`)
stay OUT — that's the hourly sweep.

**Not feasible (don't retry):** LinkedIn (no API, scraping blocked from
runners), Discord (needs a bot token in each server), Reddit beyond r/quant
(429s from CI), Devpost RSS (406), Meta/Citadel-students/Correlation One pages
(bot-blocked). Cornell was missed originally because none of this existed —
he found it on the Trading@GT Discord.

**Pagewatch keying (gotcha #9):** pagewatch state keys are
`pw::<url>::#<content-hash>` via `_pw_key()`. Keyed by URL alone — as it was
before 2026-08-03 — a watcher fires **exactly once, ever**, then goes silent, so
"tell me when applications open" never fires again. The dedicated prefix also
avoids colliding with job URLs that contain `#` fragments. A URL with no recorded
hash yet is baselined **silently**, so a keying change can't flood the first email.

## Who this is for (drives all filtering)

- Georgia Tech, B.S. Mathematics & Computer Science, **graduating May 2028**
- **U.S. Citizen with an active Secret security clearance** — still a major edge for
  defense/gov roles, but as of 2026-07-22 clearance roles are **no longer broken into
  their own email section** (Alex's call). `clearance_keywords` / `is_clearance()` are
  still computed for logging, they just don't drive the email layout anymore.
- **Email is ranked into 4 categories, in this exact order** (Alex's priority — he's
  locked on quant, NYC first): ① Quant in NYC ② Quant in other US locations
  ③ SWE / ML / AI (best location first) ④ everything else. Within each, NYC floats
  to the top, then other US hubs. See `build_email_html`.
- Targets: quant (dev / research / trading), SWE, ML / AI / CV, robotics, defense
  primes & defense tech, national labs, REUs / research programs.
- **Summer 2027 only. US-only** (2026-07-22): non-US roles are dropped from the email,
  TOP_PICKS, and OPEN_ROLES via `_is_us_location()` at the sweep's `relevant` stage
  (drops are logged `[-N non-US]`). Western Europe/AUS/Brazil, previously allowed, are
  now excluded. To re-enable a region, edit `NON_US_RE`.

## Layout

| File | Purpose |
|---|---|
| `.github/workflows/watch.yml` | Hourly cron + manual "Run workflow" button |
| `watcher.py` | All logic. Fetchers → filters → dedup → email |
| `config.json` | Sources + filters. **Most changes belong here, not in code.** |
| `seen_jobs.json` | State. Auto-committed each run. Never hand-edit. |
| `OPEN_ROLES.md` | Auto-generated snapshot of every role currently open, rewritten each full sweep and committed. Never hand-edit. |
| `applications.md` | **Private, gitignored** (repo is public!). Alex's application tracker: one section per role — company, status, resume used, notes. Claude edits this directly on request. Never commit it. |

**Secrets (repo → Settings → Secrets → Actions):** `SMTP_USERNAME` (a Gmail
address), `SMTP_PASSWORD` (Gmail **App Password**, not the account password),
`EMAIL_TO`. Optional: `USAJOBS_API_KEY` + `USAJOBS_EMAIL`.

## Architecture

Every entry in `config.json` → `firms[]` has an `ats` field that routes it to a
fetcher in `watcher.py`'s `FETCHERS` dict. Every fetcher returns normalized dicts:
`{id, title, company, location, url, content, ...}`.

| `ats` | What it does |
|---|---|
| `greenhouse` `lever` `ashby` `smartrecruiters` `workable` | Public ATS JSON APIs. `token` = the slug in the board URL. |
| `workday` | Undocumented-but-public CXS endpoint. Needs `host` / `tenant` / `site` — get them from the careers page's DevTools → Network → the POST to `/wday/cxs/.../jobs`. Use `search_text` to filter server-side and `max_pages` to cap. |
| `amazon` | Undocumented `amazon.jobs/en/search.json`. Covers AWS / Robotics / all. |
| `usajobs` | Federal: NASA, DOE labs, NSA, Army/Navy research, Pathways. Needs a free key. |
| `github_json` | Tracker repos publishing `listings.json` (vanshb03). |
| `github_md` | Tracker repos whose data is a markdown **table** (sndsh404, speedyapply). |
| `nuft` | Parses the NUFT quant README for board links, then polls them. |
| `autodiscover` | **The big one.** Harvests every apply URL from all trackers, decodes each company's ATS board, and polls ~211 boards **in parallel**. Self-expanding: any company a tracker adds gets polled from then on. |
| `pagewatch` | Change-detector for feed-less pages (NASA OSTEM, SULI, Lockheed, JHU APL...). Alerts when watched keywords appear/change. |

Failed sources are **skipped and logged** (`x <name> skipped`), never crash the run.
Read the Actions log to see which sources actually resolved.

## Filtering (`config.json` → `filters`)

1. `title_keywords` — must look like an internship
2. `title_require_any` — must be a CS/math domain (73 keywords)
3. `title_exclude` — PhD/Masters, off-cycle (spring/winter/fall), non-CS engineering
4. **Cycle check** — see gotcha #1 below
5. **US-only gate** (`_is_us_location`) — clearly-non-US roles are dropped after the
   relevance check; empty/ambiguous locations are kept (never silent-drop a US role)
6. `clearance_keywords` — still computed by `is_clearance()` for the run log, but no
   longer creates an email section (see "Who this is for")

## HARD-WON GOTCHAS — read before changing anything

1. **NEVER require the year in the job title.** Most companies don't put it there
   (Palantir: `"Forward Deployed Software Engineer - Internship - Intel"`). An
   earlier version required `"2027"` in the title and **silently discarded every
   such role** across all direct ATS sources for weeks.
   Current logic: if the title names *any* year, one of them must be ours; else
   check the description; **if no year appears anywhere, KEEP it** — a live intern
   posting is almost always the current cycle, since recruiting runs a year ahead.

2. **Silent drops are the most dangerous bug class.** #1 went unnoticed because a
   filtered-out role produces no log line. **If you add a filter, log what it drops.**

3. **Auto-discovery must stay parallel.** 211 boards polled sequentially takes
   *hours* (Workday paginates 20 at a time). Keep `ThreadPoolExecutor`,
   `max_pages=3`, and `budget_seconds`. A sequential version hung a run for 20+ min.

4. **`git push` must rebase first.** The `Save state` step does
   `git pull --rebase` before pushing — otherwise editing files via the GitHub web
   UI moves `main` and the run's state-commit fails with a non-fast-forward error.

5. **Community trackers go stale.** `sharunkumar` is really a *2026* repo; its few
   "2027" tags produced dead links and closed roles. It's disabled. **Verify a
   tracker's actual cycle before enabling it.**

6. **Trackers lag; boards don't.** Polling a company's board directly beats reading
   a tracker — you see the posting the hour it goes up instead of when a maintainer
   adds it. That's the whole point of `autodiscover`.

7. **Substring keyword matching floods the email with garbage.** `"intern" in
   title` matches Intern**al**, Intern**ational**, Intern**als**, Intern**et** —
   one baseline email had 100+ "Internal Audit" / "International Sales" directors.
   Same trap: `"systems"` matched "Eco**systems**". `_title_is_internship()` and
   `_has_term()` use word-boundary regexes; keep it that way for any new keyword
   gate. Filters now count every drop by reason (`DROP_COUNTS`) and print a
   summary at the end of each run — check it after any filter change.

8. **Emails show one line per role.** `_collapse_locations()` merges the same
   company+title posted in N cities into one entry ("NYC · Palo Alto +2 more").
   Display-only: every posting URL is still tracked individually in
   `seen_jobs.json`, so a role opening in a new city later still alerts.

## Pending / next upgrades

- **`SimplifyJobs/Summer2027-Internships` does not exist yet.** The moment it
  launches, add it as a `github_json` source — it's ~17k listings and by far the
  single biggest coverage upgrade available. Same for `cvrve` / `Ouckah` /
  `speedyapply` if they ship JSON feeds. (Probe:
  `https://raw.githubusercontent.com/<owner>/<repo>/dev/.github/scripts/listings.json`)
- **USAJOBS is configured but inert** until `USAJOBS_API_KEY` / `USAJOBS_EMAIL`
  secrets are set. Free key: <https://developer.usajobs.gov/apirequest/>
- **iCIMS fetcher** — several defense contractors (GD Mission Systems) use it; no
  clean public API, so they're `pagewatch` only right now.
- **Eightfold fetcher** — Netflix and others.
- **FAANG page-watchers are weak** (JS single-page apps; the static HTML doesn't
  change when a job posts). Set *native* job alerts at Google / Meta / Apple /
  Microsoft / Netflix as the real backup.
- LinkedIn / Indeed have **no public API** and scraping them gets IP-blocked from
  Actions runners. They mostly re-list ATS postings anyway, so polling the ATS
  directly is both earlier and cleaner. Don't go down this road.

## Testing

Filters are pure functions — test them without any network:

```bash
python3 -c "
import json, watcher
f = json.load(open('config.json'))['filters']
job = {'title': 'Software Engineer Intern', 'location': 'NYC', 'content': ''}
print(watcher.is_relevant(job, f))   # should be True
"
```

Full local run (sends a real email):

```bash
pip install -r requirements.txt
SMTP_USERNAME=... SMTP_PASSWORD=... EMAIL_TO=... python watcher.py
```

Delete `seen_jobs.json` locally to force a fresh baseline. **Don't commit that
deletion** unless you want a full catch-up email.
