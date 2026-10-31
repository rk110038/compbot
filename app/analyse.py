"""Parse MSE competition threads (title + first post) and decide how each comp should be entered."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString

try:
    import tldextract

    _extract = tldextract.TLDExtract(suffix_list_urls=())  # offline: use the bundled suffix list
except Exception:  # pragma: no cover
    _extract = None


# --------------------------------------------------------------------------- helpers

def registrable_domain(url_or_host: str | None) -> str:
    if not url_or_host:
        return ""
    host = url_or_host
    if "://" in url_or_host or url_or_host.startswith("//"):
        host = urlparse(url_or_host).hostname or ""
    elif "@" in url_or_host:
        host = url_or_host.rsplit("@", 1)[1]
    host = host.lower().strip(". ")
    if _extract:
        ext = _extract(host)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr", "pre", "section"]


def html_to_text(html: str) -> str:
    """Convert post HTML to text keeping inline elements on the same line."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_after(NavigableString("\n"))
    text = soup.get_text("")
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


URL_RE = re.compile(r"https?://[^\s<>\"'\]\[)]+", re.I)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)

EXCLUDE_DOMAINS = {"moneysavingexpert.com", "v-cdn.net", "vanillicon.com", "vanillaforums.com", "google.com/maps"}
SOCIAL_DOMAINS = {
    "instagram.com", "facebook.com", "fb.com", "fb.me", "fb.watch", "twitter.com", "x.com", "tiktok.com",
    "threads.net", "threads.com", "youtube.com", "youtu.be", "linkedin.com", "pinterest.com", "pinterest.co.uk",
    "bsky.app", "t.co", "snapchat.com",
}
WIDGET_DOMAINS = {
    "gleam.io", "rafflecopter.com", "sweepwidget.com", "viralsweep.com", "kingsumo.com", "woobox.com",
    "wishpond.com", "shortstack.com", "shortstack.page", "upviral.com", "promosimple.com", "giveawaytools.com",
    "vyper.io", "easypromosapp.com", "binkies3d.com",
}
APP_DOMAINS = {"apple.com", "google.com"}
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf")
COMP_URL_HINT = re.compile(r"win|comp|competition|giveaway|prize|enter|draw|promo|sweep|contest|quiz|form", re.I)
NON_ENTRY_URL_HINT = re.compile(r"terms|conditions|privacy|\bt-?and-?c|tandc|\bt-?cs?\b|/rules\b|policy|cookie", re.I)
IMAGE_HOSTS = {"ibb.co", "imgur.com", "postimg.cc", "postimages.org", "prnt.sc", "gyazo.com", "imgbb.com",
               "tinypic.com", "photobucket.com", "flickr.com"}


def link_kind(url: str) -> str:
    dom = registrable_domain(url)
    low = url.lower()
    if not dom or any(ex in low for ex in ("moneysavingexpert.com", "v-cdn.net")) or dom in EXCLUDE_DOMAINS:
        return "exclude"
    if dom in IMAGE_HOSTS or urlparse(low).path.endswith(IMAGE_EXT):
        return "exclude"
    if dom in SOCIAL_DOMAINS:
        return "social"
    if dom in WIDGET_DOMAINS:
        return "widget"
    if "apps.apple.com" in low or "play.google.com" in low:
        return "app"
    return "web"


# --------------------------------------------------------------------------- title parsing

TITLE_DATE_RE = re.compile(r"^\s*E\s*[:;.]?\s*(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?\s*\??", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}(?:[.:]\d{2})?\s*(?:am|pm)|noon|midday|midnight)\b", re.I)
PAREN_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
TAG_ALIASES = {
    "ig": "ig", "insta": "ig", "instagram": "ig",
    "fb": "fb", "facebook": "fb",
    "x": "x", "twitter": "x", "tw": "x",
    "tiktok": "tiktok", "tt": "tiktok",
    "nl": "nl", "newsletter": "nl", "sign up": "nl", "signup": "nl",
    "gleam": "gleam", "daily": "daily", "email": "email", "e-mail": "email",
    "md": "md", "vote": "vote", "quiz": "quiz", "survey": "survey", "app": "app",
}
PRIZE_VALUE_RE = re.compile(r"£\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d{1,2})?)\s*(k)?", re.I)


def infer_date(day: int, month: int, year: str | None, ref: date) -> date | None:
    try:
        if year:
            y = int(year)
            y = y + 2000 if y < 100 else y
            return date(y, month, day)
        cand = date(ref.year, month, day)
        if cand < ref - timedelta(days=60):
            cand = date(ref.year + 1, month, day)
        return cand
    except ValueError:
        return None


def parse_title(title: str, ref: date) -> dict:
    closes_on = None
    rest = title or ""
    m = TITLE_DATE_RE.match(rest)
    if m:
        closes_on = infer_date(int(m.group(1)), int(m.group(2)), m.group(3), ref)
        rest = rest[m.end():]
    tags: list[str] = []
    notes: list[str] = []
    for pm in PAREN_RE.finditer(rest):
        raw = (pm.group(1) or pm.group(2) or "").strip()
        if not raw:
            continue
        if TIME_RE.fullmatch(raw.strip(" -")) or re.match(r"^starts?\b", raw, re.I):
            notes.append(raw)
            continue
        for tok in re.split(r"\s*[/+,&]\s*|\s+-\s+|\s+and\s+", raw.lower()):
            tok = tok.strip(" .!-#")
            if tok:
                tags.append(TAG_ALIASES.get(tok, tok))
    closes_time = None
    tm = TIME_RE.search(rest)
    if tm:
        closes_time = tm.group(1).lower().replace(".", ":")
    prize = PAREN_RE.sub(" ", rest)
    prize = TIME_RE.sub(" ", prize) if tm and rest.find(tm.group(0)) < 12 else prize
    prize = re.sub(r"^[\s\-–:?]+", "", prize)
    prize = re.sub(r"\s{2,}", " ", prize).strip(" -–")
    values = []
    for vm in PRIZE_VALUE_RE.finditer(title or ""):
        v = float(vm.group(1).replace(",", ""))
        if vm.group(2):
            v *= 1000
        values.append(v)
    return {
        "closes_on": closes_on,
        "closes_time": closes_time,
        "tags": list(dict.fromkeys(tags)),
        "notes": notes,
        "prize": prize or (title or "").strip(),
        "prize_value": max(values) if values else None,
    }


# --------------------------------------------------------------------------- body parsing

ANSWER_LINE_RE = re.compile(
    r"^(?:my\s+|the\s+)?(?:answers?|ans|a)\s*\d*\s*(?:is|=|:|-|–|—)\s*(.*)$", re.I
)
INLINE_ANSWER_RE = re.compile(r"\b(?:my\s+)?answer\s*(?:is|=|:)\s*([^\n]+)", re.I)
QA_SAME_LINE_RE = re.compile(r"^(?:q(?:uestion)?\s*\d*\s*[:.)-]\s*)?(?:\d+\s*[.)]\s*)?(.{8,}?\?)\s+(?:a(?:nswer)?\s*[:=-]\s*)?(.{1,120})$", re.I)
QUESTION_RE = re.compile(r"^(?:q(?:uestion)?\s*\d*\s*[:.)-]\s*)(.+)$", re.I)
SUBJECT_RE = re.compile(r"\bsubject(?:\s+line)?\s*(?:[:=-]|\bof\b)\s*(.*)$", re.I)
UNKNOWN_ANSWER_RE = re.compile(r"^[?\s.]*$|^(?:unknown|not sure|tbc|\?+)$", re.I)
NEEDS_HELP_RE = re.compile(r"help please|can anyone (?:help|find)|anyone know the answer|answer (?:not|un)known|\?{3,}", re.I)


def _clean_answer(val: str) -> str:
    val = val.strip().strip("\"'“”‘’*_ ")
    # Drop trailing commentary in brackets, e.g. "7 (Cats on pages 2, 3 ...)"
    val = re.sub(r"\s*\((?:[^()]{12,})\)\s*$", "", val).strip()
    val = re.sub(r"\s*\((?:i think|i suppose|probably|maybe)[^)]*\)?\s*$", "", val, flags=re.I).strip()
    return val.strip(" .\"'“”")


def parse_body(body_html: str) -> dict:
    text = html_to_text(body_html)
    soup = BeautifulSoup(body_html or "", "html.parser")
    raw_links: list[str] = []
    for a in soup.find_all("a", href=True):
        raw_links.append(a["href"].strip())
    for u in URL_RE.findall(text):
        raw_links.append(u.rstrip(").,;:!…"))
    links = list(dict.fromkeys(raw_links))

    emails = []
    for h in links:
        if h.lower().startswith("mailto:"):
            emails.append(h[7:].split("?")[0].strip())
    emails += EMAIL_RE.findall(text)
    emails = [e for e in dict.fromkeys(e.lower() for e in emails) if "moneysavingexpert" not in e]
    links = [h for h in links if not h.lower().startswith("mailto:")]

    lines = text.splitlines()
    answers: list[str] = []
    questions: list[str] = []
    subject = None
    for i, line in enumerate(lines):
        nxt = next((lines[j] for j in range(i + 1, min(i + 3, len(lines))) if lines[j].strip()), "")
        sm = SUBJECT_RE.search(line)
        if sm and subject is None:
            subject = _clean_answer(sm.group(1) or nxt) or None
            continue
        am = ANSWER_LINE_RE.match(line)
        if am:
            val = am.group(1).strip() or nxt
            val = _clean_answer(val)
            if val and not UNKNOWN_ANSWER_RE.match(val) and not URL_RE.match(val):
                answers.append(val)
            if i > 0 and lines[i - 1].rstrip().endswith("?") and not questions:
                questions.append(lines[i - 1].strip())
            continue
        im = INLINE_ANSWER_RE.search(line)
        if im:
            val = _clean_answer(im.group(1))
            if val and not UNKNOWN_ANSWER_RE.match(val):
                answers.append(val)
            continue
        qm = QUESTION_RE.match(line)
        if qm:
            questions.append(qm.group(1).strip())
            continue
        qa = QA_SAME_LINE_RE.match(line)
        if qa and re.match(r"^\s*(?:q\s*\d*|\d+)\s*[.):]", line, re.I):
            questions.append(qa.group(1).strip())
            ans = _clean_answer(qa.group(2))
            if ans and not UNKNOWN_ANSWER_RE.match(ans):
                answers.append(ans)
    answers = list(dict.fromkeys(a for a in answers if len(a) <= 200))
    needs_help = bool(NEEDS_HELP_RE.search(text)) and not answers
    return {
        "text": text,
        "links": links,
        "emails": emails,
        "answers": answers,
        "question": " | ".join(dict.fromkeys(questions)) or None,
        "subject": subject,
        "needs_help": needs_help,
    }


# --------------------------------------------------------------------------- classification

SOCIAL_TAGS = {"ig", "fb", "x", "tiktok", "threads", "youtube", "yt", "bluesky", "linkedin", "pinterest", "snapchat"}
WIDGET_TAGS = {"gleam", "rafflecopter", "sweepwidget", "viralsweep", "kingsumo", "woobox", "wishpond", "upviral",
               "promosimple", "shortstack"}
ACCOUNT_HINTS = ("my john lewis", "waterstones plus", "reach plc", "login", "log in", "account", "members",
                 "membership", "customers", "nectar", "clubcard", "priority", "app")
PURCHASE_HINTS = ("purchase", "receipt", "proof of purchase", "pop", "buy", "on pack", "on-pack", "spend")
MAGAZINE_RE = re.compile(
    r"\b(issue\s*\d+|that'?s life|take a break|chat issue|bella issue|tv times|what'?s on tv|tv choice|"
    r"love it magazine|puzzle|crossword|wordsearch|xword|wordfinder)\b", re.I)
DAILY_RE = re.compile(r"\bdaily\b|1 entry (?:each|per|a) day|enter (?:every|each) day|once (?:a|per|each) day", re.I)
EMAIL_ENTRY_RE = re.compile(r"\b(?:send|email|e-mail)\b[^.\n]{0,80}\bto\b|\bto enter[^.\n]{0,40}\bemail\b", re.I)
NEEDS_ANSWERS_RE = re.compile(r"\banswers?\b|\bquiz\b|crossword|\bxword\b|puzzle|wordfinder|wordsearch|\bquestion\b", re.I)


@dataclass
class Analysis:
    closes_on: date | None
    closes_time: str | None
    tags: list[str]
    prize: str
    prize_value: float | None
    text: str
    entry_urls: list[str]
    other_links: list[str]
    answers: list[str]
    question: str | None
    email_to: str | None
    email_subject: str | None
    is_daily: bool
    is_multi: bool
    method: str
    status: str
    reason: str
    promoter_domain: str | None
    flags: list[str] = field(default_factory=list)


def analyse(title: str, body_html: str, settings: dict, today: date | None = None) -> Analysis:
    today = today or date.today()
    t = parse_title(title, today)
    b = parse_body(body_html)
    tags = set(t["tags"])
    title_low = (title or "").lower()
    text_low = b["text"].lower()

    kinds: dict[str, list[str]] = {"web": [], "social": [], "widget": [], "app": []}
    for link in b["links"]:
        k = link_kind(link)
        if k in kinds:
            kinds[k].append(link)
    # Drop T&C/privacy links, then prefer URLs that look like competition pages (stable sort keeps post order).
    web = [u for u in kinds["web"] if not NON_ENTRY_URL_HINT.search(urlparse(u).path)]
    web.sort(key=lambda u: 0 if COMP_URL_HINT.search(urlparse(u).netloc + urlparse(u).path) else 1)
    is_multi = "md" in tags and len(web) > 1
    entry_urls = web if is_multi and settings.get("enter_all_md_links", True) else web[:1]
    # A social comp (IG/FB/X) whose only web link is a homepage or shop page isn't a web form entry.
    if tags & SOCIAL_TAGS and "nl" not in tags and entry_urls and not any(
            COMP_URL_HINT.search(urlparse(u).netloc + urlparse(u).path) for u in entry_urls):
        entry_urls = []
    other_links = [u for u in b["links"] if u not in entry_urls and link_kind(u) != "exclude"]
    is_daily = "daily" in tags or bool(DAILY_RE.search(title or ""))
    promoter = registrable_domain(entry_urls[0]) if entry_urls else (
        registrable_domain(b["emails"][0]) if b["emails"] else None)

    email_to = None
    if b["emails"] and ("email" in tags or EMAIL_ENTRY_RE.search(b["text"])):
        email_to = b["emails"][0]

    flags: list[str] = []
    method, status, reason = "unknown", "manual", "Couldn't work out how to enter"

    def manual_or_skip(handling_key: str, m: str, why: str):
        handling = settings.get(handling_key, "manual")
        return m, ("skipped" if handling == "skip" else "manual"), why

    skip_kw = [k.strip().lower() for k in str(settings.get("skip_keywords", "")).split(",") if k.strip()]
    min_value = float(settings.get("min_prize_value") or 0)

    if t["closes_on"] and t["closes_on"] < today:
        method, status, reason = ("web_form" if entry_urls else "unknown"), "expired", "Closing date has passed"
    elif any(h in tags for h in PURCHASE_HINTS) or re.search(r"\b(purchase necessary|proof of purchase|receipt)\b", title_low):
        method, status, reason = "purchase", "skipped", "Purchase required"
    elif any(k in title_low for k in skip_kw):
        method, status, reason = "web_form" if entry_urls else "unknown", "skipped", "Title matches a skip keyword"
    elif min_value and t["prize_value"] is not None and t["prize_value"] < min_value:
        method, status, reason = "web_form" if entry_urls else "unknown", "skipped", f"Prize value below £{min_value:g}"
    elif "email" in tags or (email_to and not entry_urls):
        if not email_to:
            method, status, reason = "email", "manual", "Email entry but no address found in the post"
        elif not b["answers"] and (b["question"] or b["needs_help"] or NEEDS_ANSWERS_RE.search(b["text"] + " " + title)):
            method, status, reason = "email", "manual", "Email entry needs answers that aren't in the post"
        elif settings.get("enter_email_comps", True):
            method, status, reason = "email", "queued", f"Email entry to {email_to}"
        else:
            method, status, reason = "email", "manual", "Email entries are switched off in settings"
    elif any(any(h == tg or h in tg for h in ACCOUNT_HINTS) for tg in tags):
        method, status, reason = "account", "manual", "Needs an account, app or membership"
    elif tags & {"vote", "survey"}:
        method, status, reason = "vote", "manual", "Needs a vote/survey choice"
    elif MAGAZINE_RE.search(title or "") and "win" not in title_low:
        method, status, reason = "magazine", "manual", "Magazine thread with several puzzle comps"
    elif not entry_urls and (kinds["widget"] or tags & WIDGET_TAGS):
        method, status, reason = manual_or_skip("widget_handling", "widget", "Gleam/widget giveaway (needs a login)")
    elif not entry_urls and (kinds["social"] or tags & SOCIAL_TAGS):
        method, status, reason = manual_or_skip("social_handling", "social", "Social media entry")
    elif not entry_urls and kinds["app"]:
        method, status, reason = "account", "manual", "App-only competition"
    elif entry_urls:
        method = "web_form"
        if b["needs_help"] or (b["question"] and not b["answers"]):
            status, reason = "manual", "Needs an answer that isn't in the post yet"
        else:
            status, reason = "queued", "Web form"
        if tags & WIDGET_TAGS or kinds["widget"]:
            flags.append("Also has a Gleam/widget entry route (manual)")
        if tags & SOCIAL_TAGS or kinds["social"]:
            flags.append("Also has a social media entry route (manual)")

    if status in ("queued", "manual") and t["closes_on"] is None and "e:" not in title_low[:3]:
        flags.append("No closing date in title")
    if flags and status == "queued":
        reason = reason + "; " + "; ".join(flags)

    return Analysis(
        closes_on=t["closes_on"],
        closes_time=t["closes_time"],
        tags=t["tags"],
        prize=t["prize"],
        prize_value=t["prize_value"],
        text=b["text"],
        entry_urls=entry_urls,
        other_links=other_links,
        answers=b["answers"],
        question=b["question"],
        email_to=email_to,
        email_subject=b["subject"],
        is_daily=is_daily,
        is_multi=is_multi,
        method=method,
        status=status,
        reason=reason,
        promoter_domain=promoter,
        flags=flags,
    )


def apply_analysis(comp, a: Analysis) -> None:
    """Copy an Analysis onto a Competition row (doesn't touch outcome/history)."""
    comp.closes_on = a.closes_on
    comp.closes_time = a.closes_time
    comp.tags = a.tags
    comp.prize = a.prize[:500]
    comp.prize_value = a.prize_value
    comp.body_text = a.text[:20000]
    comp.entry_urls = a.entry_urls
    comp.other_links = a.other_links
    comp.answers = a.answers
    comp.question = a.question
    comp.email_to = a.email_to
    comp.email_subject = a.email_subject
    comp.is_daily = a.is_daily
    comp.is_multi = a.is_multi
    comp.method = a.method
    comp.status = a.status
    comp.status_reason = a.reason
    comp.promoter_domain = a.promoter_domain
