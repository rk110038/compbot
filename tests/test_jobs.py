from sqlalchemy import select

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
