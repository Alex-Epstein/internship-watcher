"""Pure-function regression tests. No network. Run: python -m pytest -q"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watcher as w  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
FILTERS = CFG["filters"]


# ---------------- config / data integrity ----------------
def test_config_validates():
    probs = w.validate_config(CFG)
    assert probs == [], "\n".join(probs)


def test_opportunities_validate():
    opps = json.load(open(os.path.join(ROOT, "opportunities.json")))["opportunities"]
    probs = w.validate_opportunities(opps)
    assert probs == [], "\n".join(probs)


def test_every_firm_routes_to_a_fetcher():
    for f in CFG["firms"]:
        assert f.get("ats") in w.FETCHERS, f["name"]


# ---------------- hourly sweep gates ----------------
def test_us_location_gate():
    keep = ["New York, NY", "Radnor, PA", "", "Albuquerque, New Mexico", "Indianapolis, Indiana", "London / New York", "Remote - US"]
    drop = ["Europe", "Munich", "Singapore", "Mexico, Monterrey", "Toronto", "CAD Remote – ON", "Suzhou - Jiangsu, China"]
    assert all(w._is_us_location(x) for x in keep)
    assert not any(w._is_us_location(x) for x in drop)


def test_fall_2027_gate():
    yes = [("Fall 2027 Software Engineer Intern", ""), ("Fall Analyst Program", ""), ("Software Engineer Intern", "We hire each fall 2027 cohort")]
    no = [("Fall 2026 Software Engineer Intern", ""), ("Summer 2027 Software Engineering Intern", ""), ("Waterfall Data Engineer Intern", ""), ("Software Engineer Intern", "prices fall when vol spikes in 2027")]
    assert all(w._is_fall_2027({"title": t, "content": c}) for t, c in yes)
    assert not any(w._is_fall_2027({"title": t, "content": c}) for t, c in no)


def test_top_firm_gate():
    assert all(w._is_top_firm(c) for c in ["Citadel", "Jane Street", "Google DeepMind", "OpenAI", "Walleye Capital", "PDT Partners"])
    assert not any(w._is_top_firm(c) for c in ["eulerity", "acds", "harman", "Stevens Capital Management"])


# ---------------- program-posting detector ----------------
def test_program_posting_detector():
    assert w._is_program_posting("LINK 2027: Software Development Intensive Program")
    assert w._is_program_posting("FutureFocus: Quants 2027")
    assert w._is_program_posting("Something", "Employment Type: Intern | Early Engagement")
    for junk in ["Summer Intern 2027 - Software Developer", "Junior Software Developer (C++)", "Programs Lead, ISV",
                 "Software Engineer, Insights", "Program Coordinator, Investment", "Chief Engineers, Advanced Aircraft Programs",
                 "Dean of Students, Academy of Math and Programming (AMP)", "Career Kickstarter: Women in Tech 2027"]:
        assert not w._is_program_posting(junk), junk


# ---------------- pagewatch keying ----------------
def test_pw_key_roundtrip_with_hash_fragment_urls():
    u = "https://x.com/jobs?req=1#job"
    assert w._pw_url(w._pw_key(u, "abc")) == u
    assert w._pw_key(u, "abc").startswith(w.PW_PREFIX)


# ---------------- whereabouts calendar ----------------
def test_region_classifier():
    cases = {"Madrid, Spain": "europe", "St. Gallen, Switzerland": "europe", "Turku, Varsinais-Suomi": "europe",
             "Secaucus NJ": "us", "Miami, FL": "us", "Heafey Hall": "us", "Toronto": "us",
             "New delhi, New delhi": "other", "Online": "online", "": "online"}
    for loc, exp in cases.items():
        assert w._loc_region(loc) == exp, loc
    assert w._loc_region("anything", mlh_region="EMEA") == "europe"
    assert w._loc_region("anything", mlh_region="APAC") == "other"


def test_alex_calendar():
    f = {"whereabouts": [{"from": "2027-01-20", "to": "2027-05-20", "region": "europe", "label": "Madrid"},
                         {"from": "2027-06-07", "to": "2027-08-14", "region": "us", "label": "NYC"}], "default_region": "us"}
    assert w._alex_region(f, dt.date(2027, 3, 1))[0] == "europe"
    assert w._alex_region(f, dt.date(2027, 7, 1))[0] == "us"
    assert w._alex_region(f, dt.date(2026, 11, 1))[0] == "us"
    assert w._event_visible(f, "europe", "2027-02-12")[0] is True
    assert w._event_visible(f, "us", "2027-03-12")[0] is False
    assert w._event_visible(f, "europe", "2026-11-06")[0] is False
    assert w._event_visible(f, "online", "2027-03-12")[0] is True
    assert w._event_visible(f, "other", "2027-03-12")[0] is False
    assert w._event_visible(f, "us", "garbage")[0] is True  # unknown date -> assume now (US)


# ---------------- devpost helpers ----------------
def test_devpost_prize_and_date():
    h = {"prize_amount": "$<span data-currency-value>12,500</span>", "submission_period_dates": "Jan 25 - Feb 11, 2027"}
    assert w._dp_prize(h) == ("$", 12500)
    assert w._dp_start_iso({"dates": h["submission_period_dates"]}) == "2027-01-25"
    assert w._dp_prize({"prize_amount": "₹ <span data-currency-value>55,000</span>"})[0] == "₹"


# ---------------- dedup ----------------
def test_norm_name_dedups_editions():
    assert w._norm_name("HackRPI 2026: In the Clouds") == w._norm_name("HackRPI")
    assert w._norm_name("Sheridan Datathon 2026") == w._norm_name("Sheridan Datathon")
    assert w._norm_name("HackMIT") != w._norm_name("HackGT")


# ---------------- rss parsing on a fixture (requests stubbed) ----------------
def test_fetch_rss_parses_rss_and_atom(monkeypatch):
    rss = b"""<?xml version="1.0"?><rss><channel><item><title>Big Hackathon with prizes</title>
      <link>https://ex.com/a</link><description>&lt;b&gt;students&lt;/b&gt; welcome</description></item>
      <item><title>Unrelated news</title><link>https://ex.com/b</link><description>x</description></item></channel></rss>"""
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Datathon travel grant</title>
      <link href="https://ex.com/c"/><summary>cash prize</summary></entry></feed>"""
    class R:
        def __init__(self, c): self.content = c; self.text = c.decode()
        def raise_for_status(self): pass
    for payload, expect_url in ((rss, "https://ex.com/a"), (atom, "https://ex.com/c")):
        monkeypatch.setattr(w.requests, "get", lambda *a, **k: R(payload))
        out = w.fetch_rss({"name": "RSS: t", "url": "https://ex.com/feed", "watch_keywords": ["hackathon", "datathon"]})
        assert [o["url"] for o in out] == [expect_url]
        assert out[0]["rss"] and out[0]["bypass_filters"]


# ---------------- digest source routing ----------------
def test_digest_source_routing():
    assert w._is_digest_source({"name": "Hackathon: X", "ats": "pagewatch"})
    assert w._is_digest_source({"name": "Quant SPA: Y", "ats": "pagewatch", "digest": True})
    assert not w._is_digest_source({"name": "Quant SPA: Y", "ats": "pagewatch"})
    assert not w._is_digest_source({"name": "Hackathon: X", "ats": "pagewatch", "digest": False})
