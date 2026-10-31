"""Gmail inbox checker: classify incoming mail (wins, losses, confirmations, spam) and match it to competitions."""
from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup

from .analyse import registrable_domain

log = logging.getLogger("compbot.inbox")

WIN_RES = [re.compile(p, re.I) for p in [
    r"\bcongratulations?\b", r"\bcongrats\b", r"\byou(?:'ve| have)? (?:just )?won\b", r"\byou won\b",
    r"\byou(?:'re| are) (?:a|our|the|one of (?:our|the)) (?:lucky )?winners?\b",
    r"\bselected as (?:a|the|one of (?:the|our)) (?:lucky )?winners?\b", r"\bwinner of (?:our|the|this)\b",
    r"\bclaim your prize\b", r"\bprize (?:notification|confirmation|claim|delivery|winner)\b", r"\byour prize\b",
    r"\b(?:delighted|pleased|happy|thrilled) to (?:tell|inform|let) you\b", r"\bwinning entry\b",
    r"\byou(?:'ve| have) been (?:drawn|picked|chosen|selected)\b", r"\bdrawn as (?:a|the) winner\b",
]]
PROMO_RES = [re.compile(p, re.I) for p in [
    r"\bchances? to win\b", r"\bcould win\b", r"\benter (?:now|today|here)\b", r"\b\d+% off\b", r"\bshop now\b",
    r"\blimited time\b", r"\bfree delivery\b", r"\bbuy now\b", r"\bsale\b", r"\bview in (?:your )?browser\b",
    r"\bnew arrivals\b", r"\bwin (?:a|an|one|£)\b", r"\bdeal of the\b",
]]
LOSS_RES = [re.compile(p, re.I) for p in [
    r"\bunfortunately\b[^.]{0,80}\b(?:not|wasn'?t|weren'?t|haven'?t|didn'?t)\b",
    r"\bnot (?:been )?(?:successful|selected|chosen|a winner)\b", r"\bbetter luck next time\b",
    r"\bweren'?t (?:a )?(?:lucky )?winner", r"\bwasn'?t (?:a )?winner",
]]
CONFIRM_RES = [re.compile(p, re.I) for p in [
    r"confirm your (?:e-?mail|subscription|entry|address|sign.?up|registration)", r"verify your (?:e-?mail|entry|address)",
    r"please confirm\b", r"activate your", r"one (?:more|last) step", r"double opt.?in",
    r"click (?:the|this) (?:link|button)(?: below)? to (?:confirm|verify|complete|validate)",
    r"confirm (?:that )?you(?:'d| would)? (?:want|like) to", r"complete your (?:entry|registration|sign.?up)",
]]
ACK_RES = [re.compile(p, re.I) for p in [
    r"thank(?:s| you) for (?:entering|your entry|taking part|participating)", r"entry (?:has been )?(?:received|confirmed)",
    r"you(?:'re| are) (?:now )?(?:entered|in the (?:prize )?draw)", r"\bwelcome to\b", r"thanks for (?:signing up|subscribing)",
]]
SCAM_RES = [re.compile(p, re.I) for p in [
    r"bank details", r"processing fee", r"(?:delivery|admin|release|clearance) fee", r"pay (?:a|the) (?:small )?fee",
    r"\bcrypto|bitcoin", r"wire transfer|western union|moneygram", r"claim within (?:24|48|72) hours",
    r"gift card code to claim", r"\blottery\b", r"\binheritance\b", r"verify your (?:account|identity) to claim",
    r"card details", r"\bsurvey\b[^.]{0,40}\breward\b",
]]
CONFIRM_LINK_RE = re.compile(
    r"confirm|verify|activate|validate|complete (?:your )?(?:entry|registration|sign.?up)|yes,? (?:subscribe|sign me up)|"
    r"opt.?in", re.I)
STOP = {"win", "winner", "worth", "prize", "with", "from", "your", "the", "and", "for", "plus", "of", "a", "an", "to",
        "in", "on", "at", "or", "x", "uk", "gift", "card", "voucher", "vouchers", "tickets", "ticket", "night", "two",
        "one", "three", "copy", "bundle", "set", "pair", "free", "year", "years", "new", "our"}


@dataclass
class ParsedMail:
    message_id: str
    folder: str
    from_addr: str
    from_name: str
    from_domain: str
    subject: str
    received_at: datetime | None
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Classification:
    category: str
    score: float
    signals: list[str]
    competition_id: int | None = None
    confirm_link: str | None = None


def _dh(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def parse_message(raw: bytes, folder: str) -> ParsedMail:
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_dh(msg.get("From")))
    try:
        received = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        received = None
    text_parts, html_parts = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if ctype == "text/plain":
            text_parts.append(decoded)
        elif ctype == "text/html":
            html_parts.append(decoded)
    links: list[tuple[str, str]] = []
    if html_parts:
        soup = BeautifulSoup("\n".join(html_parts), "html.parser")
        for t in soup(["style", "script", "head"]):
            t.decompose()
        links = [(a["href"], a.get_text(" ", strip=True)) for a in soup.find_all("a", href=True)]
        html_text = soup.get_text(" ", strip=True)
    else:
        html_text = ""
    text = "\n".join(text_parts) or html_text
    if not links:
        links = [(u, "") for u in re.findall(r"https?://\S+", text)]
    return ParsedMail(
        message_id=(msg.get("Message-ID") or f"<no-id-{hashlib.sha256(raw).hexdigest()[:32]}>").strip(),
        folder=folder, from_addr=addr.lower(), from_name=name, from_domain=registrable_domain(addr),
        subject=_dh(msg.get("Subject")).strip(), received_at=received,
        text=re.sub(r"\s+", " ", text).strip(), links=links,
    )


def _prize_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9£']{3,}", (text or "").lower()) if w not in STOP}


def match_competition(m: ParsedMail, comps: list) -> tuple[object | None, float]:
    """comps: Competition rows the user has entered. Returns best (comp, score)."""
    hay = (m.subject + " " + m.text[:4000]).lower()
    hay_tokens = set(re.findall(r"[a-z0-9£']{3,}", hay))
    best, best_score = None, 0.0
    for c in comps:
        score = 0.0
        domains = {registrable_domain(u) for u in (c.entry_urls or [])} | {c.promoter_domain or ""}
        if c.email_to:
            domains.add(registrable_domain(c.email_to))
        domains.discard("")
        if m.from_domain and m.from_domain in domains:
            score += 3
        overlap = _prize_tokens(c.prize or c.title) & hay_tokens
        score += min(len(overlap) * 0.6, 3)
        if score > best_score:
            best, best_score = c, score
    return (best, best_score) if best_score >= 2.4 else (None, best_score)


def classify(m: ParsedMail, comps: list) -> Classification:
    subject, body = m.subject, m.text[:6000]
    signals: list[str] = []

    def hits(res, text):
        return [r.pattern for r in res if r.search(text)]

    win_s, win_b = hits(WIN_RES, subject), hits(WIN_RES, body)
    promo = hits(PROMO_RES, subject + " " + body[:1500])
    loss = hits(LOSS_RES, subject + " " + body)
    conf = hits(CONFIRM_RES, subject + " " + body[:3000])
    ack = hits(ACK_RES, subject + " " + body[:2000])
    scam = hits(SCAM_RES, subject + " " + body)
    comp, match_score = match_competition(m, comps)

    win_score = 2.5 * len(win_s) + 1.5 * len(win_b) - 1.0 * len(promo) - 3 * len(loss)
    if comp:
        win_score += 2
        signals.append(f"matches comp #{comp.id} (score {match_score:.1f})")
    signals += [f"win:{p}" for p in win_s + win_b][:6] + [f"promo:{p}" for p in promo][:3]
    signals += [f"loss:{p}" for p in loss][:3] + [f"scam:{p}" for p in scam][:3]

    confirm_link = None
    if conf:
        for href, label in m.links:
            if CONFIRM_LINK_RE.search(label or "") or CONFIRM_LINK_RE.search(href.split("?")[0]):
                if href.lower().startswith("http") and "unsubscribe" not in href.lower():
                    confirm_link = href
                    break

    if loss and len(win_s) == 0:
        cat = "loss"
    elif win_score >= 4 and scam:
        cat = "scam_suspect"
    elif win_score >= 4 or (comp and win_s and win_score >= 3):
        cat = "possible_win"
    elif conf:
        cat = "confirm"
    elif ack:
        cat = "entry_ack"
    elif scam and (win_s or win_b):
        cat = "scam_suspect"
    elif promo or re.search(r"unsubscribe", body[-1500:], re.I):
        cat = "promo"
    else:
        cat = "other"
    return Classification(cat, round(win_score, 2), signals, comp.id if comp else None, confirm_link)


# --------------------------------------------------------------------------- IMAP

def _imap_date(d: datetime) -> str:
    return d.strftime("%d-%b-%Y")


LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')
SPAM_ALIASES = {"spam", "junk", "\\junk", "[gmail]/spam", "[google mail]/spam"}
ALL_ALIASES = {"all mail", "\\all", "[gmail]/all mail", "[google mail]/all mail"}


def resolve_folders(list_lines: list, folders: list[str]) -> list[str]:
    """Map Spam/All Mail aliases to the account's real special-use folders (Gmail UK uses '[Google Mail]/...')."""
    special: dict[str, str] = {}
    for raw in list_lines or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        m = LIST_RE.match(line.strip())
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"')
        for flag in m.group("flags").split():
            special.setdefault(flag.lower(), name)
    out = []
    for f in folders:
        key = f.strip().lower()
        if key in SPAM_ALIASES:
            out.append(special.get("\\junk", f))
        elif key in ALL_ALIASES:
            out.append(special.get("\\all", f))
        else:
            out.append(f)
    return list(dict.fromkeys(out))


def fetch_messages(host: str, user: str, password: str, folders: list[str], since: datetime,
                   known_ids: set[str], max_total: int = 500) -> tuple[list[ParsedMail], bool]:
    """Fetch unseen messages oldest-first. Returns (messages, truncated) - truncated means more are waiting."""
    out: list[ParsedMail] = []
    truncated = False
    imap = imaplib.IMAP4_SSL(host)
    try:
        imap.login(user, password)
        try:
            typ, listing = imap.list()
            folders = resolve_folders(listing if typ == "OK" else [], folders)
        except Exception as exc:  # noqa: BLE001
            log.warning("IMAP LIST failed, using folders as configured: %s", exc)
        for folder in folders:
            typ, _ = imap.select(f'"{folder}"', readonly=True)
            if typ != "OK":
                log.warning("Cannot open folder %s", folder)
                continue
            typ, data = imap.uid("search", None, "SINCE", _imap_date(since))
            if typ != "OK":
                continue
            uids = sorted((data[0] or b"").split(), key=int)
            if not uids:
                continue
            # Headers first (cheap) so bodies are only downloaded for Message-IDs we haven't stored yet.
            new_uids = []
            for chunk in [uids[i:i + 200] for i in range(0, len(uids), 200)]:
                typ, resp = imap.uid("fetch", b",".join(chunk), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)] RFC822.SIZE)")
                if typ != "OK":
                    continue
                for item in resp:
                    if not isinstance(item, tuple):
                        continue
                    meta, hdr = item
                    uid_m = re.search(rb"UID (\d+)", meta)
                    size_m = re.search(rb"RFC822\.SIZE (\d+)", meta)
                    mid = email.message_from_bytes(hdr).get("Message-ID", "").strip()
                    if uid_m and (not mid or mid not in known_ids) and (not size_m or int(size_m.group(1)) < 3_000_000):
                        new_uids.append(uid_m.group(1))
            new_uids.sort(key=int)
            room = max_total - len(out)
            if len(new_uids) > room:
                new_uids, truncated = new_uids[:room], True
            for uid in new_uids:
                typ, resp = imap.uid("fetch", uid, "(BODY.PEEK[])")
                if typ == "OK" and resp and isinstance(resp[0], tuple):
                    try:
                        out.append(parse_message(resp[0][1], folder))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("parse failed: %s", exc)
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return out, truncated


def default_since(last: datetime | None) -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (last - timedelta(days=1)) if last else now - timedelta(days=14)
