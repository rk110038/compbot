"""SQLite database: models, session handling, settings/profile storage and the event log."""
from __future__ import annotations

import copy
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import DEFAULT_PROFILE, DEFAULT_SETTINGS, get_config


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Competition(Base):
    __tablename__ = "competitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(20), default="mse")
    source_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    thread_url: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    prize: Mapped[str] = mapped_column(String(500), default="")
    prize_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    closes_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    closes_time: Mapped[str | None] = mapped_column(String(30), nullable=True)
    entry_urls: Mapped[list] = mapped_column(JSON, default=list)
    other_links: Mapped[list] = mapped_column(JSON, default=list)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    answers: Mapped[list] = mapped_column(JSON, default=list)
    method: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    email_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email_subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_daily: Mapped[bool] = mapped_column(Boolean, default=False)
    is_multi: Mapped[bool] = mapped_column(Boolean, default=False)
    # new | queued | manual | entered | failed | skipped | expired
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    status_reason: Mapped[str] = mapped_column(Text, default="")
    # pending | possible_win | won | lost
    outcome: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    outcome_note: Mapped[str] = mapped_column(Text, default="")
    promoter_domain: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    entered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_entered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    entered_urls: Mapped[list] = mapped_column(JSON, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    won_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    attempts: Mapped[list["EntryAttempt"]] = relationship(
        back_populates="competition", cascade="all, delete-orphan", order_by="EntryAttempt.id.desc()"
    )
    emails: Mapped[list["Email"]] = relationship(back_populates="competition", order_by="Email.id.desc()")

    @property
    def closes_label(self) -> str:
        if not self.closes_on:
            return "?"
        label = self.closes_on.strftime("%d %b %Y")
        return f"{label} {self.closes_time}" if self.closes_time else label

    @property
    def answer_text(self) -> str:
        return " | ".join(self.answers or [])


class EntryAttempt(Base):
    __tablename__ = "entry_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competition_id: Mapped[int] = mapped_column(ForeignKey("competitions.id"), index=True)
    url: Mapped[str] = mapped_column(String(1000), default="")
    mode: Mapped[str] = mapped_column(String(20), default="auto")  # auto | dry_run | email | manual
    # entered | unverified | dry_run_ok | failed | captcha | blocked | tc_blocked | needs_input | error | manual
    result: Mapped[str] = mapped_column(String(20), default="error", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    screenshot_before: Mapped[str | None] = mapped_column(String(300), nullable=True)
    screenshot_after: Mapped[str | None] = mapped_column(String(300), nullable=True)
    final_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    competition: Mapped[Competition] = relationship(back_populates="attempts")


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    folder: Mapped[str] = mapped_column(String(100), default="INBOX")
    from_addr: Mapped[str] = mapped_column(String(300), default="")
    from_name: Mapped[str] = mapped_column(String(300), default="")
    from_domain: Mapped[str] = mapped_column(String(200), default="", index=True)
    subject: Mapped[str] = mapped_column(String(1000), default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    snippet: Mapped[str] = mapped_column(Text, default="")
    # possible_win | loss | confirm | entry_ack | promo | scam_suspect | other
    category: Mapped[str] = mapped_column(String(20), default="other", index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    signals: Mapped[list] = mapped_column(JSON, default=list)
    competition_id: Mapped[int | None] = mapped_column(ForeignKey("competitions.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(Text, default="")
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    competition: Mapped[Competition | None] = relationship(back_populates="emails")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | success | warn | error
    kind: Mapped[str] = mapped_column(String(20), default="system")  # discovery | entry | inbox | system | user
    message: Mapped[str] = mapped_column(Text, default="")
    competition_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(30), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | error
    summary: Mapped[str] = mapped_column(Text, default="")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


_engine = None
_SessionLocal: sessionmaker | None = None


def init_db(url: str | None = None) -> None:
    global _engine, _SessionLocal
    cfg = get_config()
    if url is None:
        try:
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            cfg.screenshot_dir.mkdir(parents=True, exist_ok=True)
            probe = cfg.data_dir / ".write-test"
            probe.touch()
            probe.unlink()
        except PermissionError as exc:
            uid, gid = getattr(os, "getuid", lambda: "?")(), getattr(os, "getgid", lambda: "?")()
            raise RuntimeError(
                f"{cfg.data_dir} is not writable by this container (running as uid {uid}, gid {gid}). "
                f"On the host, give that user the data folder, e.g.: sudo chown -R {uid}:{gid} /path/to/data"
            ) from exc
        url = f"sqlite:///{cfg.db_path.as_posix()}"
    _engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def _add_missing_columns(engine) -> None:
    """Tiny forward-only migration: add any model columns missing from an older database."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(dialect=engine.dialect)
                default = ""
                if isinstance(col.type, Boolean):
                    default = " DEFAULT 0"
                elif isinstance(col.type, Integer):
                    default = " DEFAULT 0"
                elif isinstance(col.type, JSON):
                    default = " DEFAULT '[]'"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}{default}'))


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        init_db()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _get_blob(s: Session, key: str, defaults: dict) -> dict:
    row = s.get(Setting, key)
    merged = copy.deepcopy(defaults)
    if row and isinstance(row.value, dict):
        merged.update(row.value)
    return merged


def _save_blob(s: Session, key: str, values: dict) -> None:
    row = s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=dict(values)))
    else:
        row.value = dict(values)


def get_settings(s: Session) -> dict:
    return _get_blob(s, "settings", DEFAULT_SETTINGS)


def save_settings(s: Session, values: dict) -> None:
    _save_blob(s, "settings", values)


def get_profile(s: Session) -> dict:
    profile = _get_blob(s, "profile", DEFAULT_PROFILE)
    if not profile.get("email"):
        profile["email"] = get_config().gmail_address or ""
    return profile


def save_profile(s: Session, values: dict) -> None:
    _save_blob(s, "profile", values)


def get_state(s: Session, key: str, default=None):
    row = s.get(Setting, f"state:{key}")
    return row.value.get("v", default) if row else default


def set_state(s: Session, key: str, value) -> None:
    _save_blob(s, f"state:{key}", {"v": value})


def log_event(s: Session, kind: str, message: str, level: str = "info", competition_id: int | None = None) -> None:
    import logging

    logging.getLogger("compbot").log(
        {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO), "[%s] %s", kind, message
    )
    s.add(Event(kind=kind, message=message[:4000], level=level, competition_id=competition_id))


def entries_today(s: Session, tz_name: str) -> int:
    """Count successful automated entries (or dry runs) since local midnight."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    local_midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    since = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    rows = s.scalars(
        select(EntryAttempt.id).where(
            EntryAttempt.started_at >= since,
            EntryAttempt.mode.in_(["auto", "email", "dry_run"]),
            EntryAttempt.result.in_(["entered", "unverified", "dry_run_ok"]),
        )
    ).all()
    return len(rows)
