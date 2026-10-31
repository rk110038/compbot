"""Background jobs: discover, enter, check inbox, maintenance. Each job is single-flight (one at a time)."""
from __future__ import annotations

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
    Competition,
    Email,
    EntryAttempt,
    JobRun,
    entries_today,
    get_profile,
    get_settings,
    get_state,
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


def is_running(name: str) -> bool:
    return _locks[name].locked()


def progress(name: str) -> str:
    return _progress.get(name, "")


def today_local() -> date:
    return datetime.now(ZoneInfo(get_config().tz)).date()


def _run(name: str, fn, *args, _held: bool = False, **kwargs) -> str:
    """Run a job under its single-flight lock. `_held=True` means the caller already acquired the lock."""
    lock = _locks[name]
    if not _held and not lock.acquire(blocking=False):
        return f"{name} is already running"
    run_id = None
    try:
        with session_scope() as s:
            jr = JobRun(job=name)
            s.add(jr)
            s.flush()
            run_id = jr.id
        summary = fn(*args, **kwargs) or ""
        with session_scope() as s:
            jr = s.get(JobRun, run_id)
            jr.finished_at, jr.status, jr.summary = utcnow(), "ok", summary[:2000]
        return summary
    except Exception as exc:  # noqa: BLE001
        log.exception("%s failed", name)
        with session_scope() as s:
            if run_id:
                jr = s.get(JobRun, run_id)
                jr.finished_at, jr.status, jr.summary = utcnow(), "error", f"{type(exc).__name__}: {exc}"[:2000]
            log_event(s, "system", f"{name} job failed: {exc}", "error")
        return f"error: {exc}"
    finally:
        _progress.pop(name, None)
        lock.release()


def start(name: str, fn, *args, **kwargs) -> bool:
    """Atomically reserve the job lock, then run `fn` in a background thread. False if already running."""
    lock = _locks[name]
    if not lock.acquire(blocking=False):
        return False
    try:
        threading.Thread(target=_run, args=(name, fn, *args), kwargs={"_held": True, **kwargs}, daemon=True,
                         name=f"job-{name}").start()
    except Exception:
        lock.release()
        raise
    return True


def run_in_background(name: str) -> bool:
    target = {"discover": _discover, "enter": _enter, "inbox": _inbox, "maintenance": _maintenance}[name]
    return start(name, target)


# --------------------------------------------------------------------------- discovery

def run_discovery() -> str:
    return _run("discover", _discover)


def _discover() -> str:
    with session_scope() as s:
        settings = get_settings(s)
        known = set(s.scalars(select(Competition.source_id)).all())
        last = get_state(s, "mse_last_run")
    since = mse.default_since(datetime.fromisoformat(last) if last else None, int(settings.get("backfill_days", 14)))
    _progress["discover"] = "Fetching threads from MSE"
    threads = mse.fetch_threads(known, since)
    counts: dict[str, int] = {}
    use_llm = llm.enabled() and settings.get("llm_for_posts", True)
    for i, t in enumerate(reversed(threads), 1):  # oldest first so ids increase with time
        _progress["discover"] = f"Analysing {i}/{len(threads)}"
        a = analyse(t["title"], t["body"], settings, today_local())
        if use_llm and a.status == "manual" and a.method in ("unknown", "web_form") and not a.answers:
            hint = llm.analyse_post(t["title"], a.text, a.entry_urls + a.other_links)
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


def _urls_to_try(comp: Competition) -> list[str]:
    urls = list(comp.entry_urls or [])
    if comp.is_daily:
        return urls
    done = set(comp.entered_urls or [])
    return [u for u in urls if u not in done]


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
    budget = max(0, cap - done_today) if not force else max(1, len(comp_ids_ordered) * 6)
    if budget == 0:
        return f"Daily cap reached ({cap})"
    stats: dict[str, int] = {}
    browser = None
    try:
        for n, cid in enumerate(comp_ids_ordered, 1):
            if budget <= 0:
                break
            with session_scope() as s:
                comp = s.get(Competition, cid)
                urls = _urls_to_try(comp) or (list(comp.entry_urls or []) if force else [])
                method = comp.method
                _progress["enter"] = f"{n}/{len(comp_ids_ordered)}: {comp.title[:60]}"
            if method == "email":
                result = _enter_email(cid, settings, profile, force=force)
                stats[result] = stats.get(result, 0) + 1
                budget -= 1
                continue
            if not urls:
                continue
            if browser is None:
                from .entry.browser import Browser
                browser = Browser()
            results: dict[str, str] = {}
            for url in urls:
                if budget <= 0:
                    break
                results[url] = _enter_web(browser, cid, url, settings, profile)
                budget -= 1
                if len(urls) > 1:
                    time.sleep(random.uniform(3, 8))
            for o in results.values():
                stats[o] = stats.get(o, 0) + 1
            _finalise_web(cid, results, settings)
            if n < len(comp_ids_ordered) and budget > 0:
                lo, hi = int(settings.get("delay_min_seconds", 20)), int(settings.get("delay_max_seconds", 60))
                time.sleep(random.uniform(min(lo, hi), max(lo, hi)) if not comp_ids else 2)
    finally:
        if browser:
            browser.close()
    summary = ", ".join(f"{v} {k}" for k, v in sorted(stats.items())) or "Nothing to enter"
    with session_scope() as s:
        log_event(s, "entry", f"Entry run finished: {summary}", "success")
    return summary


def _enter_web(browser, cid: int, url: str, settings: dict, profile: dict) -> str:
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
    r = browser.enter(url, ctx, settings, f"c{cid}_a{att_id}", comp_context)
    with session_scope() as s:
        att = s.get(EntryAttempt, att_id)
        att.result, att.message, att.details = r.result, r.message, r.details
        att.screenshot_before, att.screenshot_after, att.final_url = r.screenshot_before, r.screenshot_after, r.final_url
        att.finished_at = utcnow()
        level = "success" if r.result in ENTERED_RESULTS | {"dry_run_ok"} else (
            "warn" if r.result in MANUAL_RESULTS | {"closed"} else "error")
        log_event(s, "entry", f"{r.result}: {s.get(Competition, cid).title[:80]} - {r.message[:300]}", level, cid)
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
        if llm.enabled() and settings.get("llm_for_email") and (c.score >= 1 or c.competition_id):
            verdict = llm.classify_email(m.from_addr, m.subject, m.text, titles.get(c.competition_id))
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
