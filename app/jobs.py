"""Background jobs: discover, enter, check inbox, maintenance. Each job is single-flight (one at a time)."""
from __future__ import annotations

import collections
import logging
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from . import llm
from .analyse import apply_analysis, analyse, registrable_domain
from .config import get_config
from .db import (
    DISK_BUSY_MESSAGE,
    Competition,
    Email,
    EntryAttempt,
    JobRun,
    disk_stalled,
    disk_status,
    entries_today,
    get_profile,
    get_settings,
    get_state,
    is_busy_error,
    log_event,
    session_scope,
    set_state,
    utcnow,
)
from .entry.forms import Ctx
from .sources import mse

log = logging.getLogger("compbot.jobs")

_locks = {name: threading.Lock() for name in ("discover", "enter", "inbox", "maintenance")}
_progress: dict[str, str] = {}
_between: dict[str, tuple[str, float, float | None]] = {}  # job -> (what it's doing, since, until), for the Live page


def is_running(name: str) -> bool:
    return _locks[name].locked()


def progress(name: str) -> str:
    return _progress.get(name, "")


def between(name: str) -> dict | None:
    """What a running job is doing outside the browser (saving, waiting out the delay...), with epoch times."""
    v = _between.get(name)
    return {"what": v[0], "since": v[1], "until": v[2]} if v else None


def _step(name: str, what: str, seconds: float | None = None) -> None:
    now = time.time()
    _between[name] = (what, now, now + seconds if seconds else None)


def today_local() -> date:
    return datetime.now(ZoneInfo(get_config().tz)).date()


def _run(name: str, fn, *args, _held: bool = False, _note: str | None = None, **kwargs) -> str:
    """Run a job under its single-flight lock. `_held=True` means the caller already acquired the lock."""
    lock = _locks[name]
    if not _held and not lock.acquire(blocking=False):
        return f"{name} is already running"
    run_id = None
    try:
        if disk_stalled():
            # A write is stuck on the disk: starting now would just queue behind it and look like a hung run.
            msg = f"{name} not started: " + (disk_status() or {}).get("message", "the disk holding /data is stuck")
            log.warning(msg)
            return msg
        with session_scope() as s:
            jr = JobRun(job=name)
            s.add(jr)
            if _note:
                log_event(s, "user", _note)
            s.flush()
            run_id = jr.id
        summary = fn(*args, **kwargs) or ""
        with session_scope() as s:
            jr = s.get(JobRun, run_id)
            jr.finished_at, jr.status, jr.summary = utcnow(), "ok", summary[:2000]
        return summary
    except Exception as exc:  # noqa: BLE001
        why = f"{type(exc).__name__}: {exc}"
        if is_busy_error(exc):
            why = "Stopped: " + DISK_BUSY_MESSAGE
            log.warning("%s stopped: database busy (%s)", name, exc)
        else:
            log.exception("%s failed", name)
        try:
            with session_scope() as s:
                if run_id:
                    jr = s.get(JobRun, run_id)
                    jr.finished_at, jr.status, jr.summary = utcnow(), "error", why[:2000]
                log_event(s, "system", f"{name} job failed: {why}", "error")
        except Exception:  # noqa: BLE001
            log.warning("couldn't record that %s failed (database still busy)", name)
        return f"error: {why}"
    finally:
        _progress.pop(name, None)
        _between.pop(name, None)
        lock.release()


def start(name: str, fn, *args, _note: str | None = None, **kwargs) -> bool:
    """Atomically reserve the job lock, then run `fn` in a background thread. False if already running."""
    lock = _locks[name]
    if not lock.acquire(blocking=False):
        return False
    try:
        threading.Thread(target=_run, args=(name, fn, *args), kwargs={"_held": True, "_note": _note, **kwargs},
                         daemon=True, name=f"job-{name}").start()
    except Exception:
        lock.release()
        raise
    return True


def run_in_background(name: str, note: str | None = None) -> bool:
    """Start a job in a background thread; `note` is logged with it (from the job thread, so a slow disk can't
    hold up the web page that asked for it)."""
    target = {"discover": _discover, "enter": _enter, "inbox": _inbox, "maintenance": _maintenance}[name]
    return start(name, target, _note=note)


# --------------------------------------------------------------------------- discovery

def run_discovery() -> str:
    return _run("discover", _discover)


def _discover() -> str:
    with session_scope() as s:
        settings = get_settings(s)
        known = set(s.scalars(select(Competition.source_id)).all())
        last = get_state(s, "mse_last_run")
        blocked = blocked_domains(s)
    since = mse.default_since(datetime.fromisoformat(last) if last else None, int(settings.get("backfill_days", 14)))
    _progress["discover"] = "Fetching threads from MSE"
    threads = mse.fetch_threads(known, since)
    counts: dict[str, int] = {}
    use_llm = llm.enabled(settings) and settings.get("llm_for_posts", True)
    eff = dict(settings, _ai_answers=bool(llm.enabled(settings) and settings.get("ai_answers", True)))
    for i, t in enumerate(reversed(threads), 1):  # oldest first so ids increase with time
        _progress["discover"] = f"Analysing {i}/{len(threads)}"
        a = analyse(t["title"], t["body"], eff, today_local())
        if a.status == "queued" and a.method == "web_form" and a.entry_urls and all(
                registrable_domain(u) in blocked for u in a.entry_urls):
            a.status, a.reason = "manual", "This site has blocked compbot's browser before - enter it yourself"
        if use_llm and a.status == "manual" and a.method in ("unknown", "web_form") and not a.answers:
            hint = llm.analyse_post(t["title"], a.text, a.entry_urls + a.other_links, settings)
            if hint:
                if hint.get("entry_url") and not a.entry_urls:
                    a.entry_urls = [hint["entry_url"]]
                    a.promoter_domain = registrable_domain(hint["entry_url"])
                if hint.get("answers") and isinstance(hint["answers"], list):
                    a.answers = [str(x) for x in hint["answers"] if str(x).strip()][:5]
                if a.entry_urls and (a.answers or not a.question) and hint.get("method") == "web_form":
                    a.method, a.status, a.reason = "web_form", "queued", "Web form (post analysed by LLM)"
        with session_scope() as s:
            comp = Competition(source="mse", source_id=t["source_id"], thread_url=t["url"], title=t["title"][:500],
                               posted_at=t["posted_at"])
            apply_analysis(comp, a)
            s.add(comp)
            s.flush()
            log_event(s, "discovery", f"New: {comp.title} -> {comp.status} ({comp.status_reason})",
                      competition_id=comp.id)
        counts[a.status] = counts.get(a.status, 0) + 1
    with session_scope() as s:
        set_state(s, "mse_last_run", datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
        expired = _expire(s)
        summary = f"{len(threads)} new threads: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        if expired:
            summary += f"; {expired} expired"
        log_event(s, "discovery", summary, "success")
    return summary


def _expire(s) -> int:
    today = today_local()
    rows = s.scalars(select(Competition).where(Competition.closes_on < today,
                                               Competition.status.in_(["new", "queued", "manual", "failed"]))).all()
    for c in rows:
        c.status, c.status_reason = "expired", "Closed before it was entered"
    return len(rows)


# --------------------------------------------------------------------------- entries

ENTERED_RESULTS = {"entered", "unverified"}
MANUAL_RESULTS = {"captcha", "blocked", "tc_blocked", "needs_input"}


def run_entries(comp_ids: list[int] | None = None, force: bool = False) -> str:
    return _run("enter", _enter, comp_ids, force)


def _due_competitions(s, settings: dict, comp_ids: list[int] | None) -> list[Competition]:
    today = today_local()
    if comp_ids:
        return list(s.scalars(select(Competition).where(Competition.id.in_(comp_ids))).all())
    start_local = datetime.combine(today, datetime.min.time(), ZoneInfo(get_config().tz))
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    q = select(Competition).where(
        Competition.method.in_(["web_form", "email"]),
        or_(Competition.closes_on.is_(None), Competition.closes_on >= today),
        or_(
            Competition.status == "queued",
            (Competition.status == "entered") & Competition.is_daily.is_(True) & (
                or_(Competition.last_attempt_at.is_(None), Competition.last_attempt_at < start_utc)),
        ),
    ).order_by(Competition.closes_on.is_(None), Competition.closes_on, Competition.id)
    if settings.get("entry_mode") != "auto":
        q = q.where(Competition.last_attempt_at.is_(None))  # dry runs: try each comp once
    if not settings.get("enter_email_comps", True):
        q = q.where(Competition.method != "email")
    return list(s.scalars(q).all())


def _pending_urls(comp: Competition) -> list[str]:
    urls = list(comp.entry_urls or [])
    if comp.is_daily:
        return urls
    done = set(comp.entered_urls or [])
    return [u for u in urls if u not in done]


def _urls_to_try(comp: Competition, blocked: set[str] | None = None) -> list[str]:
    from .analyse import BOT_BLOCKED_URL_RE

    blocked = blocked or set()
    return [u for u in _pending_urls(comp) if not BOT_BLOCKED_URL_RE.search(u) and registrable_domain(u) not in blocked]


BLOCK_MEMORY_DAYS = 21
# Results that prove the site let compbot's browser load the page (whatever happened next).
PAGE_LOADED_RESULTS = ["entered", "unverified", "dry_run_ok", "failed", "needs_input", "tc_blocked", "closed"]


def blocked_domains(s) -> set[str]:
    """Sites that refused compbot's browser at least twice recently and never once let it load the page.

    A site that loaded even once (the entry then failed, needed input or hit a T&C rule...) doesn't block automated
    browsers - an occasional refusal there is a blip, or a false alarm - so it keeps being tried."""
    since = utcnow() - timedelta(days=BLOCK_MEMORY_DAYS)
    rows = s.execute(select(EntryAttempt.url, EntryAttempt.result).where(
        EntryAttempt.started_at >= since, EntryAttempt.mode != "manual",
        EntryAttempt.result.in_(["blocked", *PAGE_LOADED_RESULTS]))).all()
    blocked, ok = collections.Counter(), set()
    for url, result in rows:
        d = registrable_domain(url or "")
        if result == "blocked":
            blocked[d] += 1
        else:
            ok.add(d)
    return {d for d, n in blocked.items() if n >= 2 and d not in ok and d}


MAX_ERRORS_IN_A_ROW = 6  # more than this and the problem is compbot's side (network, browser), not the sites


def _enter(comp_ids: list[int] | None = None, force: bool = False) -> str:
    cfg = get_config()
    with session_scope() as s:
        settings = get_settings(s)
        profile = get_profile(s)
        missing = [k for k in ("first_name", "last_name", "email") if not profile.get(k)]
        if missing:
            log_event(s, "entry", "Entry run skipped: fill in your profile first (" + ", ".join(missing) + ")", "warn")
            return "Profile incomplete"
        cap = int(settings.get("daily_entry_cap", 25))
        done_today = entries_today(s, cfg.tz, settings.get("entry_mode") != "auto")
        comps = _due_competitions(s, settings, comp_ids)
        comp_ids_ordered = [c.id for c in comps]
        blocked = blocked_domains(s)
    run_blocks: collections.Counter = collections.Counter()
    # The daily cap counts entries (as the dashboard does), so blocked, failed or CAPTCHA attempts don't use it up.
    # A forced retry instead allows a fixed number of attempts.
    counted = {"dry_run_ok"} if settings.get("entry_mode") != "auto" else ENTERED_RESULTS
    budget = max(0, cap - done_today) if not force else max(1, len(comp_ids_ordered) * 6)
    if budget == 0:
        return f"Daily cap reached ({cap})"
    stats: dict[str, int] = {}
    errors_in_a_row, stopped = 0, ""

    def spend(result: str) -> None:
        nonlocal budget, errors_in_a_row
        if force or result in counted:
            budget -= 1
        errors_in_a_row = errors_in_a_row + 1 if result == "error" else 0

    for n, cid in enumerate(comp_ids_ordered, 1):
        left = len(comp_ids_ordered) - n + 1
        if budget <= 0:
            stopped = (f"stopped at the daily cap of {cap} entries, {left} left for the next run" if not force
                       else f"stopped after the retry's attempt limit, {left} left")
            break
        if errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
            stopped = (f"stopped after {errors_in_a_row} errors in a row (is the network or browser OK?), "
                       f"{left} left for the next run")
            break
        with session_scope() as s:
            comp = s.get(Competition, cid)
            pending = _pending_urls(comp)
            urls = _urls_to_try(comp, blocked) or (list(comp.entry_urls or []) if force else [])
            if comp.force_next:  # you asked to retry this one: try every site once, block memory or not
                urls = pending or list(comp.entry_urls or [])
                comp.force_next = False
            method = comp.method
            _progress["enter"] = f"{n}/{len(comp_ids_ordered)}: {comp.title[:60]}"
            if method == "web_form" and pending and not urls:
                done_some = bool(comp.entered_urls)
                comp.status = "manual"
                comp.status_reason = ("Entered on some sites; the rest " if done_some else "This site ") + \
                    "blocks automated entry (refused compbot's browser, not a T&C rule) - enter it yourself"
                log_event(s, "entry", f"Skipped (site refused compbot's browser before - not a T&C rule): "
                                      f"{comp.title[:80]}", "warn", cid)
                stats["skipped_blocked_site"] = stats.get("skipped_blocked_site", 0) + 1
                continue
        if method == "email":
            _step("enter", "Sending the email entry")
            result = _enter_email(cid, settings, profile, force=force)
            stats[result] = stats.get(result, 0) + 1
            spend(result)
            continue
        if not urls:
            continue
        results: dict[str, str] = {}
        for url in urls:
            if budget <= 0 or errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
                break
            _step("enter", "Starting the browser")
            results[url] = _enter_web(cid, url, settings, profile)
            spend(results[url])
            if results[url] == "blocked":
                d = registrable_domain(url)
                run_blocks[d] += 1
                if run_blocks[d] >= 2:
                    blocked.add(d)
            if len(urls) > 1:
                pause = random.uniform(3, 8)
                _step("enter", "Waiting before the next site", pause)
                time.sleep(pause)
        for o in results.values():
            stats[o] = stats.get(o, 0) + 1
        _finalise_web(cid, results, settings)
        if n < len(comp_ids_ordered) and budget > 0 and errors_in_a_row < MAX_ERRORS_IN_A_ROW:
            lo, hi = int(settings.get("delay_min_seconds", 20)), int(settings.get("delay_max_seconds", 60))
            pause = random.uniform(min(lo, hi), max(lo, hi)) if not comp_ids else 2
            _step("enter", "Waiting before the next competition", pause)
            time.sleep(pause)
    summary = ", ".join(f"{v} {k}" for k, v in sorted(stats.items())) or "Nothing to enter"
    if stopped:
        summary += f" - {stopped}"
    with session_scope() as s:
        log_event(s, "entry", f"Entry run finished: {summary}", "warn" if "errors in a row" in stopped else "success")
    return summary


def _enter_web(cid: int, url: str, settings: dict, profile: dict) -> str:
    from .entry.browser import enter_isolated

    with session_scope() as s:
        comp = s.get(Competition, cid)
        att = EntryAttempt(competition_id=cid, url=url, mode="dry_run" if settings.get("entry_mode") != "auto" else "auto")
        s.add(att)
        s.flush()
        att_id = att.id
        ctx = Ctx(profile=profile, answers=list(comp.answers or []), question=comp.question, tags=list(comp.tags or []),
                  marketing_opt_in=bool(settings.get("marketing_opt_in")))
        comp_context = {"title": comp.title, "question": comp.question, "known_answers": comp.answers,
                        "post_excerpt": (comp.body_text or "")[:800]}
    r = enter_isolated(url, ctx, settings, f"c{cid}_a{att_id}", comp_context)
    _step("enter", "Saving the result")
    with session_scope() as s:
        att = s.get(EntryAttempt, att_id)
        att.result, att.message, att.details = r.result, r.message, r.details
        att.screenshot_before, att.screenshot_after, att.final_url = r.screenshot_before, r.screenshot_after, r.final_url
        att.finished_at = utcnow()
        level = "success" if r.result in ENTERED_RESULTS | {"dry_run_ok"} else (
            "warn" if r.result in MANUAL_RESULTS | {"closed"} else "error")
        ai_used = [x for x in (r.details or {}).get("ai", []) if x.get("used")]
        ai_note = (" [AI answered: " + "; ".join(f"{x['answer'][:40]}" + (f" {x['confidence']}%" if x.get("confidence")
                                                                            is not None else "") for x in ai_used) + "]"
                   if ai_used else "")
        shutdown = ((r.details or {}).get("timing") or {}).get("shutdown_seconds")
        if isinstance(shutdown, str):
            ai_note += f" [browser shutdown {shutdown}]"
        log_event(s, "entry", f"{r.result}: {s.get(Competition, cid).title[:80]} - {r.message[:300]}{ai_note}", level, cid)
    return r.result


def _finalise_web(cid: int, results: dict[str, str], settings: dict) -> None:
    """Update a competition from this run's per-URL results (multi-site comps can finish across several runs)."""
    if not results:
        return
    now = utcnow()
    with session_scope() as s:
        comp = s.get(Competition, cid)
        comp.last_attempt_at = now
        entered_now = [u for u, r in results.items() if r in ENTERED_RESULTS]
        unverified = [u for u, r in results.items() if r == "unverified"]
        manual = [u for u, r in results.items() if r in MANUAL_RESULTS]
        failed = [u for u, r in results.items() if r not in ENTERED_RESULTS | MANUAL_RESULTS | {"dry_run_ok", "closed"}]
        if all(r == "dry_run_ok" for r in results.values()):
            comp.status_reason = "Dry run OK - will submit once entry mode is set to auto"
            return
        if entered_now:
            comp.entry_count += len(entered_now)
            comp.entered_at = comp.entered_at or now
            comp.last_entered_at = now
            if not comp.is_daily:
                comp.entered_urls = list(dict.fromkeys(list(comp.entered_urls or []) + entered_now))
            if unverified:
                comp.needs_review = True
        all_urls = list(comp.entry_urls or [])
        remaining = [u for u in all_urls if u not in (entered_now if comp.is_daily else (comp.entered_urls or []))]
        n_done, n_all = len(all_urls) - len(remaining), len(all_urls)
        site_note = f" on {n_done} of {n_all} sites" if n_all > 1 else ""
        review_note = " - submitted but no confirmation seen, please check the screenshot" if unverified else ""

        if not remaining:
            comp.status, comp.failed_attempts = "entered", 0
            comp.status_reason = ("Entered automatically" + site_note + review_note +
                                  (" (daily - re-entered each day)" if comp.is_daily else ""))
        elif manual:
            comp.status = "manual"
            last = s.scalars(select(EntryAttempt).where(EntryAttempt.competition_id == cid,
                                                        EntryAttempt.result.in_(list(MANUAL_RESULTS)))
                             .order_by(EntryAttempt.id.desc())).first()
            prefix = f"Entered{site_note}; " if n_done else ""
            comp.status_reason = prefix + (f"needs you: {last.message[:400]}" if last else "needs manual entry")
        elif "closed" in results.values() and not entered_now and not n_done:
            comp.status, comp.status_reason = "expired", "Competition page says it has closed"
        else:
            if failed:
                comp.failed_attempts += 1
            too_many = comp.failed_attempts >= int(settings.get("max_attempts", 2))
            comp.status = "manual" if too_many else "queued"
            if too_many:
                comp.status_reason = f"Automatic entry failed {comp.failed_attempts} times" + (
                    f" (entered{site_note})" if n_done else "")
            elif n_done:
                comp.status_reason = f"Partly entered{site_note}{review_note} - will continue next run"
            else:
                comp.status_reason = f"Attempt {comp.failed_attempts} failed - will retry"


def _email_clause(post_text: str, links: list[str]) -> str | None:
    """Automation-ban check for email comps: the post text plus the promoter's competition page/T&Cs."""
    from bs4 import BeautifulSoup

    from .entry.forms import find_automation_clause
    from .netguard import guarded_client

    text = post_text or ""
    pages = [u for u in links if u.startswith("http")][:2]
    if pages:
        try:
            with guarded_client(headers={"User-Agent": get_config().user_agent}, timeout=20) as client:
                for u in pages:
                    r = client.get(u)
                    if r.is_success and "html" in (r.headers.get("content-type") or ""):
                        text += "\n" + BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)[:200000]
        except Exception as exc:  # noqa: BLE001
            log.debug("email comp page fetch failed: %s", exc)
    return find_automation_clause(text)


def _enter_email(cid: int, settings: dict, profile: dict, force: bool = False) -> str:
    from .entry import emailer

    cfg = get_config()
    with session_scope() as s:
        comp = s.get(Competition, cid)
        subject, body = emailer.compose_entry(comp, profile)
        to = comp.email_to
        post_text, links = comp.body_text or "", list(comp.other_links or []) + list(comp.entry_urls or [])
        dry = settings.get("entry_mode") != "auto"
        att = EntryAttempt(competition_id=cid, url=f"mailto:{to}", mode="dry_run" if dry else "email")
        s.add(att)
        s.flush()
        att_id = att.id
    clause = _email_clause(post_text, links) if settings.get("respect_automation_clauses", True) else None
    if not to:
        result, msg = "needs_input", "No email address to send the entry to"
    elif not force and not settings.get("enter_email_comps", True):
        result, msg = "needs_input", "Email entries are switched off in settings"
    elif clause:
        result, msg = "tc_blocked", f"T&Cs ban automated entries: \u201c{clause[:300]}\u201d"
    elif dry:
        result, msg = "dry_run_ok", f"Dry run: would email {to}\nSubject: {subject}\n\n{body}"
    elif not cfg.mail_enabled:
        result, msg = "needs_input", "Gmail is not configured, so email entries can't be sent"
    else:
        try:
            emailer.send_mail(to, subject, body)
            result, msg = "entered", f"Emailed {to} (subject: {subject})"
        except Exception as exc:  # noqa: BLE001
            result, msg = "error", f"Send failed: {exc}"
    with session_scope() as s:
        att = s.get(EntryAttempt, att_id)
        att.result, att.message, att.finished_at = result, msg, utcnow()
        att.details = {"subject": subject, "to": to}
        comp = s.get(Competition, cid)
        comp.last_attempt_at = utcnow()
        if result == "entered":
            comp.status, comp.entered_at, comp.entry_count = "entered", comp.entered_at or utcnow(), comp.entry_count + 1
            comp.last_entered_at = utcnow()
            comp.status_reason = f"Entered by email to {to}"
        elif result in ("needs_input", "tc_blocked"):
            comp.status, comp.status_reason = "manual", msg
        elif result == "dry_run_ok":
            comp.status_reason = "Dry run OK - email will be sent once entry mode is auto"
        else:
            comp.failed_attempts += 1
            if comp.failed_attempts >= int(settings.get("max_attempts", 2)):
                comp.status, comp.status_reason = "manual", msg
        log_event(s, "entry", f"{result}: {comp.title[:80]} - {msg.splitlines()[0][:200]}",
                  "success" if result in ("entered", "dry_run_ok") else "warn", cid)
    return result


# --------------------------------------------------------------------------- inbox

def run_inbox() -> str:
    return _run("inbox", _inbox)


def _inbox() -> str:
    from . import inbox
    from .entry import emailer

    cfg = get_config()
    if not cfg.mail_enabled:
        return "Gmail not configured"
    with session_scope() as s:
        settings = get_settings(s)
        last = get_state(s, "inbox_last_run")
        known = set(s.scalars(select(Email.message_id)).all())
        cutoff = utcnow() - timedelta(days=200)
        # Detached snapshot of entered comps for matching (expire_on_commit=False keeps attributes loaded).
        comps = list(s.scalars(select(Competition).where(
            or_(Competition.status == "entered", Competition.entry_count > 0),
            or_(Competition.last_entered_at.is_(None), Competition.last_entered_at >= cutoff))).all())
    started = utcnow()
    since = inbox.default_since(datetime.fromisoformat(last) if last else None)
    _progress["inbox"] = "Connecting to Gmail"
    msgs, truncated = inbox.fetch_messages(cfg.imap_host, cfg.gmail_address, cfg.gmail_app_password,
                                           cfg.imap_folders, since, known)
    counts: dict[str, int] = {}
    titles = {c.id: c.title for c in comps}
    for i, m in enumerate(msgs, 1):
        _progress["inbox"] = f"Classifying {i}/{len(msgs)}"
        if m.message_id in known:
            continue
        known.add(m.message_id)
        c = inbox.classify(m, comps)
        if llm.enabled(settings) and settings.get("llm_for_email") and (c.score >= 1 or c.competition_id):
            verdict = llm.classify_email(m.from_addr, m.subject, m.text, titles.get(c.competition_id), settings)
            if verdict and verdict.get("category") in {"possible_win", "loss", "confirm", "entry_ack", "promo",
                                                       "scam_suspect", "other"}:
                c.signals.append(f"llm:{verdict['category']} ({str(verdict.get('reason', ''))[:80]})")
                c.category = verdict["category"]
        counts[c.category] = counts.get(c.category, 0) + 1
        # 1) Record the email and any outcome change (committed on its own, so one bad mail can't undo others).
        follow_up = None
        with session_scope() as s:
            row = Email(message_id=m.message_id[:500], folder=m.folder, from_addr=m.from_addr[:300],
                        from_name=m.from_name[:300], from_domain=m.from_domain[:200], subject=m.subject[:1000],
                        received_at=m.received_at, snippet=m.text[:1500], category=c.category, score=c.score,
                        signals=c.signals, competition_id=c.competition_id)
            s.add(row)
            s.flush()
            email_id = row.id
            comp = s.get(Competition, c.competition_id) if c.competition_id else None
            if c.category == "possible_win":
                if comp and comp.outcome != "won":
                    comp.outcome, comp.outcome_note = "possible_win", f"Email: {m.subject[:200]}"
                where = f" (looks like: {comp.title})" if comp else " (no matching entry found - check it's genuine)"
                log_event(s, "inbox", f"Possible win: '{m.subject}' from {m.from_addr}{where}", "success",
                          comp.id if comp else None)
                follow_up = ("notify", f"From: {m.from_addr}\nSubject: {m.subject}\n{where}\n\n"
                             "Open compbot to review. Never pay a fee or give bank details to claim a prize.")
            elif c.category == "loss" and comp and comp.outcome == "pending":
                comp.outcome, comp.outcome_note = "lost", f"Email: {m.subject[:200]}"
            elif c.category == "confirm" and c.confirm_link and comp and settings.get("auto_confirm_links", True):
                recent = comp.last_entered_at or comp.entered_at
                if recent and recent >= utcnow() - timedelta(days=10):
                    row.action = "confirmation link pending"
                    follow_up = ("confirm", c.confirm_link, comp.id, comp.title)
            elif c.category == "entry_ack" and comp and comp.needs_review:
                comp.needs_review = False
                row.action = "Entry acknowledged by promoter - cleared 'check entry' flag"
            elif c.category == "scam_suspect":
                row.action = "Flagged as a likely scam (asks for fees/bank details)"
        # 2) Side effects after the commit; record their result separately.
        if follow_up and follow_up[0] == "notify":
            emailer.notify("Possible competition win!", follow_up[1])
        elif follow_up and follow_up[0] == "confirm":
            outcome = _click_confirm(follow_up[1])
            with session_scope() as s:
                s.get(Email, email_id).action = outcome
                log_event(s, "inbox", f"Confirmation link for '{follow_up[3][:80]}': {outcome}", "info", follow_up[2])
    with session_scope() as s:
        if not truncated:
            set_state(s, "inbox_last_run", started.isoformat())
        summary = f"{len(msgs)} new emails: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        if truncated:
            summary += " (more waiting - will continue next run)"
        log_event(s, "inbox", summary, "success")
    return summary


def _click_confirm(url: str) -> str:
    from .netguard import guarded_client

    try:
        with guarded_client(headers={"User-Agent": get_config().user_agent}) as client:
            r = client.get(url)
        return f"opened confirmation link (HTTP {r.status_code})"
    except Exception as exc:  # noqa: BLE001
        return f"confirmation link failed: {exc}"


# --------------------------------------------------------------------------- maintenance

RETRYABLE_RESULTS = {"failed", "needs_input", "error"}

# Why a competition is in the manual queue -> label, help text, and whether compbot can retry it.
RETRY_CATEGORIES: dict[str, dict] = {
    "question": {"label": "Question the post didn't answer", "retry": True,
                 "help": "The AI (if you've chosen a model) will try to answer it from the page and its knowledge."},
    "failed": {"label": "No form found / submit problem / form error", "retry": True,
               "help": "Often fixed by newer versions of compbot - worth retrying after an update."},
    "error": {"label": "Page error or time-out", "retry": True, "help": "Usually temporary."},
    "needs_input": {"label": "A field compbot couldn't fill", "retry": True,
                    "help": "Retry after updating compbot or adding missing details to your profile."},
    "phone": {"label": "Needs a phone number", "retry": True,
              "help": "Add your phone number in Settings first, then retry."},
    "captcha": {"label": "CAPTCHA", "retry": True,
                "help": "compbot now ignores invisible reCAPTCHA badges, so some of these may go through now. Real "
                        "\u201cI'm not a robot\u201d checks will come straight back here."},
    "blocked": {"label": "Site blocks automated browsers", "retry": True,
                "help": "Tried once more, even though the site blocked compbot before - most will be blocked again."},
    "tc": {"label": "T&Cs ban automated entries", "retry": True,
           "help": "The T&Cs are checked again on retry, so these come back unless the T&Cs have changed."},
    "other": {"label": "Other", "retry": True, "help": ""},
    "login": {"label": "Needs you to sign in", "retry": False, "help": "compbot can't log in to sites for you."},
    "social": {"label": "Social media, Gleam, app, vote or magazine", "retry": False,
               "help": "Not something compbot can enter automatically."},
}
CATEGORY_ORDER = ["question", "failed", "error", "needs_input", "phone", "captcha", "blocked", "tc", "other",
                  "login", "social"]
QUESTION_NOTES_RE = ("question with no known answer", "required choice with no known answer", "AI wasn't sure",
                     "AI couldn't", "second check")


def manual_category(c: Competition, last: EntryAttempt | None) -> str:
    """Why a manual-queue competition is there (see RETRY_CATEGORIES)."""
    from .analyse import ACCOUNT_URL_RE

    if c.method == "account" or (c.method == "web_form" and any(ACCOUNT_URL_RE.search(u) for u in (c.entry_urls or []))):
        return "login"
    if c.method in ("social", "widget", "magazine", "vote", "purchase"):
        return "social"
    reason = c.status_reason or ""
    if last is None:
        if reason.startswith(("Needs an answer", "Re-queued: the AI")) or "needs answers" in reason:
            return "question"
        if "blocks automated entry" in reason or "blocked compbot" in reason:
            return "blocked"
        return "other"
    msg = last.message or ""
    if "blocks automated entry" in reason and last.result not in ("blocked",):
        return "blocked"
    if last.result == "captcha":
        return "captcha"
    if last.result == "blocked":
        return "blocked"
    if last.result == "tc_blocked":
        return "tc"
    if last.result == "needs_input":
        if "profile has no phone" in msg:
            return "phone"
        if any(k in msg for k in QUESTION_NOTES_RE):
            return "question"
        return "needs_input"
    if last.result == "failed":
        return "failed"
    if last.result == "error":
        return "error"
    return "other"


def manual_breakdown(s) -> tuple[dict[str, list[int]], dict[int, str]]:
    """Open manual-queue competitions grouped by why they're there."""
    today = today_local()
    comps = s.scalars(select(Competition).where(
        Competition.status == "manual", or_(Competition.closes_on.is_(None), Competition.closes_on >= today))).all()
    ids = [c.id for c in comps]
    last: dict[int, EntryAttempt] = {}
    if ids:
        for att in s.scalars(select(EntryAttempt).where(EntryAttempt.competition_id.in_(ids),
                                                        EntryAttempt.mode != "dry_run")
                             .order_by(EntryAttempt.id.desc())).all():
            last.setdefault(att.competition_id, att)
    groups: dict[str, list[int]] = {k: [] for k in CATEGORY_ORDER}
    by_id: dict[int, str] = {}
    for c in comps:
        cat = manual_category(c, last.get(c.id))
        groups.setdefault(cat, []).append(c.id)
        by_id[c.id] = cat
    return groups, by_id


def recommended_categories(settings: dict, profile: dict) -> set[str]:
    rec = {"failed", "error", "needs_input", "captcha", "other"}
    if llm.enabled(settings) and settings.get("ai_answers", True):
        rec.add("question")
    if profile.get("phone"):
        rec.add("phone")
    return rec


def requeue_categories(categories: list[str] | set[str]) -> str:
    """Send manual-queue competitions with the chosen reasons back to the queue."""
    wanted = {c for c in categories if RETRY_CATEGORIES.get(c, {}).get("retry")}
    if not wanted:
        return "Nothing selected to retry"
    counts: collections.Counter = collections.Counter()
    with session_scope() as s:
        _, by_id = manual_breakdown(s)
        for cid, cat in by_id.items():
            if cat not in wanted:
                continue
            c = s.get(Competition, cid)
            c.status, c.failed_attempts, c.last_attempt_at, c.needs_review = "queued", 0, None, False
            c.force_next = cat == "blocked"
            c.status_reason = f"Re-queued by you ({RETRY_CATEGORIES[cat]['label'].lower()})"
            counts[cat] += 1
        total = sum(counts.values())
        detail = ", ".join(f"{n} {RETRY_CATEGORIES[k]['label'].lower()}" for k, n in counts.most_common())
        msg = f"Re-queued {total} competition{'s' if total != 1 else ''}" + (f": {detail}" if detail else "")
        log_event(s, "user", msg)
    return msg


def requeue_retryable() -> str:
    """The 'recommended' retry: everything compbot may now manage."""
    with session_scope() as s:
        rec = recommended_categories(get_settings(s), get_profile(s))
    return requeue_categories(rec)


def clear_dry_runs(requeue_manual: bool = False) -> str:
    """Delete dry-run attempts + their screenshots and reset competitions that were only ever dry-run."""
    from sqlalchemy import delete

    cfg = get_config()
    shots: list[str] = []
    with session_scope() as s:
        rows = s.execute(select(EntryAttempt.id, EntryAttempt.competition_id, EntryAttempt.screenshot_before,
                                EntryAttempt.screenshot_after).where(EntryAttempt.mode == "dry_run")).all()
        if not rows:
            return "No dry-run history to clear"
        comp_ids = {r.competition_id for r in rows}
        shots = [n for r in rows for n in (r.screenshot_before, r.screenshot_after) if n]
        s.execute(delete(EntryAttempt).where(EntryAttempt.mode == "dry_run"))
        s.flush()
        reset = requeued = 0
        today = today_local()
        for cid in comp_ids:
            c = s.get(Competition, cid)
            if c is None:
                continue
            real = s.scalar(select(EntryAttempt.id).where(EntryAttempt.competition_id == cid).limit(1))
            if real is not None:
                if (c.status_reason or "").startswith("Dry run OK"):
                    c.status_reason = "Queued"
                continue
            c.last_attempt_at, c.failed_attempts = None, 0
            reset += 1
            open_ = c.closes_on is None or c.closes_on >= today
            if (c.status_reason or "").startswith("Dry run OK"):
                c.status_reason = "Queued"
            elif requeue_manual and c.status == "manual" and c.method in ("web_form", "email") and open_ and (
                    c.entry_urls or c.email_to):
                c.status, c.status_reason = "queued", "Re-queued after clearing dry-run history"
                requeued += 1
        summary = f"Cleared {len(rows)} dry-run attempts on {len(comp_ids)} competitions ({reset} reset" + (
            f", {requeued} re-queued from manual" if requeue_manual else "") + ")"
        log_event(s, "user", summary, "warn")
    removed = 0
    for name in shots:
        p = cfg.screenshot_dir / name
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return f"{summary}; {removed} screenshots deleted"


def run_maintenance() -> str:
    return _run("maintenance", _maintenance)


def _maintenance() -> str:
    cfg = get_config()
    with session_scope() as s:
        settings = get_settings(s)
        expired = _expire(s)
        days = int(settings.get("presumed_lost_days", 60))
        cutoff = today_local() - timedelta(days=days)
        lost = s.scalars(select(Competition).where(Competition.outcome == "pending", Competition.status == "entered",
                                                   Competition.closes_on.is_not(None),
                                                   Competition.closes_on < cutoff)).all()
        for c in lost:
            c.outcome, c.outcome_note = "lost", f"Presumed lost: no win email {days} days after closing"
        # Prune screenshots older than 120 days to save space.
        pruned = 0
        old = time.time() - 120 * 86400
        if cfg.screenshot_dir.exists():
            for p in cfg.screenshot_dir.glob("*.jpg"):
                if p.stat().st_mtime < old:
                    p.unlink(missing_ok=True)
                    pruned += 1
        summary = f"{expired} expired, {len(lost)} presumed lost, {pruned} old screenshots removed"
        log_event(s, "system", f"Maintenance: {summary}")
    return summary
