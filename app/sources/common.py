"""What every competition source hands to discovery, and how a listing is turned into an MSE-style post so the same
analysis (closing date, tags, answers, entry method, skip rules) applies to every site.

A listing is a dict:
  source, source_id   "ct" / "cdb" / "mse" and an id unique across all sources ("ct:89161", "cdb:<hash>")
  url                 the listing page, for you to open
  title               the competition as the site names it
  company             who runs it, when the site says (Competition Database: "Magic Radio")
  closes_on           date or None; closes_precise False when the site only says "in 2 months"
  posted_at           when the site added it (datetime) or None
  entry_urls          the promoter's entry page(s), when the site gives them to compbot
  promoter_domain     the promoter's website ("sheerluxe.com"), when known
  description         what the site says about the prize (plain text)
  question, answers   the competition question and suggested answer(s), when given
  tags                compbot tags: "nl" (marketing/newsletter sign-up), "account" (must register first)
  notes               extra lines for the post ("Details required: Name, Email")
  manual_link         a link for you (not compbot) to reach the entry page, e.g. a site's own "Enter" redirect
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

URL_IN_TEXT_RE = re.compile(r"https?://\S+", re.I)


def as_post(listing: dict, today: date | None = None) -> tuple[str, str]:
    """(title, body_html) in the shape MSE threads have, e.g. "E: 04/11 Win £500 Dissh Vouchers (NL)". Dates more
    than ~200 days away carry their year, which a bare day/month would be misread without (as this year's, closed)."""
    name = re.sub(r"\s+", " ", listing.get("title") or "").strip()
    if not re.match(r"(?i)win\b", name):
        name = f"Win {name}"
    if listing.get("company"):
        name += f" - {listing['company'].strip()}"
    closes: date | None = listing.get("closes_on")
    today = today or datetime.now(ZoneInfo("Europe/London")).date()
    fmt = "%d/%m/%Y" if closes and abs((closes - today).days) > 200 else "%d/%m"
    prefix = f"E: {closes:{fmt}}{'' if listing.get('closes_precise', True) else '?'} " if closes else ""
    tags = "".join(f" ({'NL' if t == 'nl' else t})" for t in listing.get("tags") or [])
    title = f"{prefix}{name}{tags}"

    parts = []
    desc = URL_IN_TEXT_RE.sub("", listing.get("description") or "").strip()
    if desc:
        parts += [f"<p>{html.escape(line)}</p>" for line in desc.splitlines() if line.strip()]
    for note in listing.get("notes") or []:
        parts.append(f"<p>{html.escape(note)}</p>")
    if listing.get("question"):
        parts.append(f"<p>Question: {html.escape(listing['question'])}</p>")
    for a in listing.get("answers") or []:
        parts.append(f"<p>Answer: {html.escape(a)}</p>")
    for u in listing.get("entry_urls") or []:
        parts.append(f'<p>Enter here: <a href="{html.escape(u, quote=True)}">{html.escape(u)}</a></p>')
    return title, "\n".join(parts)


UNIT_DAYS = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30.44, "year": 365.25}
RELATIVE_RE = re.compile(r"(\d+)\s*(minute|hour|day|week|month|year)s?", re.I)


def relative_close(text: str, now: datetime | None = None, tz: str = "Europe/London") -> tuple[date | None, bool]:
    """'closes in 24 days and 8 hours' -> (that date, precise). Months and years are only approximate."""
    low = (text or "").lower()
    now = now or datetime.now(ZoneInfo(tz))
    if re.search(r"\b(?:ends|closes)\s+today\b", low):
        return now.date(), True
    if re.search(r"\b(?:has )?(?:ended|closed)\b", low) and "in " not in low:
        return now.date() - timedelta(days=1), True
    parts = RELATIVE_RE.findall(low)
    if not parts:
        return None, False
    days = sum(int(n) * UNIT_DAYS[u.lower()] for n, u in parts)
    precise = not any(u.lower() in ("month", "year") for _, u in parts)
    return (now + timedelta(days=days)).date(), precise


def ordinal_date(text: str) -> date | None:
    """'4th Nov 2026' / '24th December 2026' -> date."""
    s = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", (text or "").strip())
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
