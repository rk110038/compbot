"""Environment configuration (secrets and infrastructure) plus defaults for GUI-editable settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    data_dir: Path
    tz: str
    web_user: str
    web_password: str | None
    gmail_address: str | None
    gmail_app_password: str | None
    imap_host: str
    imap_folders: list[str]
    smtp_host: str
    smtp_port: int
    llm_base_url: str | None
    llm_api_key: str | None
    llm_model: str | None
    ollama_url: str | None
    notify_email: str | None
    ntfy_url: str | None
    discover_cron: str
    enter_cron: str
    inbox_cron: str
    maintenance_cron: str
    mse_category_id: int
    user_agent: str
    headless: bool
    scheduler_enabled: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "compbot.db"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def live_dir(self) -> Path:
        """Where the live browser view is written: shared memory, so streaming never touches the NAS's disks."""
        env = _env("LIVE_DIR")
        if env:
            return Path(env)
        shm = Path("/dev/shm")
        if shm.is_dir():
            return shm / "compbot-live"
        import tempfile
        return Path(tempfile.gettempdir()) / "compbot-live"

    @property
    def llm_enabled(self) -> bool:
        """An endpoint is configured (the model itself may be chosen in the GUI)."""
        return bool(self.llm_base_url)

    @property
    def mail_enabled(self) -> bool:
        return bool(self.gmail_address and self.gmail_app_password)


@lru_cache(maxsize=1)
def get_config() -> Config:
    ollama_url = (_env("OLLAMA_URL") or "").rstrip("/") or None
    return Config(
        data_dir=Path(_env("DATA_DIR", "/data")),
        tz=_env("TZ", "Europe/London"),
        web_user=_env("WEB_USER", "admin"),
        web_password=_env("WEB_PASSWORD"),
        gmail_address=_env("GMAIL_ADDRESS"),
        gmail_app_password=(_env("GMAIL_APP_PASSWORD") or "").replace(" ", "") or None,
        imap_host=_env("IMAP_HOST", "imap.gmail.com"),
        imap_folders=[f.strip() for f in _env("IMAP_FOLDERS", "INBOX,[Gmail]/Spam").split(",") if f.strip()],
        smtp_host=_env("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(_env("SMTP_PORT", "465")),
        llm_base_url=_env("LLM_BASE_URL") or (f"{ollama_url}/v1" if ollama_url else None),
        llm_api_key=_env("LLM_API_KEY"),
        llm_model=_env("LLM_MODEL"),
        ollama_url=ollama_url,
        notify_email=_env("NOTIFY_EMAIL"),
        ntfy_url=_env("NTFY_URL"),
        discover_cron=_env("DISCOVER_CRON", "0 7 * * *"),
        enter_cron=_env("ENTER_CRON", "30 7 * * *"),
        inbox_cron=_env("INBOX_CRON", "0 8,20 * * *"),
        maintenance_cron=_env("MAINTENANCE_CRON", "0 3 * * *"),
        mse_category_id=int(_env("MSE_CATEGORY_ID", "72")),
        user_agent=_env("USER_AGENT", "compbot/1.0 (personal competition tracker; daily check)"),
        headless=_bool(_env("HEADLESS"), True),
        scheduler_enabled=_bool(_env("SCHEDULER_ENABLED"), True),
    )


# Behaviour settings editable in the GUI (stored in the database).
DEFAULT_SETTINGS: dict = {
    "entry_mode": "dry_run",            # dry_run = fill forms + screenshot, never submit; auto = submit
    "daily_entry_cap": 25,               # max automated entries per day (web + email)
    "max_attempts": 2,                   # failed attempts before a comp moves to the manual queue
    "enter_email_comps": True,           # send email entries automatically
    "respect_automation_clauses": True,  # send comps whose T&Cs ban automated entries to the manual queue
    "auto_confirm_links": True,          # click 'confirm your entry/subscription' links for comps we entered
    "marketing_opt_in": False,           # tick optional marketing boxes on non-newsletter comps
    "enter_all_md_links": True,          # (MD) threads: enter on every magazine site listed
    "social_handling": "skip",           # skip | manual  (Instagram/Facebook/X/TikTok comps)
    "widget_handling": "manual",         # skip | manual  (Gleam/Rafflecopter/etc.)
    "presumed_lost_days": 60,            # days after closing with no win email before marking lost
    "backfill_days": 14,                 # on first run, import threads posted in the last N days
    "llm_for_posts": True,               # use the LLM (if configured) to analyse unclear posts
    "llm_for_forms": True,               # use the LLM (if configured) for fields heuristics can't map
    "llm_for_email": False,              # send candidate win emails (subject + snippet) to the LLM
    "llm_model": "",                     # model chosen in Settings (blank = LLM_MODEL from the environment)
    "ai_answers": True,                  # let the AI answer competition questions the MSE post didn't answer
    "ai_min_confidence": 80,             # only use AI answers it is at least this sure of (0-100)
    "ai_double_check": True,             # a second AI pass must not judge the answer wrong before it's used
    "ai_tiebreakers": False,             # let the AI write short tie-breaker / "tell us why" answers
    "live_view": True,                   # stream the browser to the Live page while entering
    "delay_min_seconds": 20,
    "delay_max_seconds": 60,
    "skip_keywords": "",                 # comma-separated; titles containing any are skipped
    "min_prize_value": 0,                # skip comps whose stated prize value is below this (0 = off)
}

DEFAULT_PROFILE: dict = {
    "title": "",
    "first_name": "",
    "last_name": "",
    "email": "",
    "phone": "",
    "dob": "",          # YYYY-MM-DD
    "gender": "",
    "address1": "",
    "address2": "",
    "town": "",
    "county": "",
    "postcode": "",
    "country": "United Kingdom",
    "twitter": "",
    "instagram": "",
}
