"""Network smoke test: builds the real digest in DRY_RUN and checks its shape.
Skipped unless SMOKE=1 (CI sets it). Never sends mail, never writes state."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.skipif(os.environ.get("SMOKE") != "1", reason="set SMOKE=1 for the network smoke test")


def test_digest_builds_and_fits_gmail(tmp_path, monkeypatch):
    os.environ["DRY_RUN"] = "1"
    import importlib
    import watcher as w
    importlib.reload(w)
    monkeypatch.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    monkeypatch.setattr(w, "PREVIEW_FILE", str(tmp_path / "preview.html"))
    monkeypatch.setattr(w, "ICS_FILE", str(tmp_path / "opps.ics"))
    saved = {}
    monkeypatch.setattr(w, "save_json", lambda p, d: saved.setdefault("called", True))
    w.send_weekly_digest()
    assert "called" not in saved, "DRY_RUN must not persist state"
    html = open(tmp_path / "preview.html", encoding="utf-8").read()
    assert len(html.encode()) < 102_000, "Gmail clips > 102KB"
    for must in ["Opportunities digest", "Everything currently open", "MLH hackathons", "Hackathons &mdash;"]:
        assert must in html, must
    assert "Feed failure this run" not in html, re.search(r"Feed failure this run.*?</div>", html, re.S).group(0)[:400]
    ics = open(tmp_path / "opps.ics", encoding="utf-8").read()
    assert ics.startswith("BEGIN:VCALENDAR") and ics.count("BEGIN:VEVENT") > 10
