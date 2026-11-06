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
        "wronglink": _manual("m-wrong", ["https://www.futureplc.com/competition-rules/"],
                             attempt=("wrongpage", "The link goes to the competition's rules, not the entry form")),
    }
    with session_scope() as s:
        groups, by_id = jobs.manual_breakdown(s)
    assert by_id[ids["captcha"]] == "captcha" and by_id[ids["phone"]] == "phone"
    assert by_id[ids["question"]] == "question" and by_id[ids["noanswer"]] == "question"
    assert by_id[ids["failed"]] == "failed" and by_id[ids["blocked"]] == "blocked" and by_id[ids["tc"]] == "tc"
    assert by_id[ids["login"]] == "login" and by_id[ids["social"]] == "social"
    assert by_id[ids["wronglink"]] == "wronglink" and not jobs.RETRY_CATEGORIES["wronglink"]["retry"]

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

def test_manual_queue_clears_closed_and_long_undated_comps():
    from datetime import timedelta

    from app.db import utcnow
    init_db()
    today, old = jobs.today_local(), utcnow() - timedelta(days=jobs.UNDATED_MANUAL_DAYS + 1)

    def add(key, title, status="manual", closes_on=None, found=None):
        with session_scope() as s:
            c = Competition(source="test", source_id=f"exp-{key}", thread_url="x", title=title, method="web_form",
                            status=status, closes_on=closes_on, discovered_at=found or utcnow(), posted_at=found)
            s.add(c)
            s.flush()
            return c.id

    closed = add("closed", "E: 01/01 Win a kettle", closes_on=today - timedelta(days=1))
    open_ = add("open", "E: 01/01 Win a toaster", closes_on=today + timedelta(days=3))
    undated_old = add("undated-old", "Win a £20 Amazon Voucher (Gleam)", found=old)
    undated_new = add("undated-new", "Win a copy of a journal (Gleam)")
    queued_old = add("queued-old", "Win a lamp (IG)", status="queued", found=old)
    redated = add("redated", f"Ends {(today - timedelta(days=2)):%d/%m/%Y} Win baby keepsakes")  # unreadable before
    with session_scope() as s:
        assert jobs._expire(s) >= 3
    assert _get(closed).status == "expired" and _get(open_).status == "manual"
    assert _get(undated_old).status == "expired" and "No closing date" in _get(undated_old).status_reason
    assert _get(undated_new).status == "manual" and _get(queued_old).status == "queued"
    assert _get(redated).closes_on == today - timedelta(days=2) and _get(redated).status == "expired"


def test_stop_finishes_the_current_competition_then_stops(entry_run, monkeypatch):
    from app.db import JobRun
    multi = _comp("stop-multi", ["https://s1.example/win", "https://s2.example/win"])
    rest = [_comp(f"stop-{i}", [f"https://stop{i}.example/win"]) for i in range(2)]
    tried = entry_run(cap=10, results=[])

    def enter_web(cid, url, settings, profile):
        tried.append(url)
        jobs.request_stop("enter")  # pressed while the first site's entry is in progress
        return "entered"

    monkeypatch.setattr(jobs, "_enter_web", enter_web)
    summary = jobs._run("enter", jobs._enter, [multi, *rest])
    assert tried == ["https://s1.example/win"]
    assert summary == "1 entered - still queued: 1 partly entered (the other sites next run) - stopped by you, " \
                      "2 left for the next run"
    c = _get(multi)
    assert c.status == "queued" and c.entered_urls == ["https://s1.example/win"] and "Partly entered" in c.status_reason
    assert all(_get(cid).last_attempt_at is None for cid in rest)
    with session_scope() as s:
        jr = s.scalars(select(JobRun).where(JobRun.job == "enter").order_by(JobRun.id.desc())).first()
        assert jr.status == "stopped" and jr.summary == summary
    assert not jobs.is_running("enter") and not jobs._stops["enter"].is_set()


def test_stop_cuts_the_wait_short_and_needs_a_running_job():
    import time
    assert jobs.request_stop("enter") is False  # not running: nothing to stop, and no stale flag left behind
    assert not jobs._stops["enter"].is_set()
    jobs._stops["enter"].set()
    try:
        t0 = time.monotonic()
        jobs._wait("enter", 30)
        assert time.monotonic() - t0 < 1
    finally:
        jobs._stops["enter"].clear()


def test_stopped_discovery_picks_up_the_rest_next_run(monkeypatch):
    from datetime import datetime

    from app.db import get_state
    init_db()
    threads = [{"source_id": f"stopdisc-{i}", "title": f"E: 30/12 Win a stop test prize {i}", "url": f"https://f/{i}",
                "body": f"<p>https://www.stop-test-{i}.example/win</p>", "posted_at": datetime(2026, 11, 2),
                "comments": []} for i in range(3)]
    monkeypatch.setattr(jobs.mse, "fetch_threads", lambda known, since: [t for t in threads if t["source_id"] not in known])
    monkeypatch.setattr(jobs.ct, "fetch_listings", lambda known, should_stop=None: [])
    with session_scope() as s:
        before = get_state(s, "mse_last_run")
    real = jobs.analyse

    def analyse_then_stop(*a, **k):
        jobs.request_stop("discover")
        return real(*a, **k)

    monkeypatch.setattr(jobs, "analyse", analyse_then_stop)
    summary = jobs._run("discover", jobs._discover)
    assert "stopped by you after 1 of 3" in summary
    with session_scope() as s:
        assert get_state(s, "mse_last_run") == before
    monkeypatch.setattr(jobs, "analyse", real)
    assert jobs._run("discover", jobs._discover).startswith("MSE: 2 new threads")


def test_queue_only_holds_comps_an_entry_run_can_enter():
    # 3 Nov: a run finished with 1 still "queued". Failed-once comps rightly wait for one more try, but nothing may sit
    # in the queue that no run will ever pick up.
    from datetime import timedelta

    from app.config import DEFAULT_SETTINGS
    init_db()

    def add(key, method="web_form", urls=None, entered=None, daily=False, failed=0, closes=None):
        with session_scope() as s:
            c = Competition(source="test", source_id=f"park-{key}", thread_url="x", title=f"Park {key}", method=method,
                            status="queued", entry_urls=urls if urls is not None else [f"https://{key}.example/win"],
                            entered_urls=entered or [], is_daily=daily, failed_attempts=failed, email_to="a@b.example",
                            closes_on=closes)
            s.add(c)
            s.flush()
            return c.id

    social, nolink = add("social", method="social", urls=[]), add("nolink", urls=[])
    email = add("email", method="email", urls=[])
    done = add("done", urls=["https://d1.example/w", "https://d2.example/w"],
               entered=["https://d1.example/w", "https://d2.example/w"])
    daily = add("daily", urls=["https://dy.example/w"], entered=["https://dy.example/w"], daily=True)
    retry, fresh = add("retry", failed=1), add("fresh")
    today = jobs.today_local()
    closed, closes_today = add("closed", closes=today - timedelta(days=1)), add("closes-today", closes=today)
    with session_scope() as s:
        jobs._due_competitions(s, dict(DEFAULT_SETTINGS, enter_email_comps=False), None)
    assert _get(social).status == "manual" and "can't enter social" in _get(social).status_reason
    assert _get(nolink).status == "manual" and "No entry link" in _get(nolink).status_reason
    assert _get(email).status == "manual" and "switched off" in _get(email).status_reason
    assert _get(done).status == "entered"
    assert _get(closed).status == "expired"  # closed yesterday: gone now, not after the next discovery run
    for cid in (daily, retry, fresh, closes_today):
        assert _get(cid).status == "queued"


def test_run_summary_says_what_is_left_to_retry(entry_run):
    # 3 Nov: "It says successfully entered but leaves a queue of 1" - the summary now says why one is still queued.
    ids = [_comp(f"left{i}", [f"https://left{i}.example/win"]) for i in range(3)]
    entry_run(cap=100, results=["entered", "failed", "entered"])
    summary = jobs._enter(ids)
    assert summary == "2 entered, 1 failed - still queued: 1 to retry after a failed try"
    assert _get(ids[1]).status == "queued" and _get(ids[1]).status_reason == "Attempt 1 failed - will retry"


def _discover_with(monkeypatch, threads, ct_listings):
    monkeypatch.setattr(jobs.mse, "fetch_threads", lambda known, since: [t for t in threads if t["source_id"] not in known])
    monkeypatch.setattr(jobs.ct, "fetch_listings",
                        lambda known, should_stop=None: [x for x in ct_listings if x["source_id"] not in known])
    monkeypatch.setattr(jobs.cdb, "fetch_listings", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no CDB")))
    return jobs._run("discover", jobs._discover)


def _ct(cid, title, closes, domain=None, answers=None):
    return {"source": "ct", "source_id": f"ct:{cid}", "url": f"https://www.competitions-time.co.uk/x#{cid}",
            "title": title, "company": "", "closes_on": closes, "closes_precise": True, "posted_at": None,
            "entry_urls": [], "promoter_domain": domain, "description": "", "question": None,
            "answers": answers or [], "tags": [], "notes": [],
            "manual_link": f"https://www.competitions-time.co.uk/redir/{cid}"}


def test_discovery_keeps_one_competition_per_comp_across_sites(monkeypatch):
    # 3 Nov: ~90% of Competitions-Time's comps were already on MSE, and MSE had comps posted twice
    from datetime import datetime, timedelta

    from app.db import Listing
    init_db()
    closes = jobs.today_local() + timedelta(days=40)
    e = f"E: {closes:%d/%m}"
    threads = [  # newest first, as MSE returns them
        {"source_id": "merge-2", "title": f"{e} Win the Merge Test Hamper!", "url": "https://mse/2",
         "body": "<p>https://www.merge-test.example/win-hamper/?utm_source=mse</p>",
         "posted_at": datetime(2026, 11, 3), "comments": 0},
        {"source_id": "merge-1", "title": f"{e} Win a Merge Test hamper", "url": "https://mse/1",
         "body": "<p>Q: What colour is the box?</p><p>https://merge-test.example/win-hamper</p>",
         "posted_at": datetime(2026, 11, 2), "comments": 0},
    ]
    listings = [_ct("m1", "Merge Test Hamper", closes, "merge-test.example", ["Blue"]),
                _ct("m2", "Unique Merge Widget Bundle", closes)]
    summary = _discover_with(monkeypatch, threads, listings)
    assert summary.startswith("MSE: 2 new threads: 1 already listed (merged)")
    assert "Competitions-Time: 2 new listings: 1 already listed (merged), 1 manual" in summary
    with session_scope() as s:
        c = s.scalars(select(Competition).where(Competition.source_id == "merge-1")).one()
        assert sorted(x.source_id for x in c.listings) == ["ct:m1", "merge-1", "merge-2"]
        assert c.answers == ["Blue"] and c.status == "queued" and c.status_reason == "Answer from Competitions-Time"
        assert s.scalars(select(Competition).where(Competition.source_id == "merge-2")).first() is None
        w = s.scalars(select(Competition).where(Competition.source_id == "ct:m2")).one()
        assert w.status == "manual" and w.status_reason.startswith("No entry link: Competitions-Time")
        assert w.other_links == ["https://www.competitions-time.co.uk/redir/m2"]
        assert jobs.manual_category(w, None) == "nolink"
        assert s.scalars(select(Listing).where(Listing.source_id == "ct:m2")).one().competition_id == w.id
    # nothing new second time round
    assert "Competitions-Time: nothing new" in _discover_with(monkeypatch, threads, listings)


def test_discovery_looks_up_entry_pages_competitions_time_hides(monkeypatch):
    from datetime import timedelta

    from app import websearch
    init_db()
    closes = jobs.today_local() + timedelta(days=30)
    results = [{"url": "https://widgets-ws.example/", "title": "Widgets home", "content": ""},
               {"url": "https://widgets-ws.example/terms", "title": "Widget Bundle terms", "content": ""},
               {"url": "https://widgets-ws.example/competitions/win-a-rare-widget-bundle",
                "title": "Win a Rare Widget Bundle", "content": "Enter our rare widget bundle competition"}]
    searched = []
    monkeypatch.setattr(websearch, "providers", lambda: ["searxng"])
    monkeypatch.setattr(websearch, "search", lambda q, n=6, use_cache=True: searched.append(q) or results)
    _discover_with(monkeypatch, [], [_ct("ws1", "Rare Widget Bundle", closes, "widgets-ws.example")])
    assert searched == ["Rare Widget Bundle competition site:widgets-ws.example"]
    with session_scope() as s:
        c = s.scalars(select(Competition).where(Competition.source_id == "ct:ws1")).one()
        assert c.entry_urls == ["https://widgets-ws.example/competitions/win-a-rare-widget-bundle"]
        assert c.method == "web_form" and c.status == "queued" and "found by web search" in c.status_reason


def test_entry_run_never_enters_the_same_page_twice(entry_run):
    # the same comp found twice (and not merged) is entered once; hub pages shared by many comps are still entered
    from app.db import EntryAttempt
    a = _comp("twin-a", ["https://twin.example/win-x?utm_source=a"])
    b = _comp("twin-b", ["https://www.twin.example/win-x/"])
    hub_done, hub = _comp("hub-a", ["https://hub-twin.example/competitions"]), _comp("hub-b", ["https://hub-twin.example/competitions"])
    with session_scope() as s:
        for cid, url in ((a, "https://twin.example/win-x?utm_source=a"), (hub_done, "https://hub-twin.example/competitions")):
            s.add(EntryAttempt(competition_id=cid, url=url, mode="auto", result="entered"))
            s.get(Competition, cid).status = "entered"
    tried = entry_run(cap=10, results=["entered"])
    summary = jobs._enter([b, hub])
    assert tried == ["https://hub-twin.example/competitions"]
    assert "1 already entered" in summary
    c = _get(b)
    assert c.status == "entered" and c.status_reason == f"Already entered as #{a} (the same competition, found twice)"
    assert c.entry_count == 0

def test_entry_run_enters_each_round_of_a_comp_that_reuses_its_page(entry_run):
    from datetime import timedelta

    from app.db import EntryAttempt
    page = "https://rounds.example/win-this-week"
    old, new = _comp("round-1", [page]), _comp("round-2", [page])
    with session_scope() as s:
        s.get(Competition, old).closes_on = jobs.today_local() - timedelta(days=7)
        s.get(Competition, old).status = "entered"
        s.get(Competition, new).closes_on = jobs.today_local() + timedelta(days=7)
        s.add(EntryAttempt(competition_id=old, url=page, mode="auto", result="entered"))
    tried = entry_run(cap=10, results=["entered"])
    jobs._enter([new])
    assert tried == [page] and _get(new).status == "entered" and _get(new).entry_count == 1


def test_email_entries_never_open_listing_sites_links(monkeypatch):
    from app.config import DEFAULT_SETTINGS
    init_db()
    with session_scope() as s:
        c = Competition(source="ct", source_id="ct:email-links", thread_url="x", title="E: 30/12 Win tea", method="email",
                        status="queued", email_to="tea@promo.example",
                        other_links=["https://www.competitions-time.co.uk/redir/5", "https://promo.example/tea-terms"])
        s.add(c)
        s.flush()
        cid = c.id
    seen = []
    monkeypatch.setattr(jobs, "_email_clause", lambda text, links: seen.extend(links) or None)
    jobs._enter_email(cid, dict(DEFAULT_SETTINGS, entry_mode="dry_run"), {"first_name": "A", "last_name": "B"})
    assert seen == ["https://promo.example/tea-terms"]


def test_competitions_time_email_comps_stay_email_comps(monkeypatch):
    from datetime import timedelta

    from app import websearch
    init_db()
    monkeypatch.setattr(websearch, "providers", lambda: ["searxng"])
    monkeypatch.setattr(websearch, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched")))
    listing = dict(_ct("em1", "Afternoon Tea For Two", jobs.today_local() + timedelta(days=20), "teacomp-promo.example"),
                   description="To enter, email your name and address to teacomp@teacomp-promo.example")
    _discover_with(monkeypatch, [], [listing])
    with session_scope() as s:
        c = s.scalars(select(Competition).where(Competition.source_id == "ct:em1")).one()
        assert c.method == "email" and c.email_to == "teacomp@teacomp-promo.example" and not c.entry_urls

def _gmail(monkeypatch, **extra):
    from app.config import get_config

    cfg = get_config()
    for k, v in {"gmail_address": "me@gmail.com", "gmail_app_password": "pw", "notify_email": None, "ntfy_url": None,
                 **extra}.items():
        monkeypatch.setattr(cfg, k, v)


def test_win_alert_says_what_the_email_was_and_alerts_are_never_read_back(monkeypatch):
    from email.message import EmailMessage

    from app import inbox
    from app.db import Email, set_state, utcnow
    from app.entry import emailer

    _gmail(monkeypatch, notify_email="me@gmail.com")  # alerts go to the competition Gmail itself
    init_db()
    with session_scope() as s:
        c = Competition(source="test", source_id="test-hotbin-alert", thread_url="x", title="Win a HOTBIN bundle",
                        prize="HOTBIN composting bundle worth £250", method="web_form", status="entered",
                        entry_count=1, entry_urls=["https://www.hotbin-test.example/win"], entered_at=utcnow(),
                        last_entered_at=utcnow())
        s.add(c)
        s.flush()
        cid = c.id
        set_state(s, "gui_url", "http://nas.local:8085")

    def mail(frm, subject, body, mid):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, "me@gmail.com", subject, mid
        m["Date"] = "Thu, 05 Nov 2026 19:58:00 +0000"
        m.set_content(body)
        return inbox.parse_message(m.as_bytes(), "INBOX", "me@gmail.com")

    win = mail("HOTBIN <winners@hotbin-test.example>", "Congratulations - you've won!",
               "Dear Test, we are delighted to tell you that you are the winner of our HOTBIN composting bundle "
               "competition. Please reply with your address.", "<win-1@hotbin-test.example>")
    # An alert from before they were tagged, which used to come back in as a 'scam' or another 'possible win'.
    old_alert = mail("me@gmail.com", "[compbot] Possible competition win!",
                     "From: winners@hotbin-test.example\nSubject: Congratulations - you've won!\n\nOpen compbot to "
                     "review. Never pay a fee or give bank details to claim a prize.", "<alert-0@gmail.com>")
    monkeypatch.setattr(inbox, "fetch_messages", lambda *a, **k: ([win, old_alert], False))
    sent = []
    monkeypatch.setattr(emailer, "send_mail",
                        lambda to, subject, body, headers=None: sent.append((to, subject, body, headers)) or "<id>")

    jobs._inbox()
    with session_scope() as s:
        rows = s.scalars(select(Email).where(Email.message_id.in_(["<win-1@hotbin-test.example>",
                                                                   "<alert-0@gmail.com>"]))).all()
        assert [r.message_id for r in rows] == ["<win-1@hotbin-test.example>"]
        assert s.get(Competition, cid).outcome == "possible_win"
        eid = rows[0].id
    assert len(sent) == 1
    to, subject, body, headers = sent[0]
    assert to == "me@gmail.com" and subject == "[compbot] Possible win: Congratulations - you've won!"
    assert headers == {"X-Compbot": "alert"}  # so it's never read back in
    assert "From:      HOTBIN <winners@hotbin-test.example>" in body
    assert ("\u201cDear Test, we are delighted to tell you that you are the winner of our HOTBIN composting bundle "
            "competition.\u201d") in body
    assert "Looks like your entry for: HOTBIN composting bundle worth £250 (entered " in body
    assert "because it's from the competition's site (hotbin-test.example) and both mention \u201chotbin\u201d" in body
    assert ("https://mail.google.com/mail/?authuser=me%40gmail.com#search/rfc822msgid%3Awin-1%40hotbin-test.example"
            in body)
    assert f"Review it in compbot: http://nas.local:8085/inbox/{eid}" in body


def test_alerts_read_back_in_before_are_removed(monkeypatch):
    from app.db import Email

    _gmail(monkeypatch)
    init_db()
    note = "Email: [compbot] Possible competition win!"
    with session_scope() as s:
        only = Competition(source="test", source_id="test-alert-only", thread_url="x", title="Win a kettle",
                           method="web_form", status="entered", entry_count=1, outcome="possible_win",
                           outcome_note=note)
        both = Competition(source="test", source_id="test-alert-both", thread_url="x", title="Win a toaster",
                           method="web_form", status="entered", entry_count=1, outcome="possible_win",
                           outcome_note=note)
        s.add_all([only, both])
        s.flush()
        s.add_all([
            Email(message_id="<a1@gmail.com>", from_addr="me@gmail.com", subject="[compbot] Possible competition win!",
                  category="possible_win", competition_id=only.id),
            Email(message_id="<a2@gmail.com>", from_addr="me@gmail.com", subject="[compbot] Possible competition win!",
                  category="scam_suspect", competition_id=both.id),
            Email(message_id="<real@toast.example>", from_addr="win@toast.example", subject="You've won a toaster",
                  category="possible_win", competition_id=both.id),
            Email(message_id="<fwd@gmail.com>", from_addr="me@gmail.com", subject="Fwd: you've won",
                  category="possible_win"),
        ])
        ids = (only.id, both.id)
    assert jobs.forget_own_alerts() == 2
    with session_scope() as s:
        left = set(s.scalars(select(Email.message_id).where(Email.message_id.in_(
            ["<a1@gmail.com>", "<a2@gmail.com>", "<real@toast.example>", "<fwd@gmail.com>"]))).all())
        assert left == {"<real@toast.example>", "<fwd@gmail.com>"}
        assert s.get(Competition, ids[0]).outcome == "pending"  # only the alert said it was a win
        b = s.get(Competition, ids[1])
        assert b.outcome == "possible_win" and b.outcome_note == "Email: You've won a toaster"
    assert jobs.forget_own_alerts() == 0