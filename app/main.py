"""FastAPI web GUI + scheduler bootstrap."""
from __future__ import annotations

import base64
import csv
import io
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__, jobs
from .config import DEFAULT_PROFILE, DEFAULT_SETTINGS, get_config
from .db import (
    Competition,
    Email,
    EntryAttempt,
    Event,
    JobRun,
    entries_today,
    get_profile,
    get_settings,
    init_db,
    log_event,
    save_profile,
    save_settings,
    session_scope,
    utcnow,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("compbot.web")

BASE = Path(__file__).resolve().parent
cfg = get_config()
templates = Jinja2Templates(directory=str(BASE / "templates"))
scheduler = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global scheduler
    init_db()
    with session_scope() as s:
        for jr in s.scalars(select(JobRun).where(JobRun.status == "running")).all():
            jr.status, jr.finished_at, jr.summary = "error", utcnow(), "Interrupted (app restarted)"
        log_event(s, "system", f"compbot {__version__} started")
    if cfg.scheduler_enabled:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BackgroundScheduler(timezone=cfg.tz, job_defaults={"coalesce": True, "misfire_grace_time": 3600})
        for name, expr, fn in [("discover", cfg.discover_cron, jobs.run_discovery),
                               ("enter", cfg.enter_cron, jobs.run_entries),
                               ("inbox", cfg.inbox_cron, jobs.run_inbox),
                               ("maintenance", cfg.maintenance_cron, jobs.run_maintenance)]:
            scheduler.add_job(fn, CronTrigger.from_crontab(expr, timezone=cfg.tz), id=name, name=name,
                              max_instances=1)
        scheduler.start()
        log.info("Scheduler started")
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="compbot", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# --------------------------------------------------------------------------- template helpers

def _local(dt: datetime | None, fmt: str = "%d %b %H:%M") -> str:
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(cfg.tz)).strftime(fmt)


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    secs = (utcnow() - dt).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(secs // 60)} min ago"
    if secs < 129600:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} days ago"


def _days_left(d) -> str:
    if not d:
        return ""
    n = (d - jobs.today_local()).days
    return "closed" if n < 0 else "today" if n == 0 else "1 day" if n == 1 else f"{n} days"


templates.env.filters["local"] = _local
templates.env.filters["ago"] = _ago
templates.env.filters["days_left"] = _days_left
templates.env.globals["version"] = __version__
templates.env.globals["cfg"] = cfg


def _qs(request: Request, **changes) -> str:
    params = dict(request.query_params)
    params.update({k: v for k, v in changes.items() if v is not None})
    return "?" + urlencode({k: v for k, v in params.items() if v not in ("", None)})


templates.env.globals["qs"] = _qs


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("flash", request.query_params.get("msg"))
    with session_scope() as s:
        ctx.setdefault("settings", get_settings(s))
        ctx["nav_manual"] = s.scalar(select(func.count(Competition.id)).where(Competition.status == "manual")) or 0
        ctx["nav_wins"] = s.scalar(select(func.count(Competition.id)).where(Competition.outcome == "possible_win")) or 0
    ctx["running"] = {n: jobs.progress(n) or "running" for n in ("discover", "enter", "inbox", "maintenance")
                      if jobs.is_running(n)}
    ctx["auth_disabled"] = not cfg.web_password
    return templates.TemplateResponse(request, name, ctx)


def back(request: Request, default: str, msg: str | None = None) -> RedirectResponse:
    """Redirect to the referring page on this site (or `default`), optionally with a flash message."""
    path, params = default, {}
    ref = request.headers.get("referer")
    if ref:
        p = urlparse(ref)
        if p.netloc == request.headers.get("host"):
            path = p.path or default
            params = {k: v for k, v in parse_qsl(p.query) if k != "msg"}
    if msg:
        params["msg"] = msg
    return RedirectResponse(path + ("?" + urlencode(params) if params else ""), status_code=303)


# --------------------------------------------------------------------------- security middleware

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if cfg.web_password:
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                    ok = secrets.compare_digest(user, cfg.web_user) and secrets.compare_digest(pwd, cfg.web_password)
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return PlainTextResponse("Authentication required", status_code=401,
                                         headers={"WWW-Authenticate": 'Basic realm="compbot"'})
        if request.method == "POST":
            # Same-origin check (CSRF): browsers send Origin (or at least Referer) on form POSTs.
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            host = request.headers.get("host", "")
            if not origin or urlparse(origin).netloc != host:
                return PlainTextResponse("Cross-site or header-less request blocked", status_code=403)
        return await call_next(request)


app.add_middleware(AuthMiddleware)


# --------------------------------------------------------------------------- lifecycle


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


# --------------------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    today = jobs.today_local()
    with session_scope() as s:
        def count(*where):
            return s.scalar(select(func.count(Competition.id)).where(*where)) or 0

        stats = {
            "discovered": count(),
            "queued": count(Competition.status == "queued"),
            "manual": count(Competition.status == "manual"),
            "entered": count(or_(Competition.status == "entered", Competition.entry_count > 0)),
            "won": count(Competition.outcome == "won"),
            "possible": count(Competition.outcome == "possible_win"),
            "lost": count(Competition.outcome == "lost"),
            "pending": count(Competition.outcome == "pending", Competition.status == "entered"),
            "review": count(Competition.needs_review.is_(True)),
            "today": entries_today(s, cfg.tz, get_settings(s).get("entry_mode") != "auto"),
            "by_you": s.scalar(select(func.count(func.distinct(EntryAttempt.competition_id)))
                               .where(EntryAttempt.mode == "manual", EntryAttempt.result == "entered")) or 0,
            "won_value": s.scalar(select(func.sum(Competition.prize_value)).where(Competition.outcome == "won")) or 0,
        }
        decided = stats["won"] + stats["lost"]
        stats["win_rate"] = f"{100 * stats['won'] / decided:.1f}%" if decided else "–"
        since = utcnow() - timedelta(days=29)
        rows = s.execute(select(func.date(EntryAttempt.started_at), func.count(EntryAttempt.id))
                         .where(EntryAttempt.started_at >= since,
                                EntryAttempt.result.in_(["entered", "unverified"]))
                         .group_by(func.date(EntryAttempt.started_at))).all()
        per_day = {str(d): n for d, n in rows}
        days = [(today - timedelta(days=i)) for i in range(29, -1, -1)]
        chart = [(d, per_day.get(d.isoformat(), 0)) for d in days]
        chart_max = max([n for _, n in chart] + [1])
        last_runs = {}
        for name in ("discover", "enter", "inbox", "maintenance"):
            last_runs[name] = s.scalars(select(JobRun).where(JobRun.job == name).order_by(JobRun.id.desc())).first()
        closing = s.scalars(select(Competition).where(Competition.status.in_(["queued", "manual"]),
                                                      Competition.closes_on >= today)
                            .order_by(Competition.closes_on).limit(8)).all()
        possible = s.scalars(select(Competition).where(Competition.outcome == "possible_win")).all()
        events = s.scalars(select(Event).order_by(Event.id.desc()).limit(15)).all()
        methods = s.execute(select(Competition.method, func.count(Competition.id)).group_by(Competition.method)).all()
    next_runs = {}
    if scheduler:
        for j in scheduler.get_jobs():
            next_runs[j.id] = j.next_run_time
    return render(request, "dashboard.html", stats=stats, chart=chart, chart_max=chart_max, last_runs=last_runs,
                  next_runs=next_runs, closing=closing, possible=possible, events=events, methods=methods)


@app.post("/run/{job}")
def run_job(request: Request, job: str):
    if job not in ("discover", "enter", "inbox", "maintenance"):
        raise HTTPException(404)
    started = jobs.run_in_background(job)
    with session_scope() as s:
        log_event(s, "user", f"Manual run requested: {job}")
    return back(request, "/", f"{job.title()} started" if started else f"{job.title()} is already running")


# --------------------------------------------------------------------------- competitions

STATUSES = ["new", "queued", "manual", "entered", "failed", "skipped", "expired"]
OUTCOMES = ["pending", "possible_win", "won", "lost"]
METHODS = ["web_form", "email", "social", "widget", "account", "vote", "magazine", "purchase", "unknown"]


@app.get("/competitions", response_class=HTMLResponse)
def competitions(request: Request, status: str = "", method: str = "", outcome: str = "", q: str = "",
                 sort: str = "posted", review: str = "", page: int = 1):
    page = max(1, page)
    with session_scope() as s:
        stmt = select(Competition)
        if review:
            stmt = stmt.where(Competition.needs_review.is_(True))
        if status:
            stmt = stmt.where(Competition.status == status)
        if method:
            stmt = stmt.where(Competition.method == method)
        if outcome:
            stmt = stmt.where(Competition.outcome == outcome)
        if q:
            stmt = stmt.where(Competition.title.ilike(f"%{q}%"))
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        order = {"posted": [Competition.id.desc()],
                 "closing": [Competition.closes_on.is_(None), Competition.closes_on],
                 "value": [Competition.prize_value.is_(None), Competition.prize_value.desc()],
                 "updated": [Competition.updated_at.desc()]}.get(sort, [Competition.id.desc()])
        rows = s.scalars(stmt.order_by(*order).offset((page - 1) * 50).limit(50)).all()
    pages = max(1, (total + 49) // 50)
    return render(request, "competitions.html", rows=rows, total=total, page=page, pages=pages,
                  f={"status": status, "method": method, "outcome": outcome, "q": q, "sort": sort, "review": review},
                  statuses=STATUSES, outcomes=OUTCOMES, methods=METHODS)


@app.get("/competitions/{cid}", response_class=HTMLResponse)
def competition_detail(request: Request, cid: int):
    with session_scope() as s:
        c = s.get(Competition, cid)
        if not c:
            raise HTTPException(404)
        attempts = list(c.attempts)
        emails = list(c.emails)
        events = s.scalars(select(Event).where(Event.competition_id == cid).order_by(Event.id.desc()).limit(30)).all()
    return render(request, "competition.html", c=c, attempts=attempts, emails=emails, events=events,
                  statuses=STATUSES, outcomes=OUTCOMES, methods=METHODS)


@app.post("/competitions/{cid}/action")
def competition_action(request: Request, cid: int, action: str = Form(...), note: str = Form("")):
    msg = "Updated"
    with session_scope() as s:
        c = s.get(Competition, cid)
        if not c:
            raise HTTPException(404)
        if action == "mark_entered":
            c.status, c.entered_at = "entered", c.entered_at or utcnow()
            c.entry_count += 1
            c.last_attempt_at = c.last_entered_at = utcnow()
            c.needs_review = False
            c.status_reason = "Entered manually"
            s.add(EntryAttempt(competition_id=cid, url=(c.entry_urls or [""])[0], mode="manual", result="entered",
                               message="Marked as entered by you", finished_at=utcnow()))
            msg = "Marked as entered"
        elif action == "confirm_entry":
            c.needs_review = False
            msg = "Thanks - entry confirmed"
        elif action == "won":
            c.outcome, c.won_at, c.outcome_note = "won", utcnow(), note or c.outcome_note
            msg = "Congratulations! Marked as won"
        elif action == "lost":
            c.outcome, c.outcome_note = "lost", note or "Marked lost by you"
            msg = "Marked as lost"
        elif action == "pending":
            c.outcome, c.outcome_note = "pending", ""
        elif action == "skip":
            c.status, c.status_reason = "skipped", "Skipped by you"
            msg = "Skipped"
        elif action == "requeue":
            c.status, c.failed_attempts = "queued", 0
            c.last_attempt_at = None
            c.needs_review = False
            c.status_reason = "Re-queued by you"
            msg = "Re-queued for the next entry run"
        elif action == "manual":
            c.status, c.status_reason = "manual", "Moved to manual queue by you"
        elif action in ("enter_now", "dry_run_now"):
            if c.method not in ("web_form", "email") or (c.method == "web_form" and not c.entry_urls):
                return back(request, f"/competitions/{cid}", "This competition has no automatic entry route")
            msg = None
        else:
            raise HTTPException(400, "Unknown action")
        log_event(s, "user", f"{action}: {c.title[:100]}", competition_id=cid)
    if msg is None:
        started = (jobs.start("enter", _dry_single, cid) if action == "dry_run_now"
                   else jobs.start("enter", jobs._enter, [cid], True))
        if not started:
            return back(request, f"/competitions/{cid}", "An entry run is already in progress - try again shortly")
        msg = "Entry started - refresh in a minute to see the result"
    return back(request, f"/competitions/{cid}", msg)


def _dry_single(cid: int) -> str:
    """Dry-run one competition regardless of the global entry mode."""
    with session_scope() as s:
        settings = dict(get_settings(s))
        profile = get_profile(s)
        c = s.get(Competition, cid)
        urls = list(c.entry_urls or [])
        method = c.method
    settings["entry_mode"] = "dry_run"
    if method == "email":
        return jobs._enter_email(cid, settings, profile, force=True)
    return ", ".join(jobs._enter_web(cid, u, settings, profile) for u in urls[:3])


@app.post("/competitions/{cid}/edit")
def competition_edit(request: Request, cid: int, answers: str = Form(""), entry_urls: str = Form(""),
                     notes: str = Form(""), status: str = Form(""), method: str = Form(""),
                     email_to: str = Form(""), email_subject: str = Form(""), prize_value: str = Form(""),
                     is_daily: str = Form("")):
    with session_scope() as s:
        c = s.get(Competition, cid)
        if not c:
            raise HTTPException(404)
        c.answers = [a.strip() for a in answers.splitlines() if a.strip()]
        c.entry_urls = [u.strip() for u in entry_urls.splitlines() if u.strip().lower().startswith("http")]
        c.notes = notes
        c.email_to = email_to.strip() or None
        c.email_subject = email_subject.strip() or None
        c.is_daily = bool(is_daily)
        try:
            c.prize_value = float(prize_value.replace("£", "").replace(",", "")) if prize_value.strip() else None
        except ValueError:
            pass
        if method in METHODS:
            c.method = method
        if status in STATUSES and status != c.status:
            c.status, c.status_reason = status, f"Set to {status} by you"
            if status == "queued":
                c.failed_attempts, c.last_attempt_at = 0, None
        log_event(s, "user", f"Edited: {c.title[:100]}", competition_id=cid)
    return back(request, f"/competitions/{cid}", "Saved")


@app.post("/manual/requeue-retryable")
def manual_requeue(request: Request):
    if jobs.is_running("enter"):
        return back(request, "/manual", "An entry run is in progress - try again when it finishes")
    return back(request, "/manual", jobs.requeue_retryable())


@app.get("/manual", response_class=HTMLResponse)
def manual_queue(request: Request, method: str = ""):
    today = jobs.today_local()
    with session_scope() as s:
        stmt = select(Competition).where(Competition.status == "manual",
                                         or_(Competition.closes_on.is_(None), Competition.closes_on >= today))
        if method:
            stmt = stmt.where(Competition.method == method)
        rows = s.scalars(stmt.order_by(Competition.closes_on.is_(None), Competition.closes_on).limit(300)).all()
        by_method = s.execute(select(Competition.method, func.count(Competition.id))
                              .where(Competition.status == "manual",
                                     or_(Competition.closes_on.is_(None), Competition.closes_on >= today))
                              .group_by(Competition.method)).all()
        profile = get_profile(s)
        last_msgs = {}
        for c in rows:
            a = c.attempts[0] if c.attempts else None
            last_msgs[c.id] = a
    return render(request, "manual.html", rows=rows, by_method=by_method, method=method, profile=profile,
                  last_msgs=last_msgs)


# --------------------------------------------------------------------------- inbox

EMAIL_CATS = ["possible_win", "loss", "confirm", "entry_ack", "scam_suspect", "promo", "other"]


@app.get("/inbox", response_class=HTMLResponse)
def inbox_view(request: Request, category: str = "possible_win", page: int = 1):
    page = max(1, page)
    with session_scope() as s:
        stmt = select(Email)
        if category:
            stmt = stmt.where(Email.category == category)
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = s.scalars(stmt.order_by(Email.received_at.desc().nulls_last(), Email.id.desc())
                         .offset((page - 1) * 50).limit(50)).all()
        counts = dict(s.execute(select(Email.category, func.count(Email.id)).group_by(Email.category)).all())
        recent = s.scalars(select(Competition).where(or_(Competition.status == "entered", Competition.entry_count > 0))
                           .order_by(Competition.entered_at.desc()).limit(300)).all()
        comps = {c.id: c for c in recent}
        for r in rows:
            if r.competition_id and r.competition_id not in comps:
                comps[r.competition_id] = s.get(Competition, r.competition_id)
    return render(request, "inbox.html", rows=rows, category=category, counts=counts, cats=EMAIL_CATS,
                  comps=comps, recent=recent, page=page, pages=max(1, (total + 49) // 50), total=total)


@app.post("/inbox/{eid}/action")
def inbox_action(request: Request, eid: int, action: str = Form(...), competition_id: str = Form("")):
    with session_scope() as s:
        e = s.get(Email, eid)
        if not e:
            raise HTTPException(404)
        cid = int(competition_id) if competition_id.strip().isdigit() else e.competition_id
        c = s.get(Competition, cid) if cid else None
        if action == "confirm_win":
            e.category, e.reviewed, e.competition_id = "possible_win", True, cid
            if c:
                c.outcome, c.won_at, c.outcome_note = "won", utcnow(), f"Win email: {e.subject[:200]}"
            msg = "Win confirmed" + (f" for {c.title[:60]}" if c else " (link it to a competition to track the prize)")
        elif action == "not_win":
            e.category, e.reviewed = "other", True
            if c and c.outcome == "possible_win":
                c.outcome = "pending"
            msg = "Marked as not a win"
        elif action == "loss":
            e.category, e.reviewed, e.competition_id = "loss", True, cid
            if c:
                c.outcome, c.outcome_note = "lost", f"Email: {e.subject[:200]}"
            msg = "Marked as a loss"
        elif action == "link":
            e.competition_id = cid
            msg = "Linked"
        elif action == "reviewed":
            e.reviewed = True
            msg = "Marked reviewed"
        else:
            raise HTTPException(400)
        log_event(s, "user", f"Inbox {action}: {e.subject[:100]}", competition_id=cid)
    return back(request, "/inbox", msg)


# --------------------------------------------------------------------------- wins / log / settings

@app.get("/wins", response_class=HTMLResponse)
def wins(request: Request):
    with session_scope() as s:
        won = s.scalars(select(Competition).where(Competition.outcome == "won").order_by(Competition.won_at.desc())).all()
        possible = s.scalars(select(Competition).where(Competition.outcome == "possible_win")).all()
        total = sum(c.prize_value or 0 for c in won)
    return render(request, "wins.html", won=won, possible=possible, total=total)


@app.get("/log", response_class=HTMLResponse)
def event_log(request: Request, kind: str = "", level: str = "", page: int = 1):
    page = max(1, page)
    with session_scope() as s:
        stmt = select(Event)
        if kind:
            stmt = stmt.where(Event.kind == kind)
        if level:
            stmt = stmt.where(Event.level == level)
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = s.scalars(stmt.order_by(Event.id.desc()).offset((page - 1) * 100).limit(100)).all()
        runs = s.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(20)).all()
    return render(request, "log.html", rows=rows, kind=kind, level=level, page=page,
                  pages=max(1, (total + 99) // 100), runs=runs)


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request):
    from . import llm

    with session_scope() as s:
        profile = get_profile(s)
        settings = get_settings(s)
        dry_runs = s.scalar(select(func.count(EntryAttempt.id)).where(EntryAttempt.mode == "dry_run")) or 0
    ollama = llm.ollama_status() if llm.ollama_url() else None
    pulls = llm.pull_progress()
    installed = {m["name"] for m in (ollama or {}).get("models", [])}
    return render(request, "settings.html", profile=profile, settings=settings, defaults=DEFAULT_SETTINGS,
                  dry_runs=dry_runs, ollama=ollama, pulls=pulls, recommended=llm.RECOMMENDED_MODELS,
                  installed=installed, active_model=llm.model_name(settings),
                  pulling=any(not p["done"] for p in pulls.values()))


MODEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/]{0,80}(:[a-z0-9._\-]{1,40})?$", re.I)


@app.post("/ai/pull")
def ai_pull(request: Request, model: str = Form(""), custom: str = Form("")):
    import threading

    from . import llm

    name = (custom or model).strip()
    if not MODEL_NAME_RE.match(name):
        return back(request, "/settings", "That doesn't look like an Ollama model name (e.g. gemma4:12b)")
    if not llm.ollama_url():
        return back(request, "/settings", "Ollama isn't configured (OLLAMA_URL)")
    threading.Thread(target=llm.pull_model, args=(name,), daemon=True, name=f"pull-{name}").start()
    with session_scope() as s:
        log_event(s, "user", f"Downloading AI model {name}")
    return back(request, "/settings", f"Downloading {name} - progress shows below; you can leave this page")


@app.post("/ai/delete")
def ai_delete(request: Request, model: str = Form(...)):
    from . import llm

    if not MODEL_NAME_RE.match(model.strip()):
        return back(request, "/settings", "Invalid model name")
    msg = llm.delete_model(model.strip())
    with session_scope() as s:
        log_event(s, "user", f"Removed AI model {model}: {msg}")
    return back(request, "/settings", f"{model}: {msg}")


@app.post("/ai/test")
def ai_test(request: Request):
    from . import llm

    with session_scope() as s:
        settings = get_settings(s)
    if not llm.enabled(settings):
        return back(request, "/settings", "Choose a model first (and save settings)")
    return back(request, "/settings", llm.self_test(settings))


@app.post("/settings/clear-dry-runs")
def clear_dry_runs(request: Request, requeue_manual: str = Form("")):
    if jobs.is_running("enter"):
        return back(request, "/settings", "An entry run is in progress - try again when it finishes")
    msg = jobs.clear_dry_runs(requeue_manual=bool(requeue_manual))
    return back(request, "/settings", msg)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    with session_scope() as s:
        profile = {k: str(form.get(f"p_{k}", "")).strip() for k in DEFAULT_PROFILE}
        save_profile(s, profile)
        current = get_settings(s)
        new = {}
        for k, default in DEFAULT_SETTINGS.items():
            raw = form.get(f"s_{k}")
            if isinstance(default, bool):
                new[k] = raw is not None
            elif isinstance(default, int) and not isinstance(default, bool):
                try:
                    new[k] = int(str(raw).strip())
                except (TypeError, ValueError):
                    new[k] = current.get(k, default)
            else:
                new[k] = str(raw).strip() if raw is not None else current.get(k, default)
        if new.get("entry_mode") not in ("dry_run", "auto"):
            new["entry_mode"] = "dry_run"
        if current.get("entry_mode") != new["entry_mode"]:
            log_event(s, "user", f"Entry mode changed to {new['entry_mode']}", "warn")
        save_settings(s, new)
        log_event(s, "user", "Settings saved")
    return RedirectResponse("/settings?msg=Settings+saved", status_code=303)


# --------------------------------------------------------------------------- live view

def _live_status() -> dict:
    import json as _json
    import time as _time

    d = cfg.live_dir
    status = {}
    try:
        status = _json.loads((d / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        frame_age = round(_time.time() - (d / "frame.jpg").stat().st_mtime, 1)
    except OSError:
        frame_age = None
    return {"running": jobs.is_running("enter"), "progress": jobs.progress("enter"), "phase": status.get("phase"),
            "title": status.get("title"), "url": status.get("url"), "result": status.get("result"),
            "status_age": round(_time.time() - status["updated"], 1) if status.get("updated") else None,
            "frame_age": frame_age}


@app.get("/live", response_class=HTMLResponse)
def live_page(request: Request):
    return render(request, "live.html", status=_live_status(), no_autoreload=True)


@app.get("/live/status")
def live_status():
    return _live_status()


@app.get("/live/frame.jpg")
def live_frame():
    path = cfg.live_dir / "frame.jpg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/live/stream")
async def live_stream(request: Request, max_frames: int = 0):
    """MJPEG stream of the browser compbot is driving (watch-only)."""
    import asyncio
    import time as _time

    path = cfg.live_dir / "frame.jpg"

    async def frames():
        last, sent, started = None, 0, _time.monotonic()
        while _time.monotonic() - started < 600:  # the page reconnects; caps any stream a proxy forgot to close
            if await request.is_disconnected():
                break
            try:
                st = path.stat()
                key = (st.st_mtime_ns, st.st_size)
            except OSError:
                key = None
            if key and key != last:
                try:
                    data = path.read_bytes()
                except OSError:
                    data = b""
                if data[:2] == b"\xff\xd8":
                    last = key
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode()
                           + b"\r\n\r\n" + data + b"\r\n")
                    sent += 1
                    if max_frames and sent >= max_frames:
                        break
            await asyncio.sleep(0.12)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/screenshots/{name}")
def screenshot(name: str):
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".jpg"):
        raise HTTPException(404)
    path = cfg.screenshot_dir / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/export/competitions.csv")
def export_csv():
    cols = ["id", "title", "prize", "prize_value", "closes_on", "method", "status", "status_reason", "outcome",
            "entry_count", "entered_at", "won_at", "thread_url", "entry_urls", "answers", "notes"]

    def cell(v):
        if isinstance(v, list):
            v = " | ".join(str(x) for x in v)
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v  # stop spreadsheet formula injection from public forum text
        return v

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    with session_scope() as s:
        for c in s.scalars(select(Competition).order_by(Competition.id)).all():
            w.writerow([cell(getattr(c, k)) for k in cols])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=competitions.csv"})
