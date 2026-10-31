"""MoneySavingExpert 'Competitions Time' source, via the forum's public Vanilla API (/api/v2 is allowed by robots.txt)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..config import get_config

log = logging.getLogger("compbot.mse")

API = "https://forums.moneysavingexpert.com/api/v2/discussions"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def fetch_threads(known_ids: set[str], since: datetime, max_pages: int = 15, page_size: int = 100) -> list[dict]:
    """Return new threads (newest first) posted after `since`, stopping once we reach known/older threads."""
    cfg = get_config()
    out: list[dict] = []
    headers = {"User-Agent": cfg.user_agent, "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            params = {
                "categoryID": cfg.mse_category_id,
                "limit": page_size,
                "page": page,
                "sort": "-dateInserted",
                "expand": "body",
            }
            r = client.get(API, params=params)
            r.raise_for_status()
            items = r.json()
            if not items:
                break
            stop = False
            for it in items:
                sid = str(it.get("discussionID"))
                posted = _parse_dt(it.get("dateInserted"))
                if posted and posted < since:
                    stop = True
                    break
                if sid in known_ids or it.get("closed"):
                    continue
                out.append({
                    "source_id": sid,
                    "title": (it.get("name") or "").strip(),
                    "url": it.get("canonicalUrl") or it.get("url") or "",
                    "body": it.get("body") or "",
                    "posted_at": posted,
                    "comments": it.get("countComments") or 0,
                })
            if stop or len(items) < page_size:
                break
            time.sleep(1.5)  # be polite
    log.info("MSE: %d new threads", len(out))
    return out


def default_since(last_run: datetime | None, backfill_days: int) -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if last_run:
        return last_run - timedelta(days=2)  # overlap in case of edits/moves
    return now - timedelta(days=max(1, backfill_days))
