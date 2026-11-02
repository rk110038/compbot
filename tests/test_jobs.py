from sqlalchemy import select

import pytest

from app import jobs
from app.config import DEFAULT_SETTINGS
from app.db import Competition, init_db, session_scope

U = ["https://a.example/win", "https://b.example/win", "https://c.example/win"]


def _comp(title: str, urls: list[str] = U) -> int:
    init_db()
    with session_scope() as s:
        c = Competition(source="test", source_id=f"test-{title}", thread_url="x", title=title, method="web_form",
                        status="queued", entry_urls=list(urls))
        s.add(c)
        s.flush()
        return c.id


def _get(cid):
    with session_scope() as s:
        return s.get(Competition, cid)


def test_partial_multi_site_with_captcha_goes_manual_but_keeps_progress():
    cid = _comp("md1")
    jobs._finalise_web(cid, {U[0]: "entered", U[1]: "captcha"}, DEFAULT_SETTINGS)
    c = _get(cid)
    assert c.status == "manual" and c.entered_urls == [U[0]] and c.entry_count == 1
    assert "1 of 3" in c.status_reason


def test_cap_stops_midway_stays_queued_and_resumes():
    cid = _comp("md2")
    jobs._finalise_web(cid, {U[0]: "entered"}, DEFAULT_SETTINGS)
    c = _get(cid)
    assert c.status == "queued" and "Partly entered" in c.status_reason
    assert jobs._urls_to_try(c) == U[1:]
    jobs._finalise_web(cid, {U[1]: "entered", U[2]: "entered"}, DEFAULT_SETTINGS)
    assert _get(cid).status == "entered"


def test_unverified_flags_review():
    cid = _comp("uv", [U[0]])
    jobs._finalise_web(cid, {U[0]: "unverified"}, DEFAULT_SETTINGS)
    c = _get(cid)
    assert c.status == "entered" and c.needs_review and "no confirmation" in c.status_reason


def test_repeated_failures_go_manual():
    cid = _comp("ff", [U[0]])
    jobs._finalise_web(cid, {U[0]: "failed"}, DEFAULT_SETTINGS)
    assert _get(cid).status == "queued"
    jobs._finalise_web(cid, {U[0]: "failed"}, DEFAULT_SETTINGS)
    assert _get(cid).status == "manual"


def test_dry_run_leaves_status_alone():
    cid = _comp("dr", [U[0]])
    jobs._finalise_web(cid, {U[0]: "dry_run_ok"}, DEFAULT_SETTINGS)
    c = _get(cid)
    assert c.status == "queued" and c.entry_count == 0 and "Dry run OK" in c.status_reason


def test_clear_dry_runs():
    from app.db import EntryAttempt, utcnow

    a = _comp("clear-a", [U[0]])   # only dry-run, OK
    b = _comp("clear-b", [U[0]])   # dry-run failures pushed it to manual
    c = _comp("clear-c", [U[0]])   # has a real entry as well
    with session_scope() as s:
        s.add_all([EntryAttempt(competition_id=a, url=U[0], mode="dry_run", result="dry_run_ok"),
                   EntryAttempt(competition_id=b, url=U[0], mode="dry_run", result="failed"),
                   EntryAttempt(competition_id=c, url=U[0], mode="dry_run", result="dry_run_ok"),
                   EntryAttempt(competition_id=c, url=U[0], mode="auto", result="entered")])
        for cid in (a, b, c):
            s.get(Competition, cid).last_attempt_at = utcnow()
        s.get(Competition, a).status_reason = "Dry run OK - will submit once entry mode is set to auto"
        cb = s.get(Competition, b)
        cb.status, cb.failed_attempts, cb.status_reason = "manual", 2, "Automatic entry failed 2 times"
    msg = jobs.clear_dry_runs(requeue_manual=True)
    assert "Cleared 3 dry-run attempts" in msg
    ca, cb, cc = _get(a), _get(b), _get(c)
    assert ca.status == "queued" and ca.last_attempt_at is None and ca.status_reason == "Queued"
    assert cb.status == "queued" and cb.failed_attempts == 0
    assert cc.last_attempt_at is not None  # real attempt kept
    with session_scope() as s:
        modes = {m for (m,) in s.execute(select(EntryAttempt.mode)).all()}
    assert "dry_run" not in modes and "auto" in modes
    assert jobs.clear_dry_runs() == "No dry-run history to clear"


# --- manual queue: reasons and selective retry ----------------------------------------------------------------------

def _manual(title, urls, method="web_form", reason="", attempt=None):
    from app.db import EntryAttempt
    init_db()
    with session_scope() as s:
        c = Competition(source="test", source_id=f"man-{title}", thread_url="x", title=title, method=method,
                        status="manual", entry_urls=list(urls), status_reason=reason)
        s.add(c)
        s.flush()
        if attempt:
            s.add(EntryAttempt(competition_id=c.id, url=urls[0] if urls else "", mode="auto", result=attempt[0],
                               message=attempt[1]))
        return c.id


def test_manual_reasons_and_selective_retry():
    ids = {
        "captcha": _manual("m-captcha", [U[0]], attempt=("captcha", "CAPTCHA on the form (reCAPTCHA checkbox)")),
        "phone": _manual("m-phone", [U[0]], attempt=("needs_input", "Couldn't fill: Phone * (profile has no phone)")),
        "question": _manual("m-q", [U[0]], attempt=("needs_input", "Couldn't fill: Which team? (AI wasn't sure enough)")),
        "noanswer": _manual("m-noans", [U[0]], reason="Needs an answer that isn't in the post yet"),
        "failed": _manual("m-failed", [U[0]], attempt=("failed", "No entry form found on the page")),
        "blocked": _manual("m-blocked", ["https://comps.mag.co.uk/competition/x/123.php"],
                           reason="This magazine competition site blocks automated entry - enter it yourself"),
        "tc": _manual("m-tc", [U[0]], attempt=("tc_blocked", "T&Cs ban automated entries")),
        "login": _manual("m-login", ["https://comp.smoothradio.com/x/1"], method="account"),
        "social": _manual("m-social", [], method="social"),
    }
    with session_scope() as s:
        groups, by_id = jobs.manual_breakdown(s)
    assert by_id[ids["captcha"]] == "captcha" and by_id[ids["phone"]] == "phone"
    assert by_id[ids["question"]] == "question" and by_id[ids["noanswer"]] == "question"
    assert by_id[ids["failed"]] == "failed" and by_id[ids["blocked"]] == "blocked" and by_id[ids["tc"]] == "tc"
    assert by_id[ids["login"]] == "login" and by_id[ids["social"]] == "social"

    msg = jobs.requeue_categories(["captcha", "blocked", "login"])  # login can't be retried
    assert "Re-queued" in msg
    assert _get(ids["captcha"]).status == "queued" and not _get(ids["captcha"]).force_next
    assert _get(ids["blocked"]).status == "queued" and _get(ids["blocked"]).force_next
    assert _get(ids["login"]).status == "manual" and _get(ids["failed"]).status == "manual"


def test_forced_retry_tries_blocked_site_once(monkeypatch):
    from app.db import get_settings, save_profile, save_settings
    cid = _manual("m-force", ["https://comps.mag2.co.uk/competition/y/456.php"],
                  reason="This magazine competition site blocks automated entry - enter it yourself")
    with session_scope() as s:
        save_profile(s, {"first_name": "Test", "last_name": "Person", "email": "t@example.com"})
        st = get_settings(s)
        st.update(entry_mode="auto", delay_min_seconds=0, delay_max_seconds=0)
        save_settings(s, st)
    tried = []
    monkeypatch.setattr(jobs, "_enter_web", lambda cid_, url, settings, profile: tried.append(url) or "blocked")
    jobs.requeue_categories(["blocked"])
    jobs._enter([cid])
    assert tried == ["https://comps.mag2.co.uk/competition/y/456.php"]
    assert not _get(cid).force_next
    tried.clear()
    jobs._enter([cid])  # without the override the platform is skipped again
    assert tried == [] and _get(cid).status == "manual"


def test_site_that_ever_loaded_is_not_treated_as_blocking():
    # Rayo: two false "bot-check" verdicts got the whole site skipped, though its pages had loaded fine (the entries
    # then stopped on a T&C rule or a question). Only sites that never let compbot's browser in are skipped.
    from app.db import EntryAttempt
    init_db()
    with session_scope() as s:
        c = Competition(source="test", source_id="bd-1", thread_url="x", title="bd", method="web_form", status="queued",
                        entry_urls=["https://www.loadsonce.co.uk/a"])
        s.add(c)
        s.flush()
        for url, result, mode in [("https://www.loadsonce.co.uk/a", "blocked", "auto"),
                                  ("https://www.loadsonce.co.uk/b", "blocked", "auto"),
                                  ("https://www.loadsonce.co.uk/c", "needs_input", "auto"),
                                  ("https://www.loadsonce2.co.uk/a", "blocked", "auto"),
                                  ("https://www.loadsonce2.co.uk/b", "tc_blocked", "auto"),
                                  ("https://www.loadsonce2.co.uk/c", "blocked", "auto"),
                                  ("https://www.alwaysblocks.co.uk/a", "blocked", "auto"),
                                  ("https://www.alwaysblocks.co.uk/b", "blocked", "auto"),
                                  ("https://www.alwaysblocks.co.uk/c", "entered", "manual")]:  # you entered it yourself
            s.add(EntryAttempt(competition_id=c.id, url=url, mode=mode, result=result))
    with session_scope() as s:
        b = jobs.blocked_domains(s)
    assert "alwaysblocks.co.uk" in b
    assert "loadsonce.co.uk" not in b and "loadsonce2.co.uk" not in b


@pytest.fixture
def entry_run(monkeypatch):
    """Runs _enter against scripted per-attempt results, with no delays and nothing entered earlier today."""
    from app.db import get_settings, save_profile, save_settings
    init_db()
    with session_scope() as s:
        saved = dict(get_settings(s))
    tried: list[str] = []

    def setup(cap: int, results: list[str]) -> list[str]:
        with session_scope() as s:
            save_profile(s, {"first_name": "Test", "last_name": "Person", "email": "t@example.com"})
            save_settings(s, {**saved, "entry_mode": "auto", "daily_entry_cap": cap,
                              "delay_min_seconds": 0, "delay_max_seconds": 0})
        script = iter(results)
        monkeypatch.setattr(jobs, "entries_today", lambda *a, **k: 0)
        monkeypatch.setattr(jobs.time, "sleep", lambda *_: None)
        monkeypatch.setattr(jobs, "_enter_web", lambda cid, url, settings, profile: tried.append(url) or next(script))
        return tried

    yield setup
    with session_scope() as s:
        save_settings(s, saved)


def test_daily_cap_counts_entries_not_attempts(entry_run):
    # 2 Nov: with 17 entries made, a run stopped after 83 attempts (3 of them entries) with 324 comps still queued,
    # because blocked, failed and CAPTCHA attempts were using up the 100-entry daily cap.
    ids = [_comp(f"cap{i}", [f"https://cap{i}.example/win"]) for i in range(6)]
    tried = entry_run(cap=2, results=["blocked", "failed", "entered", "captcha", "unverified", "entered"])
    summary = jobs._enter(ids)
    assert len(tried) == 5
    assert summary.endswith(" - stopped at the daily cap of 2 entries, 1 left for the next run")
    assert _get(ids[5]).status == "queued" and _get(ids[5]).last_attempt_at is None


def test_run_stops_after_errors_in_a_row_but_not_scattered_errors(entry_run):
    run = jobs.MAX_ERRORS_IN_A_ROW
    ids = [_comp(f"err{i}", [f"https://err{i}.example/win"]) for i in range(2 * run + 2)]
    tried = entry_run(cap=100, results=["error"] * (run - 1) + ["failed"] + ["error"] * (run + 5))
    summary = jobs._enter(ids)
    assert len(tried) == 2 * run  # the "failed" reset the count, so it stopped at the second streak
    assert summary.endswith(f"stopped after {run} errors in a row (is the network or browser OK?), "
                            "2 left for the next run")
    assert _get(ids[-1]).last_attempt_at is None


def test_retried_blocked_comps_stop_once_the_platform_refuses_twice(entry_run):
    # 2 Nov: "Retry blocked" re-queued ~30 magazine-platform comps (comps.<magazine>/competition/<slug>/<n>.php) and
    # every link was tried, all refused (HTTP 403). Two refusals are enough to know; the rest go back to you.
    def mag(site, n):
        return f"https://competitions.{site}/competition/win_tickets_{site.split('.')[0]}/{n}.php"
    multi = _comp("mag-multi", [mag("womansweekly.com", 1), mag("marieclaire.co.uk", 2), mag("idealhome.co.uk", 3)])
    single = [_comp(f"mag-{i}", [mag(f"site{i}.co.uk", 10 + i)]) for i in range(3)]
    other = _comp("other-site", ["https://www.unrelated-shop.example/win"])
    ids = [multi, *single, other]
    with session_scope() as s:
        for cid in ids[:-1]:
            s.get(Competition, cid).force_next = True  # what the manual queue's retry does
    tried = entry_run(cap=10, results=["blocked", "blocked", "entered"])
    summary = jobs._enter(ids)
    assert tried == [mag("womansweekly.com", 1), mag("marieclaire.co.uk", 2), "https://www.unrelated-shop.example/win"]
    assert "3 skipped_blocked_site" in summary
    for cid in ids[:-1]:
        c = _get(cid)
        assert c.status == "manual" and not c.force_next
    assert _get(other).status == "entered"
