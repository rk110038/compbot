"""Competition Database (competitiondatabase.co.uk): a large, member-run list of UK competitions.

compbot reads its "Simple Form Competitions" list - the web-form comps compbot can enter - newest first. Titles are
public, but the "Enter Here" links are only shown to signed-in members, so compbot signs in with your account
(CDB_USERNAME / CDB_PASSWORD). Its terms allow one account per person, used by that person: compbot only reads the
list as you would, a page every few seconds, and never enters Competition Database's own giveaways (its terms ban
automated entries to those).
"""
from __future__ import annotations

import html
import logging
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx

from ..analyse import registrable_domain
from ..config import get_config
from .common import relative_close

log = logging.getLogger("compbot.cdb")

BASE = "https://www.competitiondatabase.co.uk"
LIST_URL = BASE + "/quick-links/simple-form-competitions/{page}/5/easy-to-enter-free-competitions/"
SORT_RECENTLY_ADDED = "4"
MAX_PAGES = 120
KNOWN_PAGES_TO_STOP = 2  # stop after this many pages in a row with nothing new
PAUSE_S = 2.5
OWN_DOMAIN = "competitiondatabase.co.uk"


class CdbError(Exception):
    pass


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": get_config().user_agent, "Accept": "text/html"}, timeout=30,
                        follow_redirects=True)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", fragment or ""))).strip()


def signed_in(page_html: str) -> bool:
    """A list page as a member: no visitor-only "register to enter" links or "log in to mark entered" prompts."""
    return not any(s in page_html for s in ('id="logInForm"', "/register/from_entry/", "CompNotLoggedIn("))


def _sign_in_tips() -> str:
    cfg = get_config()
    tips = []
    if "@" not in (cfg.cdb_username or ""):
        tips.append("CDB_USERNAME must be the email address you sign in to Competition Database with.")
    tips.append(f"compbot was given a {len(cfg.cdb_password or '')}-character password: if that's not the length of "
                "yours, or it contains a $, put it in single quotes in .env (CDB_PASSWORD='...'; Docker Compose reads "
                "$ as the start of a variable), then recreate the container (docker compose up -d).")
    return " ".join(tips)


def login(client: httpx.Client) -> None:
    cfg = get_config()
    if not cfg.cdb_enabled:
        raise CdbError("Set CDB_USERNAME and CDB_PASSWORD (your Competition Database login) to use it")
    page = client.get(BASE + "/login/")
    page.raise_for_status()
    token = re.search(r'name="user_action_id"[^>]*value="([^"]*)"|value="([^"]*)"[^>]*name="user_action_id"', page.text)
    data = {"user_username": cfg.cdb_username, "user_password": cfg.cdb_password}
    if token:
        data["user_action_id"] = token.group(1) or token.group(2)
    time.sleep(1.0)
    r = client.post(BASE + "/login/", data=data, headers={"Referer": BASE + "/login/"})
    r.raise_for_status()
    refused = re.search(r"Logon Error:\s*(.*?)(?:<br|</div>|$)", r.text, re.S | re.I)
    if refused:
        raise CdbError(f"it didn't accept the sign-in. It said \u201c{_text(refused.group(1))}\u201d. {_sign_in_tips()}")
    time.sleep(1.0)
    check = client.get(LIST_URL.format(page=1))
    if not signed_in(check.text):
        still_form = 'id="logInForm"' in r.text
        raise CdbError("it didn't accept the sign-in" + (
            f" (it showed the sign-in form again). {_sign_in_tips()}" if still_form else
            ": the sign-in went through, but the competition list still shows the visitor view (no entry links). "
            "Tell Scout - the site may have changed."))


def resolve_entry(client: httpx.Client, href: str) -> str | None:
    """The promoter's page behind an Enter Here link. Links into Competition Database itself are followed (without
    leaving it) to find where they point."""
    if not href or href.startswith(("javascript:", "#", "mailto:")):
        return None
    url = urljoin(BASE + "/", html.unescape(href))
    for _ in range(4):
        if registrable_domain(url) != OWN_DOMAIN:
            return url
        if "/register/" in url or "/login" in url:
            return None
        time.sleep(1.0)
        r = client.get(url, follow_redirects=False)
        loc = r.headers.get("location")
        if loc:
            url = urljoin(url, loc)
            continue
        m = re.search(r'http-equiv="refresh"[^>]*url=([^"\'>]+)|window\.location(?:\.href)?\s*=\s*["\']([^"\']+)', r.text,
                      re.I)
        if not m:
            return None
        url = urljoin(url, html.unescape(m.group(1) or m.group(2)))
    return url if registrable_domain(url) != OWN_DOMAIN else None


def parse_cards(page_html: str) -> list[dict]:
    """The competitions on one list page. entry_href is the raw Enter Here link (resolved later)."""
    out = []
    for chunk in page_html.split('class="comp_slot"')[1:]:
        head = re.search(r'competition_panel_heading"><a href="(/view/([0-9a-f]+)/[^"]*)">(.*?)</a>', chunk, re.S)
        if not head:
            continue
        path, cid, full = head.group(1), head.group(2), _text(head.group(3))
        company, _, prize = full.partition(" - ")
        if not prize:
            company, prize = "", full
        closes_txt = re.search(r'<div style="font-size:0\.8em;">(.*?)</div>', chunk, re.S)
        closes_on, precise = relative_close(_text(closes_txt.group(1)) if closes_txt else "", tz=get_config().tz)
        enter = re.search(r'<a[^>]*href="([^"]+)"[^>]*>\s*Enter Here\s*</a>', chunk, re.S)
        out.append({
            "source": "cdb",
            "source_id": f"cdb:{cid}",
            "url": BASE + path,
            "title": prize.strip(),
            "company": company.strip(),
            "closes_on": closes_on,
            "closes_precise": precise,
            "posted_at": None,
            "entry_urls": [],
            "entry_href": enter.group(1) if enter else "",
            "promoter_domain": None,
            "description": "",
            "question": None,
            "answers": [],
            "tags": [],
            "notes": [],
            "manual_link": None,
            "own_giveaway": "competition database" in company.lower(),
        })
    return out


def _sorted_newest_first(client: httpx.Client) -> str:
    r = client.post(LIST_URL.format(page=1), data={"filter_sort": SORT_RECENTLY_ADDED},
                    headers={"Referer": LIST_URL.format(page=1)})
    r.raise_for_status()
    return r.text


def _finish(client: httpx.Client, card: dict) -> dict | None:
    """Resolve the entry link; None for Competition Database's own giveaways."""
    if card.pop("own_giveaway", False):
        return None
    href = card.pop("entry_href", "")
    url = resolve_entry(client, href)
    if url and registrable_domain(url) == OWN_DOMAIN:
        return None
    if url:
        card["entry_urls"] = [url]
        card["promoter_domain"] = registrable_domain(url)
    return card


def fetch_listings(known: set[str], should_stop=lambda: False) -> list[dict]:
    """Competitions added since compbot last looked (newest first), with their entry links."""
    new: list[dict] = []
    seen: set[str] = set()
    with _client() as client:
        login(client)
        page_html = _sorted_newest_first(client)
        quiet_pages = 0
        for page in range(1, MAX_PAGES + 1):
            if should_stop():
                break
            if page > 1:
                time.sleep(PAUSE_S)
                r = client.get(LIST_URL.format(page=page))
                r.raise_for_status()
                page_html = r.text
            if page == 1 and not signed_in(page_html):
                raise CdbError("Signed in, but Competition Database still shows the list as a visitor")
            cards = parse_cards(page_html)
            if not cards:
                break
            # a comp added while compbot reads pushes older ones down a page, so one can appear twice
            fresh = [c for c in cards if c["source_id"] not in known and c["source_id"] not in seen
                     and not c.get("own_giveaway")]
            seen.update(c["source_id"] for c in cards)
            quiet_pages = 0 if fresh else quiet_pages + 1
            for c in fresh:
                done = _finish(client, c)
                if done:
                    new.append(done)
            if quiet_pages >= KNOWN_PAGES_TO_STOP:
                break
            if f"/simple-form-competitions/{page + 1}/" not in page_html:
                break
    log.info("Competition Database: %d new", len(new))
    return new


def self_test() -> tuple[bool, str]:
    """Settings page check: sign in and read the first page of the list."""
    if not get_config().cdb_enabled:
        return False, ("Not set up: add CDB_USERNAME and CDB_PASSWORD (your Competition Database login) to compbot's "
                       "environment, then restart it. See the README.")
    try:
        with _client() as client:
            login(client)
            page_html = _sorted_newest_first(client)
            total = re.search(r"of <b>(\d+)</b>", page_html)
            cards = [c for c in parse_cards(page_html) if not c.get("own_giveaway")]
            links = [_finish(client, dict(c)) for c in cards]
    except (CdbError, httpx.HTTPError) as exc:
        return False, f"Competition Database: {exc}"
    with_link = [c for c in links if c and c["entry_urls"]]
    example = f", e.g. {urlsplit(with_link[0]['entry_urls'][0]).netloc}" if with_link else ""
    ok = bool(with_link) or not cards
    return ok, (f"Signed in to Competition Database. {total.group(1) if total else '?'} simple-form competitions "
                f"listed; the newest {len(cards)} had {len(with_link)} entry link{'s' if len(with_link) != 1 else ''}"
                f"{example}." + ("" if ok else " No entry links found: the site may have changed - tell Scout."))
