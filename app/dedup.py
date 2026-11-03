"""One competition, many listings: spot the same comp found again (on another site, or posted twice on MSE).

Checked on 3 Nov 2026 data: about 90% of Competitions-Time's comps and well over half of Competition Database's were
already on MSE, and MSE itself had comps posted twice. Neither the entry link nor the title is enough on its own:
  * some links are hub pages shared by many different comps (John Lewis rewards, a publisher's /competitions page),
    so the same link doesn't always mean the same comp;
  * the same comp is worded differently on each site ("All At Sea - A Copy Of First Aid On Board" vs
    "E: 03/11 Win a copy of \"First Aid On Board\" book"), while different comps can look alike ("Win a Liverpool
    shirt signed by Alisson Becker" vs "... signed by van Dijk", "£100 store vouchers (find the dog)" vs
    "(fuzzy celeb)"). Matching on loose title similarity merged dozens of different MSE comps in testing.
See Matcher for the rules.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit

from .analyse import PAREN_RE, TITLE_DATE_RE, registrable_domain

TRACKING_PARAM_RE = re.compile(
    r"^(?:utm_\w+|fbclid|gclid|gbraid|wbraid|dclid|msclkid|yclid|mc_cid|mc_eid|_hs\w+|hsctatracking|igshid|igsh|"
    r"ref|ref_src|referrer|source|src|cmp|cmpid|campaign|s_cid|icid|intcmp|trk|share|si|_ga|_gl|at_\w+|sc_\w+|"
    r"cid|affid|aff|clickid|irclickid|awc|ito)$", re.I)
# Paths that are a site's list of competitions (or its rules), not one competition.
HUB_PATH_RE = re.compile(
    r"^/?(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:competitions?|comps?|win|giveaways?|prizes?|prize-?draws?|promotions?|offers?|"
    r"rewards?|puzzles?|contests?|sweepstakes|competition-rules|terms(?:-and-conditions)?|rules)?/?$", re.I)
HUB_MIN_COMPS = 3  # a specific-looking link shared by this many different comps is a hub too

STOP_WORDS = set("""
win wins winning a an the and of for to in on at with from by x your our my their its is be are this that or plus
worth prize prizes competition competitions comp comps giveaway giveaways draw prize-draw enter entry chance one
uk e d r daily inc incl including thanks courtesy via new latest special exclusive amazing fantastic luxury luxurious
""".split())


def canonical_url(url: str | None) -> str:
    """The part of a link that identifies the page: no scheme, www, tracking tags, fragment or trailing slash."""
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    path = re.sub(r"/{2,}", "/", p.path or "/").rstrip("/").lower() or "/"
    path = re.sub(r"/(?:index\.(?:html?|php|aspx?))$", "", path) or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
                             if not TRACKING_PARAM_RE.match(k)))
    return host + path + (f"?{query}" if query else "")


def is_generic_path(url: str | None) -> bool:
    """A homepage or a site's competitions/rewards/rules page: shared by every comp on that site."""
    key = canonical_url(url)
    if not key:
        return True
    path = key.split("?", 1)[0][len(key.split("/", 1)[0]):]
    return bool(HUB_PATH_RE.match(path))


def title_tokens(title: str | None) -> set[str]:
    """Meaningful words of a title: no closing-date prefix, compbot's tags, filler words or plural endings.
    Bracketed notes that aren't tags stay in ("(find the dog)" vs "(fuzzy celeb)" are different puzzles)."""
    from .analyse import TAG_ALIASES, TIME_RE

    t = title or ""
    m = TITLE_DATE_RE.match(t)
    if m:
        t = t[m.end():]

    def keep_note(pm: re.Match) -> str:
        raw = (pm.group(1) or pm.group(2) or "").strip()
        parts = [p.strip(" .!-#").lower() for p in re.split(r"\s*[/+,&]\s*|\s+-\s+|\s+and\s+", raw)]
        if TIME_RE.fullmatch(raw.strip(" -")) or all(not p or p in TAG_ALIASES for p in parts):
            return " "
        return f" {raw} "

    t = PAREN_RE.sub(keep_note, t)
    t = t.lower().replace("&", " ").replace("’", "").replace("'", "").replace("£", " ")
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)  # £1,000 -> 1000
    words = re.findall(r"[a-z0-9]+", t)
    out = set()
    for w in words:
        if w in STOP_WORDS or len(w) < 2:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


MONEY_RE = re.compile(r"£\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?\s*(k\b)?", re.I)


def money_amounts(title: str | None) -> set[int]:
    out = set()
    for m in MONEY_RE.finditer(title or ""):
        v = int(m.group(1).replace(",", ""))
        out.add(v * 1000 if m.group(2) else v)
    return out


def title_similarity(a: str | set[str], b: str | set[str], idf: dict[str, float] | None = None,
                     unseen: float = 1.0) -> float:
    """Share of the shorter title's words found in the other (0-1), rare words weighing more when idf is given
    (words never seen before count as the rarest). Needs 2+ shared words to count at all."""
    ta = a if isinstance(a, set) else title_tokens(a)
    tb = b if isinstance(b, set) else title_tokens(b)
    if not ta or not tb:
        return 0.0
    shared = ta & tb
    if len(shared) < 2:
        return 0.0
    w = (lambda x: idf.get(x, unseen)) if idf else (lambda x: 1.0)
    return sum(w(x) for x in shared) / min(sum(w(x) for x in ta), sum(w(x) for x in tb))


def _dates_close(a: date | None, b: date | None, days: int) -> bool | None:
    """True/False when both dates are known, None when either isn't."""
    if a is None or b is None:
        return None
    return abs((a - b).days) <= days




@dataclass
class Candidate:
    """What a listing tells us, for matching it against competitions already known."""
    title: str
    source: str = "mse"
    entry_urls: list[str] = field(default_factory=list)
    promoter_domain: str | None = None
    closes_on: date | None = None
    closes_precise: bool = True
    extra_title: str = ""  # e.g. the company on Competition Database ("Magic Radio")

    @property
    def tokens(self) -> set[str]:
        return title_tokens(self.title) | title_tokens(self.extra_title)


class Matcher:
    """Matches new listings against a snapshot of recent competitions (built once per discovery run).

    1. The same specific entry page (not a hub page) is the same competition, from any site, unless the two titles
       have nothing in common (two prizes on one page), or the closing dates or £ amounts differ (another round of a
       comp that reuses its page).
    2. Different specific entry pages are different entries, even for the same prize (an (MD) comp on several
       magazine sites can be entered once on each), so they are never merged.
    3. When one side has no entry link (Competitions-Time hides them; social comps have none), and the listing comes
       from a different site than the competition: the same promoter site, a similar title (rare words count more:
       "Kehlani" says more than "tickets") and the same closing date. With no promoter site either, the title has to
       be very similar. Different £ amounts ("£100 voucher" vs "£200 voucher") are never the same comp.
    """

    def __init__(self, comps: list, sources: dict[int, set[str]] | None = None):
        self.comps: list = []
        self.by_url: dict[str, list] = {}
        self.by_domain: dict[str, list] = {}
        self._tokens: dict[int, set[str]] = {}
        self._money: dict[int, set[int]] = {}
        self._sources: dict[int, set[str]] = {}
        self._df: dict[str, int] = {}
        self._idf: tuple[dict[str, float], float] | None = None
        for c in comps:
            self.add(c, (sources or {}).get(getattr(c, "id", None)))

    def add(self, comp, sources: set[str] | None = None) -> None:
        self.comps.append(comp)
        self._idf = None
        toks = title_tokens(comp.title)
        self._tokens[id(comp)] = toks
        self._money[id(comp)] = money_amounts(comp.title)
        self._sources[id(comp)] = set(sources or ()) | {getattr(comp, "source", None) or "mse"}
        for w in toks:
            self._df[w] = self._df.get(w, 0) + 1
        for u in dict.fromkeys(canonical_url(u) for u in (comp.entry_urls or [])):
            if u:
                self.by_url.setdefault(u, []).append(comp)
        doms = {registrable_domain(u) for u in (comp.entry_urls or [])} | {comp.promoter_domain or ""}
        for d in doms - {""}:
            self.by_domain.setdefault(d, []).append(comp)

    def note_source(self, comp, source: str) -> None:
        self._sources.setdefault(id(comp), set()).add(source)

    def index_urls(self, comp, urls: list[str]) -> None:
        """A competition already matched gained entry links (from another listing)."""
        for u in dict.fromkeys(canonical_url(u) for u in urls):
            if u and comp not in self.by_url.get(u, []):
                self.by_url.setdefault(u, []).append(comp)
        for d in {registrable_domain(u) for u in urls} - {""}:
            if comp not in self.by_domain.get(d, []):
                self.by_domain.setdefault(d, []).append(comp)

    def idf(self) -> tuple[dict[str, float], float]:
        """Word weights (rare = heavy) and the weight of a word no title has used yet."""
        if self._idf is None:
            n = max(len(self.comps), 1)
            self._idf = ({w: max(0.2, math.log((n + 1) / (df + 0.5))) for w, df in self._df.items()},
                         math.log((n + 1) / 0.5))
        return self._idf

    def is_hub(self, url: str) -> bool:
        if is_generic_path(url):
            return True
        sharing = self.by_url.get(canonical_url(url), [])
        if len(sharing) < HUB_MIN_COMPS:
            return False
        first = self._tokens[id(sharing[0])]
        return any(title_similarity(first, self._tokens[id(c)]) < 0.5 for c in sharing[1:])

    def find(self, cand: Candidate):
        """(the known competition this listing is, why) or (None, "")."""
        tokens = cand.tokens
        money = money_amounts(cand.title) | money_amounts(cand.extra_title)
        tolerance = 1 if cand.closes_precise else 7
        best, best_score, why = None, 0.0, ""
        for u in cand.entry_urls:
            if self.is_hub(u):
                continue
            for c in self.by_url.get(canonical_url(u), []):
                ct = self._tokens[id(c)]
                sim = title_similarity(tokens, ct)
                if len(tokens) >= 2 and len(ct) >= 2 and sim == 0:
                    continue  # two different prizes on one page stay separate (and the page is entered once)
                if _dates_close(cand.closes_on, c.closes_on, tolerance) is False or (
                        money and self._money[id(c)] and not money & self._money[id(c)]):
                    continue  # another round of a comp that reuses its page (this week's prize, next month's...)
                if sim + 1 > best_score:
                    best, best_score, why = c, sim + 1, "same entry page"
        if best is not None:
            return best, why
        idf, unseen = self.idf()

        def eligible(c) -> bool:
            if cand.entry_urls and c.entry_urls:
                return False  # both have specific entry pages and none is shared: different entries
            if cand.source in self._sources.get(id(c), set()):
                return False  # same site: only the same entry page counts
            if money and self._money[id(c)] and not money & self._money[id(c)]:
                return False
            return _dates_close(cand.closes_on, c.closes_on, tolerance) is not False

        domains = {registrable_domain(u) for u in cand.entry_urls} | {cand.promoter_domain or ""}
        for d in domains - {""}:
            for c in self.by_domain.get(d, []):
                if not eligible(c):
                    continue
                sim = title_similarity(tokens, self._tokens[id(c)], idf, unseen)
                if sim >= 0.6 and sim > best_score:
                    best, best_score, why = c, sim, f"same site ({d}), similar title and closing date"
        if best is not None or cand.closes_on is None:
            return best, why
        for c in self.comps:
            if c.closes_on is None or not eligible(c):
                continue
            ct = self._tokens[id(c)]
            if len(tokens & ct) < 3:
                continue
            sim = title_similarity(tokens, ct, idf, unseen)
            if sim >= 0.75 and sim > best_score:
                best, best_score, why = c, sim, "very similar title, same closing date"
        return (best, why) if best is not None else (None, "")


def recent_comps(s, today: date) -> tuple[list, dict[int, set[str]]]:
    """Competitions a new listing could be a repeat of (still open, or closed in the last few days), and the sites
    each has been found on."""
    from sqlalchemy import or_, select

    from .db import Competition, Listing, utcnow

    comps = list(s.scalars(select(Competition).where(
        Competition.discovered_at >= utcnow() - timedelta(days=200),
        or_(Competition.closes_on.is_(None), Competition.closes_on >= today - timedelta(days=3)))).all())
    sources: dict[int, set[str]] = {}
    ids = [c.id for c in comps]
    for i in range(0, len(ids), 500):
        for cid, src in s.execute(select(Listing.competition_id, Listing.source)
                                  .where(Listing.competition_id.in_(ids[i:i + 500]))).all():
            sources.setdefault(cid, set()).add(src)
    return comps, sources