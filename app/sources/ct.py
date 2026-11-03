"""Competitions-Time.co.uk: a hand-checked list of free UK competitions (about 120 open at a time).

Its pages are public (robots.txt allows them) and compbot reads them a page at a time, politely. The site's "Enter
Competition" buttons go through /redir/, which robots.txt asks bots not to follow, so compbot never does: most of its
comps are also on MSE (with the entry link), and for the rest discovery looks the promoter's page up with web search,
or leaves the comp in your manual queue with the site's own Enter link for you to click.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime

import httpx

from ..analyse import registrable_domain
from ..config import get_config
from .common import ordinal_date

log = logging.getLogger("compbot.ct")

BASE = "https://www.competitions-time.co.uk"
MAX_PAGES = 15
PAUSE_S = 2.0


def _text(fragment: str) -> str:
    s = re.sub(r"(?is)<br\s*/?>|</p>", "\n", fragment or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    lines = [re.sub(r"[ \t\u00a0]+", " ", html.unescape(x)).strip() for x in s.splitlines()]
    return "\n".join(x for x in lines if x)


def _labelled(card: str, label: str) -> str | None:
    m = re.search(rf'cc2-label[^"]*">\s*{re.escape(label)}\s*</div>\s*<span[^>]*>(.*?)</span>', card, re.S | re.I)
    return _text(m.group(1)) or None if m else None


CARD_RE = re.compile(r'<div\b[^>]*\bclass="cc2-card\b[^"]*"[^>]*>', re.I)


def parse_cards(page_html: str) -> list[dict]:
    """The competitions on one Competitions-Time listing page."""
    out = []
    starts = list(CARD_RE.finditer(page_html))
    for i, m in enumerate(starts):
        head = m.group(0)  # the card's opening tag: data-entry-options and the anchor id
        chunk = page_html[m.end():starts[i + 1].start() if i + 1 < len(starts) else len(page_html)]
        cid = re.search(r'href="/redir/(\d+)"', chunk) or re.search(r'data-entry-options-toggle="(\d+)"', chunk)
        title = re.search(r'cc2-title[^"]*">(.*?)</h3>', chunk, re.S)
        if not cid or not title:
            continue
        cid = cid.group(1)
        opts = re.search(r'data-entry-options="([^"]*)"', head)
        opts = set(filter(None, re.split(r"[\s,]+", opts.group(1)))) if opts else set()
        slug = re.search(r'\sid="([^"]+)"', head)
        slug = slug.group(1) if slug else ""
        website = re.search(r'class="cc2-website[^"]*"[^>]*>\s*([^<]+?)\s*<', chunk)
        website = website.group(1).strip().lower() if website else ""
        closes = re.search(r'cc2-date-text">\s*([^<]+?)\s*<', chunk)
        added = re.search(r'cc2-added[^>]*>\s*(?:<[^>]+>\s*)*Added\s+([^<]+?)\s*<', chunk, re.S)
        desc = re.search(r'cc2-quote-body">(.*?)</div>', chunk, re.S)
        pills = [_text(p) for p in re.findall(r'<span class="cc2-pill">(.*?)</span>', chunk, re.S)]
        category = re.search(r'href="/([a-z-]+-comps)/[^"]*"', chunk)
        question = _labelled(chunk, "Question:")
        answer = _labelled(chunk, "Suggested Answer:")
        tags = (["nl"] if "marketing" in opts else []) + (["account"] if "registration" in opts else [])
        if website:
            page = f"{BASE}/comps-from/{website.replace('.', '_')}"
        elif category:
            page = f"{BASE}/{category.group(1)}"
        else:
            page = f"{BASE}/"
        posted = ordinal_date(added.group(1)) if added else None
        out.append({
            "source": "ct",
            "source_id": f"ct:{cid}",
            "url": f"{page}#{slug}" if slug else page,
            "title": _text(title.group(1)),
            "company": "",
            "closes_on": ordinal_date(closes.group(1)) if closes else None,
            "closes_precise": True,
            "posted_at": datetime.combine(posted, datetime.min.time()) if posted else None,
            "entry_urls": [],
            "promoter_domain": registrable_domain(f"https://{website}") if website else None,
            "description": _text(desc.group(1)) if desc else "",
            "question": question,
            "answers": [answer] if answer else [],
            "tags": tags,
            "notes": [f"Details required: {', '.join(p for p in pills if p)}"] if pills else [],
            "manual_link": f"{BASE}/redir/{cid}",
        })
    return out


def fetch_listings(known: set[str], should_stop=lambda: False) -> list[dict]:
    """Every open competition on the site that compbot hasn't seen, oldest-closing first."""
    cfg = get_config()
    found: dict[str, dict] = {}
    headers = {"User-Agent": cfg.user_agent, "Accept": "text/html"}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for page in range(1, MAX_PAGES + 1):
            if should_stop():
                break
            r = client.get(BASE + ("/" if page == 1 else f"/page/{page}"))
            if r.status_code == 404:
                break
            r.raise_for_status()
            cards = parse_cards(r.text)
            if not cards:
                break
            for c in cards:
                found.setdefault(c["source_id"], c)
            if f'href="/page/{page + 1}"' not in r.text:
                break
            time.sleep(PAUSE_S)
    new = [c for c in found.values() if c["source_id"] not in known]
    log.info("Competitions-Time: %d open, %d new", len(found), len(new))
    return new
