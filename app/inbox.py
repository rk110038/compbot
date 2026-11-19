"""Gmail inbox checker: classify incoming mail (wins, losses, confirmations, spam) and match it to competitions."""
from __future__ import annotations

import email
import hashlib
import heapq
import imaplib
import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from types import SimpleNamespace
from urllib.parse import quote

from bs4 import BeautifulSoup

from .analyse import registrable_domain
from .dedup import money_amounts, title_tokens

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
# compbot's own alerts carry this header. Earlier ones are known by a "[compbot]" subject sent from your own address.
# Neither is ever read back in as incoming mail: an alert quotes the win email, so it looked like a win (or, with its
# "never give bank details" line, a scam) itself, and could set off an alert about the alert.
OWN_HEADER = "X-Compbot"
OWN_SUBJECT_RE = re.compile(r"^\s*\[compbot\]", re.I)


def is_own_alert(from_addr: str, subject: str, tagged: bool = False, own_addr: str | None = None) -> bool:
    if tagged:
        return True
    own = (own_addr or "").strip().lower()
    return bool(own and (from_addr or "").strip().lower() == own and OWN_SUBJECT_RE.match(subject or ""))


def _own_alert_headers(head, own_addr: str | None) -> bool:
    """is_own_alert from a message's headers alone (before its body is downloaded). Never stops the fetch."""
    try:
        return is_own_alert(parseaddr(str(_dh(head.get("From"))))[1], str(_dh(head.get("Subject"))).strip(),
                            bool(head.get(OWN_HEADER)), own_addr)
    except Exception:  # noqa: BLE001
        return False


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
    own: bool = False  # one of compbot's own alerts


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


def parse_message(raw: bytes, folder: str, own_addr: str | None = None) -> ParsedMail:
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_dh(msg.get("From")))
    subject = _dh(msg.get("Subject")).strip()
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
        subject=subject, received_at=received,
        text=re.sub(r"\s+", " ", text).strip(), links=links,
        own=is_own_alert(addr, subject, bool(msg.get(OWN_HEADER)), own_addr),
    )


def _comp_domains(c) -> set[str]:
    domains = {registrable_domain(u) for u in (c.entry_urls or [])} | {c.promoter_domain or ""}
    if c.email_to:
        domains.add(registrable_domain(c.email_to))
    domains.discard("")
    return domains


def match_competition(m: ParsedMail, comps: list, index: "CompIndex | None" = None) -> tuple[object | None, float]:
    """The competition you entered that a newly arrived email is about, to link it to: the likeliest of your entries
    from the sender's own site (not a free email address), as before; with none, compbot's guess when it's a clear
    favourite (the one the Inbox would tick for you). Returns (comp or None, score).

    The sender's own site comes first because its emails often advertise its other prizes ("Thanks for entering our
    family competition... more chances to win an air fryer"): a strong match on one of those is still not the entry
    this confirmation is for. The email's date doesn't rule an entry out here: it has only just arrived, and you may
    press "I've entered it" after the confirmation email comes."""
    g = (index if index is not None else CompIndex(comps)).rank(m.subject, m.text, m.from_domain, m.from_name,
                                                                 None, limit=1)
    pick = g.from_site or (g.guesses[0] if g.confident else None)
    if pick:
        return pick.comp, pick.score
    return None, (g.guesses[0].score if g.guesses else 0.0)


def _found(res: list[re.Pattern], *texts: str) -> list[str]:
    """The words each pattern matched, as the email writes them (not the pattern), without repeats."""
    out: list[str] = []
    for r in res:
        for t in texts:
            hit = r.search(t or "")
            if hit:
                p = re.sub(r"\s+", " ", hit.group(0)).strip()
                if p and p.lower() not in {o.lower() for o in out}:
                    out.append(p)
                break
    return out


def classify(m: ParsedMail, comps: list, index: "CompIndex | None" = None) -> Classification:
    """index: comps already indexed (once per Gmail check, rather than once per email)."""
    subject, body = m.subject, m.text[:6000]
    signals: list[str] = []

    def hits(res, text):
        # One per pattern that matched (the score counts patterns), recorded as the words the email used.
        return [h.group(0) for r in res if (h := r.search(text))]

    def said(kind: str, found: list[str], n: int) -> list[str]:
        out: list[str] = []
        for p in found:
            p = re.sub(r"\s+", " ", p).strip()
            if p.lower() not in {o.lower() for o in out}:
                out.append(p)
        return [f"{kind}:{p}" for p in out[:n]]

    win_s, win_b = hits(WIN_RES, subject), hits(WIN_RES, body)
    promo = hits(PROMO_RES, subject + " " + body[:1500])
    loss = hits(LOSS_RES, subject + " " + body)
    conf = hits(CONFIRM_RES, subject + " " + body[:3000])
    ack = hits(ACK_RES, subject + " " + body[:2000])
    scam = hits(SCAM_RES, subject + " " + body)
    comp, match_score = match_competition(m, comps, index)

    win_score = 2.5 * len(win_s) + 1.5 * len(win_b) - 1.0 * len(promo) - 3 * len(loss)
    if comp:
        win_score += 2
        signals.append(f"matches comp #{comp.id} (score {match_score:.1f})")
    signals += said("win", win_s + win_b, 6) + said("promo", promo, 3)
    signals += said("loss", loss, 3) + said("scam", scam, 3)

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


# --------------------------------------------------------------------------- explaining an email

@dataclass
class Explanation:
    """Why an email was filed where it was, in the email's own words (for the Inbox and the win alert)."""
    why: list[str] = field(default_factory=list)    # "it says “Congratulations”, “you've won”"
    match: list[str] = field(default_factory=list)  # why it looks like the competition it's linked to
    quote_before: str = ""                          # the sentence that says it, split around the words found
    quote_hit: str = ""
    quote_after: str = ""

    @property
    def quote(self) -> str:
        return f"{self.quote_before}{self.quote_hit}{self.quote_after}"


_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+|\s+\|\s+")


def _quoted(phrases: list[str], n: int = 4) -> str:
    return ", ".join(f"\u201c{p}\u201d" for p in phrases[:n])


def _sentence(text: str, phrases: list[str], width: int = 260) -> tuple[str, str, str]:
    """(before, words, after): the sentence of the email that says it, trimmed to about `width` characters."""
    low = text.lower()
    for p in phrases:
        i = low.find(p.lower())
        if i < 0:
            continue
        j = i + len(p)
        start = 0
        for b in _BOUNDARY_RE.finditer(text, 0, i):
            start = b.end()
        b = _BOUNDARY_RE.search(text, j)
        end = b.start() if b else len(text)
        if b and end - start < 60:  # "Hi Rob, congratulations!" says little on its own: take the next sentence too
            b2 = _BOUNDARY_RE.search(text, b.end())
            end = b2.start() if b2 else len(text)
        before, after = text[start:i], text[j:end]
        half = width // 2
        if len(before) > half:
            before = "\u2026" + before[-half:].split(" ", 1)[-1]
        if len(after) > half:
            after = after[:half].rsplit(" ", 1)[0] + "\u2026"
        return before, text[i:j], after
    return "", "", ""


def explain(subject: str, text: str, from_domain: str, comp=None, category: str = "",
            signals: list | None = None) -> Explanation:
    """What the email says that made compbot file it as `category`, and why it looks like `comp`."""
    subject, text = subject or "", text or ""
    wins, losses = _found(WIN_RES, subject, text), _found(LOSS_RES, subject, text)
    scams, promos = _found(SCAM_RES, subject, text), _found(PROMO_RES, subject, text[:1500])
    confirms, acks = _found(CONFIRM_RES, subject, text), _found(ACK_RES, subject, text)
    ex = Explanation()
    if category == "loss":
        if losses:
            ex.why.append(f"it says {_quoted(losses)}")
        quote_from = losses
    elif category == "confirm":
        if confirms:
            ex.why.append(f"it asks you to confirm ({_quoted(confirms)})")
        quote_from = confirms
    elif category == "entry_ack":
        if acks:
            ex.why.append(f"it thanks you for entering ({_quoted(acks)})")
        quote_from = acks
    elif category == "promo":
        if promos:
            ex.why.append(f"it reads like marketing ({_quoted(promos)})")
        quote_from = []
    else:
        if scams and category == "scam_suspect":
            ex.why.append(f"it mentions {_quoted(scams)}, which are scam warning signs")
        if wins:
            ex.why.append(f"it says {_quoted(wins)}")
        if scams and category != "scam_suspect":
            ex.why.append(f"but it also mentions {_quoted(scams)}, which are scam warning signs")
        if promos and category in ("possible_win", "won"):
            ex.why.append(f"it also reads a bit like marketing ({_quoted(promos, 2)})")
        quote_from = scams + wins if category == "scam_suspect" else wins + scams + losses
    for sig in signals or []:
        if isinstance(sig, str) and sig.startswith("llm:"):
            reason = re.search(r"\((.*)\)\s*$", sig)
            ex.why.append(f"the AI check says: {reason.group(1)}" if reason and reason.group(1).strip()
                          else f"the AI check filed it as {sig[4:].split(' ')[0].replace('_', ' ')}")
    ex.quote_before, ex.quote_hit, ex.quote_after = _sentence(text, quote_from)
    if comp is not None:
        doms = _comp_domains(comp) - FREEMAIL
        brand = sorted(d for d in doms if _same_brand(_site_label(from_domain), _site_label(d))) if from_domain else []
        if from_domain and from_domain in doms:
            ex.match.append(f"it's from the competition's site ({from_domain})")
        elif from_domain not in FREEMAIL and brand:
            ex.match.append(f"the sender, {from_domain}, looks like the competition's site ({brand[0]})")
        hay = set(re.findall(r"[a-z0-9£']{3,}", (subject + " " + text[:4000]).lower()))
        words = [w for w in dict.fromkeys(re.findall(r"[a-z0-9£']{3,}", (comp.prize or comp.title or "").lower()))
                 if w not in STOP and w in hay]
        if words:
            ex.match.append(f"both mention {_quoted(words, 5)}")
    return ex


# --------------------------------------------------------------------------- guessing the competition

# Anyone can send from these, so a shared one says nothing about which competition an email is about.
FREEMAIL = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "live.co.uk",
            "msn.com", "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "aol.com", "aol.co.uk", "btinternet.com",
            "sky.com", "virginmedia.com", "protonmail.com", "proton.me", "gmx.com", "mail.com"}
# Site names too general to say two sites belong to the same brand ("mail" is in "dailymail", "comps" in "ukcomps").
GENERIC_LABELS = {"mail", "email", "news", "info", "comp", "comps", "competition", "competitions", "prize", "prizes",
                  "winner", "winners", "shop", "store", "online", "service", "services", "group", "official",
                  "media", "marketing", "promo", "promos", "promotions", "offers", "rewards", "club", "home"}
GUESS_MIN = 1.5               # a weaker guess isn't worth showing
SURE_MIN, SURE_LEAD = 4.0, 1.5  # ticked for you only when this strong and this far ahead of the next one
# An entry first made this long after an email's date can't be what it's about. Not less: you may press "I've
# entered it" in the manual queue days after entering, and some senders date an email when they wrote it.
ENTERED_AFTER = timedelta(days=7)


@dataclass
class Guess:
    comp: object
    score: float
    reasons: list[str]


@dataclass
class Guesses:
    guesses: list[Guess] = field(default_factory=list)
    confident: bool = False  # the first is a clear favourite
    pool: int = 0            # how many competitions you entered were considered
    tick: bool = False       # the Inbox ticks the favourite for you (set by the page: it depends on the email)
    from_site: Guess | None = None  # the likeliest of your entries from the sender's own site, if any


def _plain(text: str) -> str:
    """Lower case without accents or apostrophes ("Café Nolan’s" -> "cafe nolans"), for typing to find a comp."""
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in t if not unicodedata.combining(ch)).lower().replace("'", "").replace("\u2019", "")


def _site_label(domain: str) -> str:
    return (domain or "").split(".", 1)[0]


def comp_site(c) -> str:
    """The competition's own site (its promoter, else its first entry page's), to tell similar prizes apart."""
    if c.promoter_domain and c.promoter_domain not in FREEMAIL:
        return c.promoter_domain
    return next((d for d in (registrable_domain(u) for u in (c.entry_urls or [])) if d and d not in FREEMAIL), "")


def _same_brand(a: str, b: str) -> bool:
    """kraftheinz / heinz, heinz (heinz.com) / heinz (heinz.co.uk): one site's name inside the other's."""
    if len(a) < 4 or len(b) < 4 or a in GENERIC_LABELS or b in GENERIC_LABELS:
        return False
    return a in b or b in a


class CompIndex:
    """The competitions you entered, indexed so the Inbox can guess which one an email is about, and find one from a
    few typed words.

    A guess weighs up, for each competition:
      * the sender: the competition's own site (kraftheinz.com for heinz.com counts too, as a brand's other site);
        a site shared by many of your entries (a competition platform) counts for less;
      * the words both use, rare ones counting most ("Heinz" says more than "voucher"); words in the subject or the
        sender's name count most, then the opening lines (where the prize is usually named), then the rest;
      * the same £ amount; and, to break ties, how recently you entered.
    Competitions first entered after the email arrived can't be what it's about. To stay quick with thousands of
    entries, each word's rarity is worked out once, and the reasons are only written for the guesses shown.
    Different £ amounts don't rule a
    competition out: an email names the prize you won ("a £50 Heinz to Home voucher"), the listing its top prize
    ("Win a £250 Honest Burgers voucher ... or Heinz to Home vouchers").
    """

    def __init__(self, comps: list):
        self.comps = list(comps)
        self._tok: dict[int, set[str]] = {}
        self._money: dict[int, set[int]] = {}
        self._doms: dict[int, set[str]] = {}
        self._hay: dict[int, tuple[str, ...]] = {}
        self._by_word: dict[str, list] = {}
        self._by_domain: dict[str, list] = {}
        for c in self.comps:
            k = id(c)
            self._tok[k] = {w for w in title_tokens(c.title) | title_tokens(c.prize) if not w.isdigit()}
            self._money[k] = money_amounts(c.title) | money_amounts(c.prize)
            self._doms[k] = {d for d in _comp_domains(c) if d not in FREEMAIL}
            self._hay[k] = tuple(dict.fromkeys(re.findall(
                r"[a-z0-9]+", _plain(f"{c.title or ''} {c.prize or ''} {' '.join(sorted(self._doms[k]))}"))))
            for w in self._tok[k]:
                self._by_word.setdefault(w, []).append(c)
            for d in self._doms[k]:
                self._by_domain.setdefault(d, []).append(c)
        n = len(self.comps)
        top = max(math.log((n + 1) / 1.5), 0.1)
        # 0-1: 1 for a word only one competition uses, near 0 for one most of them use.
        self._rarity = {w: max(0.05, min(1.0, math.log((n + 1) / (len(cs) + 0.5)) / top))
                        for w, cs in self._by_word.items()}
        # Words a sender's site name might contain ("heinz" in kraftheinz.com): long, and rare enough to mean something.
        self._name_words = [w for w, r in self._rarity.items() if len(w) >= 4 and r >= 0.6]

    def _rare(self, word: str) -> float:
        return self._rarity.get(word, 1.0)

    def _site_weight(self, domain: str) -> float:
        return min(1.0, 3 / max(len(self._by_domain.get(domain, ())), 1))

    @staticmethod
    def _when(c) -> datetime | None:
        return getattr(c, "last_entered_at", None) or getattr(c, "entered_at", None)

    def rank(self, subject: str, text: str, from_domain: str = "", from_name: str = "",
             received_at: datetime | None = None, limit: int = 3, exclude: int | None = None) -> Guesses:
        head = {w for w in title_tokens(f"{subject or ''} {from_name or ''}") if not w.isdigit()}
        lead = {w for w in title_tokens((text or "")[:800]) if not w.isdigit()}
        words = head | lead | {w for w in title_tokens((text or "")[:4000]) if not w.isdigit()}
        # Each word's worth: rarer counts more; the subject and sender's name most, then the opening lines.
        worth = {w: self._rarity[w] * (1.25 if w in head else 1.0 if w in lead else 0.6)
                 for w in words if w in self._rarity}
        sender = "" if (from_domain or "") in FREEMAIL else (from_domain or "")
        slabel = _site_label(sender)
        ctx = SimpleNamespace(
            worth=worth, words=set(worth), amounts=money_amounts(f"{subject or ''} {(text or '')[:2000]}"),
            sender=sender, ref=received_at or datetime.now(timezone.utc).replace(tzinfo=None),
            brand_sites={d for d in self._by_domain if _same_brand(slabel, _site_label(d))} if slabel else set(),
            named_words={w for w in self._name_words if w in slabel} if slabel else set())

        # Every competition sharing a word or a site is scored, common words included: one that shares only "gift
        # voucher" can still be the runner-up that stops a guess counting as a clear favourite.
        cands = {id(c): c for w in worth for c in self._by_word[w]}
        for d in {sender} | ctx.brand_sites:
            cands.update({id(c): c for c in self._by_domain.get(d, ())})
        for w in ctx.named_words:
            cands.update({id(c): c for c in self._by_word[w]})

        scored: list[tuple[float, datetime, int, int]] = []
        for k, c in cands.items():
            if exclude is not None and getattr(c, "id", None) == exclude:
                continue
            first = getattr(c, "entered_at", None) or self._when(c)
            if received_at and first and first > received_at + ENTERED_AFTER:
                continue  # entered well after the email came: it can't be about this one
            # Ties go to the most recent entry (a confirmation email follows the entry just made), then the newest.
            scored.append((self._score(k, c, ctx)[0], self._when(c) or datetime.min, getattr(c, "id", 0) or 0, k))
        best = heapq.nlargest(max(limit, 2), scored)
        top = [Guess(cands[k], round(s, 2), self._score(k, cands[k], ctx, explain=True)[1])
               for s, _, _, k in best[:limit] if s >= GUESS_MIN]
        sure = bool(top) and top[0].score >= SURE_MIN and (len(best) < 2 or best[0][0] - best[1][0] >= SURE_LEAD)
        site = max((t for t in scored if sender and sender in self._doms[t[3]]), default=None)
        from_site = None
        if site:
            from_site = Guess(cands[site[3]], round(site[0], 2), self._score(site[3], cands[site[3]], ctx, True)[1])
        return Guesses(top, sure, len(self.comps), from_site=from_site)

    def _score(self, k: int, c, ctx, explain: bool = False) -> tuple[float, list[str]]:
        """How likely the email is about competition c, and (only when asked, as it's slower) why in words."""
        score, reasons = 0.0, []
        doms, sender = self._doms[k], ctx.sender
        brand = sorted(doms & ctx.brand_sites) if ctx.brand_sites else []
        if sender and sender in doms:
            score += 3 * self._site_weight(sender)
            if explain:
                reasons.append(f"it's from the competition's site ({sender})")
        elif brand:
            score += 2 * self._site_weight(brand[0])
            if explain:
                reasons.append(f"the sender, {sender}, looks like the competition's site ({brand[0]})")
        shared = self._tok[k] & ctx.words
        money = self._money[k] & ctx.amounts
        if shared:
            score += min(3 * sum(ctx.worth[w] for w in shared), 6.0)
        if money:
            score += 1.0
        if explain and (shared or money):
            said = [f"\u201c{w}\u201d" for w in sorted(shared, key=lambda w: (-self._rare(w), w))[:5]]
            reasons.append(f"both mention {', '.join(said + ([f'£{min(money):,}'] if money else []))}")
        if ctx.named_words and not (sender in doms or brand):
            # A comp with no site of its own (found on a listing site) can still be named by the sender.
            named = sorted(self._tok[k] & ctx.named_words, key=lambda w: (-self._rare(w), w))
            if named:
                score += 1.5 * self._rare(named[0])
                if explain:
                    reasons.append(f"the sender, {sender}, has \u201c{named[0]}\u201d in its name")
        when = self._when(c)
        if when:  # ties go to the more recent entry
            score += 0.3 * max(0.0, 1 - abs((ctx.ref - when).days) / 120)
        return score, reasons

    def search(self, query: str, limit: int = 20) -> list:
        """Competitions whose title, prize or site has every typed word (or the start of it), fullest match first,
        then most recently entered. "#123" finds competition 123."""
        q = (query or "").strip()
        if m := re.fullmatch(r"#\s*(\d+)", q):
            return [c for c in self.comps if getattr(c, "id", None) == int(m.group(1))][:1]
        terms = list(dict.fromkeys(re.findall(r"[a-z0-9]+", _plain(q).replace("\u00a3", " "))))
        if not terms:
            return []
        found = []
        for c in self.comps:
            hay = self._hay[id(c)]
            if all(any(w.startswith(t) for w in hay) for t in terms):
                whole = sum(t in hay for t in terms)
                found.append((whole, self._when(c) or datetime.min, c))
        found.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [c for _, _, c in found[:limit]]


def gmail_link(message_id: str, account: str | None = None) -> str | None:
    """Opens this very email in Gmail (a search for its Message-ID), in the competition Gmail account."""
    mid = (message_id or "").strip().strip("<>").strip()
    if not mid or mid.startswith("no-id-"):
        return None
    who = f"?authuser={quote(account.strip())}" if account and account.strip() else ""
    return f"https://mail.google.com/mail/{who}#search/rfc822msgid%3A{quote(mid, safe='')}"


def folder_label(folder: str) -> str:
    """'[Google Mail]/Spam' -> 'Spam', 'INBOX' -> 'Inbox'."""
    f = (folder or "").strip()
    low = f.lower()
    if "spam" in low or "junk" in low:
        return "Spam"
    if low == "inbox":
        return "Inbox"
    return f.rsplit("/", 1)[-1] or f


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
                   known_ids: set[str], max_total: int = 500,
                   own_addr: str | None = None) -> tuple[list[ParsedMail], bool]:
    """Fetch unseen messages oldest-first. Returns (messages, truncated) - truncated means more are waiting.
    compbot's own alerts (see is_own_alert) are left where they are, unread."""
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
                typ, resp = imap.uid("fetch", b",".join(chunk),
                                     f"(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT {OWN_HEADER.upper()})] "
                                     "RFC822.SIZE)")
                if typ != "OK":
                    continue
                for item in resp:
                    if not isinstance(item, tuple):
                        continue
                    meta, hdr = item
                    uid_m = re.search(rb"UID (\d+)", meta)
                    size_m = re.search(rb"RFC822\.SIZE (\d+)", meta)
                    head = email.message_from_bytes(hdr)
                    mid = (head.get("Message-ID") or "").strip()
                    if _own_alert_headers(head, own_addr):
                        continue
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
                        out.append(parse_message(resp[0][1], folder, own_addr))
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
