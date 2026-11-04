"""Pure heuristics that decide how to fill each form field from the saved profile and the known answers.

Nothing here touches the browser, so it can be unit-tested. browser.py extracts `Field`s from the page, calls
`plan_form`, then applies the returned actions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz

PROFILE_KEYS = [
    "title", "first_name", "last_name", "full_name", "email", "email_confirm", "phone", "dob", "dob_day",
    "dob_month", "dob_year", "age", "gender", "address1", "address2", "address3", "town", "county", "postcode",
    "country", "twitter", "instagram",
]


@dataclass
class Field:
    idx: str
    tag: str                 # input | select | textarea | radio
    type: str = "text"
    name: str = ""
    id: str = ""
    label: str = ""
    placeholder: str = ""
    autocomplete: str = ""
    aria: str = ""
    required: bool = False
    options: list[dict] = field(default_factory=list)   # {"value", "text", "idx"}
    context: str = ""
    maxlength: int | None = None
    checked: bool = False

    @property
    def desc(self) -> str:
        parts = [self.label, self.placeholder, self.aria, self.name, self.id, self.autocomplete]
        return normalise(" ".join(p for p in parts if p))

    @property
    def human_label(self) -> str:
        return (self.label or self.placeholder or self.aria or self.name or self.id or self.tag).strip()[:120]


@dataclass
class Action:
    kind: str                # fill | select | check | uncheck | radio | skip | unknown
    value: str | None = None
    source: str = "rule"     # profile key, "answer", "rule", "llm"
    note: str = ""


@dataclass
class Ctx:
    profile: dict
    answers: list[str] = field(default_factory=list)
    question: str | None = None
    tags: list[str] = field(default_factory=list)
    marketing_opt_in: bool = False
    answer_cursor: int = 0
    heard_other: bool = False  # "How did you hear?" was answered "Other": a "please specify" box may follow

    @property
    def is_newsletter(self) -> bool:
        return "nl" in self.tags

    @property
    def want_marketing(self) -> bool:
        return self.is_newsletter or self.marketing_opt_in


def normalise(s: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s or "")
    s = re.sub(r"[_\-\[\]().:/|*]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


AUTOCOMPLETE_MAP = {
    "email": "email", "given-name": "first_name", "family-name": "last_name", "name": "full_name",
    "honorific-prefix": "title", "tel": "phone", "tel-national": "phone", "mobile": "phone", "bday": "dob",
    "bday-day": "dob_day", "bday-month": "dob_month", "bday-year": "dob_year", "address-line1": "address1",
    "street-address": "address1", "address-line2": "address2", "address-line3": "address3",
    "address-level2": "town", "address-level1": "county", "postal-code": "postcode", "country": "country",
    "country-name": "country", "sex": "gender",
}

SKIP_RE = re.compile(
    r"company|business|organi[sz]ation|job title|user ?name|password|coupon|promo ?code|voucher code|discount code|"
    r"card number|cvv|captcha|\bsearch\b|\bwebsite\b|\burl\b|honey ?pot|leave (this )?(blank|empty)|referr?al code")

KEY_PATTERNS: list[tuple[str, re.Pattern]] = [(k, re.compile(p)) for k, p in [
    ("email_confirm", r"(confirm|repeat|re ?type|re ?enter|verify).{0,15}e ?mail|e ?mail.{0,15}(confirm|again|repeat|verif)"),
    ("email", r"\be ?mail\b|email address"),
    ("dob_day", r"(birth|dob|bday).{0,15}\b(day|dd)\b|\b(day|dd)\b.{0,15}(birth|dob)"),
    ("dob_month", r"(birth|dob|bday).{0,15}\b(month|mm)\b|\b(month|mm)\b.{0,15}(birth|dob)"),
    ("dob_year", r"(birth|dob|bday).{0,15}\b(year|yyyy|yy)\b|\b(year|yyyy)\b.{0,15}(birth|dob)"),
    ("dob", r"date ?of ?birth|\bdob\b|birth ?date|birthday|d o b|\bborn\b"),
    ("age", r"^age$|\byour age\b|how old"),
    ("first_name", r"first ?name|fore ?name|given ?name|\bfname\b|christian name|name first"),
    ("last_name", r"last ?name|sur ?name|family ?name|\blname\b|second name|name last"),
    ("title", r"^title\b|\btitle$|salutation|honorific|\bprefix\b"),
    ("phone", r"phone|mobile|\btel\b|telephone|contact (no|number)|\bcell\b"),
    ("postcode", r"post ?code|\bzip\b|postal"),
    ("address3", r"address.{0,8}\b3\b|addr\w* ?3\b"),
    ("address2", r"address.{0,8}\b2\b|addr\w* ?2\b|street ?2|line two|apartment|\bflat\b|suite"),
    ("address1", r"address.{0,8}\b1\b|addr\w* ?1\b|street|house (name|number|no)|address|\broad\b|building|line 1"),
    ("town", r"\btown\b|\bcity\b|post ?town|locality|suburb"),
    ("county", r"county|\bregion\b|\bstate\b|province"),
    ("country", r"country|nation"),
    ("gender", r"gender|\bsex\b"),
    ("twitter", r"twitter|\bx (handle|username)"),
    ("instagram", r"instagram|\binsta\b"),
    ("full_name", r"full ?name|your ?name|^name\b|\bname$|\bname\b"),
]]

PERSONAL_Q_RE = re.compile(
    r"\byour\b.{0,20}\b(name|e ?mail|phone|mobile|number|address|post ?code|date of birth|dob|age|town|city|county|"
    r"country|title|gender)\b")
QUESTION_START_RE = re.compile(
    r"^(q ?\d*\b|question|name (one|a|the|an)\b|which|what|who|where|when|how (many|much|long|old is)|unscramble|"
    r"complete|fill in|spot|count|identify|guess|true or false)\b")  # \b: "Country" isn't "count...", nor "WhatsApp"
ANSWER_FIELD_RE = re.compile(r"\banswer|\bquestion\b|\bq ?\d\b|\bquiz\b|\bguess\b|solution|code ?word|secret word|key ?word")
CREATIVE_RE = re.compile(
    r"tie ?break|in (\d+|ten|twenty|fifty|25|15) words|tell us|why (do|would|should|you)|caption|slogan|sentence|"
    r"describe|\bcomment|\bmessage\b|\bstory\b|\bpoem\b|\breason\b")

TERMS_RE = re.compile(
    r"terms|t ?& ?c|t s ?& ?c|\btcs\b|conditions|\brules\b|privacy|i (have )?read|i agree|i accept|i confirm|"
    r"i consent|\bagree\b|\baccept\b|over (the age of )?1[68]|aged? 1[68]|1[68] ?(\+|or over|years)|eligible|"
    r"uk resident|resident (of|in)|i am over|consent to (the )?processing")
MARKETING_RE = re.compile(
    r"newsletter|marketing|offers|promotions?\b|\bnews\b|updates|hear (about|from|more)|keep (me )?(up to date|in "
    r"touch|informed|posted)|contact me|e ?mail me|send me|sign me up|subscribe|third ?part|partners|carefully "
    r"selected|by (e ?mail|sms|text|post|phone)|opt ?in|exclusive|discounts|latest|future communications?|"
    r"communications? from|via (e ?mail|sms|text|post|phone)|contact you (about|with|by|via)")
# What makes a box genuinely about entering (not just "I agree ..." in front of a marketing consent).
TERMS_CORE_RE = re.compile(
    r"terms|t ?& ?c|t s ?& ?c|\btcs\b|conditions|\brules\b|over (the age of )?1[68]|aged? 1[68]|1[68] ?(\+|or over|years)|"
    r"eligible|uk resident|resident (of|in)|i am over")
OPTOUT_RE = re.compile(r"(do not|don ?t|not) (wish|want|like) to|opt ?out|unsubscribe|if you (do not|don ?t)|prefer not")
THIRD_PARTY_RE = re.compile(r"third ?part|partners|carefully selected|other (companies|brands)|associated brands|sister")
HEAR_RE = re.compile(r"how did you (hear|find|see)|where did you (hear|see|find)|how you heard|source|referr")
HEAR_PREFS = ["moneysavingexpert", "money saving expert", "mse", "forum", "online", "internet", "website", "web",
              "search", "other", "social", "email", "newsletter"]
HEAR_SPECIFY_RE = re.compile(r"^(?:if other,? )?(?:please )?specify\b|^other\b.{0,25}specify|^if other\b")
HEARD_AT = "MoneySavingExpert forum"
PLACEHOLDER_OPT_RE = re.compile(
    r"^\s*(?:-+.*|please (?:select|choose).*|(?:select|choose)\b.*|pick one.*|make a selection.*|dd|mm|yyyy|day|"
    r"month|year)?\s*$", re.I)
YES_RE = re.compile(r"^(yes|y|true|i do|i am|i agree|agree|yes please|opt in)\b", re.I)
NO_RE = re.compile(r"^(no|n|false|i don ?t|no thanks|not now|opt out)\b", re.I)

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


def _parse_dob(p: dict) -> date | None:
    try:
        return date.fromisoformat(str(p.get("dob", "")).strip())
    except ValueError:
        return None


SPLIT_NAME = {"first": "first_name", "forename": "first_name", "given": "first_name", "last": "last_name",
              "surname": "last_name", "family": "last_name"}
# Fields asking for someone else's details (a running partner, a nominee, a child...) must never get the profile.
OTHER_PERSON_RE = re.compile(
    r"\b(partner|friend|guest|child|children|kid|son|daughter|baby|pet|dog|cat|nominee|nominat\w*|recipient|their|"
    r"companion|plus one|loved one|teacher|colleague|mum|dad|parent)s?\b")
# Simple sums used as anti-spam checks are a CAPTCHA by another name, so they're left for a human.
MATH_CHECK_RE = re.compile(r"\d+\s*(?:\+|plus|-|minus|x|×|\*|times)\s*\d+|what is (?:\d+|one|two|three|four|five)\b")
GENERIC_AUTOCOMPLETE = {"full_name", "address1"}


def _label_text(f: Field) -> str:
    return normalise(" ".join(p for p in [f.label, f.placeholder, f.aria] if p))


def _key_from_text(d: str) -> str | None:
    for key, pat in KEY_PATTERNS:
        if pat.search(d):
            return key
    return None


def detect_profile_key(f: Field) -> str | None:
    label = _label_text(f)
    core = re.sub(r"[*?:()]+|\brequired\b", " ", label)
    core = re.sub(r"\s+", " ", core).strip()
    if label and OTHER_PERSON_RE.search(label):
        return None
    if core in SPLIT_NAME:
        return SPLIT_NAME[core]
    d = f.desc
    if f.type == "email":
        return "email_confirm" if KEY_PATTERNS[0][1].search(d) else "email"
    if f.type == "tel":
        return "phone"
    if f.type == "date" and re.search(r"birth|dob|born", d):
        return "dob"
    if re.search(r"\bdd mm yy(yy)?\b|\bmm dd yy(yy)?\b|\byyyy mm dd\b", d) and re.search(r"birth|dob|born|bday", d):
        return "dob"
    ac = (f.autocomplete or "").strip().lower().split()
    ac_key = AUTOCOMPLETE_MAP.get(ac[-1]) if ac else None
    label_key = _key_from_text(core) if core else None
    # The visible label beats a generic autocomplete hint (e.g. "First name" marked autocomplete="name") and beats
    # the field's internal name (e.g. a "City" box named address[city]).
    if ac_key and not (label_key and label_key != ac_key and ac_key in GENERIC_AUTOCOMPLETE):
        return ac_key
    if label_key:
        return label_key
    return _key_from_text(d)


def looks_like_question(f: Field) -> bool:
    label = normalise(f.label or f.placeholder or f.aria)
    if not label or PERSONAL_Q_RE.search(label):
        return False
    # A short label naming a personal field ("EMAIL ADDRESS * ?" with a tooltip icon) isn't a quiz question.
    core = re.sub(r"\s+", " ", re.sub(r"[*?:()]+|\brequired\b", " ", label)).strip()
    if len(core.split()) <= 4 and (core in SPLIT_NAME or _key_from_text(core)) and not QUESTION_START_RE.match(core):
        return False
    return (f.label or "").strip().rstrip("* ").endswith("?") or bool(QUESTION_START_RE.match(label)) or bool(
        ANSWER_FIELD_RE.search(f.desc))


def is_placeholder_option(o: dict) -> bool:
    return not str(o.get("value", "")).strip() or bool(PLACEHOLDER_OPT_RE.match(str(o.get("text", "")).strip()))


def choose_option(options: list[dict], candidates: list[str], threshold: int = 86) -> dict | None:
    real = [o for o in options if not is_placeholder_option(o)]
    cands = [normalise(c) for c in candidates if c and str(c).strip()]
    for c in cands:
        for o in real:
            if normalise(o.get("text", "")) == c or normalise(str(o.get("value", ""))) == c:
                return o
    best, best_score = None, 0
    for c in cands:
        for o in real:
            score = max(fuzz.token_set_ratio(c, normalise(o.get("text", ""))),
                        fuzz.token_set_ratio(c, normalise(str(o.get("value", "")))))
            if score > best_score:
                best, best_score = o, score
    return best if best_score >= threshold else None


def match_answer_option(options: list[dict], answers: list[str]) -> tuple[dict | None, int]:
    best, best_score = None, 0
    for a in answers:
        na = normalise(a)
        for o in options:
            if is_placeholder_option(o):
                continue
            no = normalise(o.get("text", "")) or normalise(str(o.get("value", "")))
            if not no:
                continue
            score = fuzz.token_set_ratio(na, no)
            if na == no:
                score = 100
            elif len(na) > 2 and (na in no or no in na):
                score = max(score, 92)
            if score > best_score:
                best, best_score = o, score
    return best, best_score


def profile_value(key: str, p: dict, f: Field | None = None) -> str:
    dob = _parse_dob(p)
    if key in ("email", "email_confirm"):
        return p.get("email", "")
    if key == "full_name":
        return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    if key == "dob":
        if not dob:
            return ""
        hint = (f.placeholder + " " + f.label).lower() if f else ""
        if f and f.type == "date" or "yyyy-mm-dd" in hint:
            return dob.isoformat()
        if "mm/dd" in hint:
            return dob.strftime("%m/%d/%Y")
        if "dd-mm" in hint:
            return dob.strftime("%d-%m-%Y")
        return dob.strftime("%d/%m/%Y")
    if key == "dob_day":
        return str(dob.day) if dob else ""
    if key == "dob_month":
        return str(dob.month) if dob else ""
    if key == "dob_year":
        return str(dob.year) if dob else ""
    if key == "age":
        if not dob:
            return ""
        t = date.today()
        return str(t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day)))
    if key == "country":
        return p.get("country") or "United Kingdom"
    if key == "address3":
        return ""
    return str(p.get(key, "") or "")


UK_COUNTY_ABBREV = {
    "beds": "Bedfordshire", "berks": "Berkshire", "bucks": "Buckinghamshire", "cambs": "Cambridgeshire",
    "derbys": "Derbyshire", "glos": "Gloucestershire", "gloucs": "Gloucestershire", "hants": "Hampshire",
    "herts": "Hertfordshire", "lancs": "Lancashire", "leics": "Leicestershire", "lincs": "Lincolnshire",
    "middx": "Middlesex", "northants": "Northamptonshire", "northd": "Northumberland", "notts": "Nottinghamshire",
    "oxon": "Oxfordshire", "salop": "Shropshire", "shrops": "Shropshire", "staffs": "Staffordshire",
    "warks": "Warwickshire", "wilts": "Wiltshire", "worcs": "Worcestershire", "yorks": "Yorkshire",
}


def county_candidates(county: str) -> list[str]:
    """'Berks' -> ['Berks', 'Berkshire'], 'Berkshire' -> ['Berkshire', 'Berks'] (lists use either form, or
    regions such as 'Bucks & Oxon')."""
    if not county:
        return []
    n = normalise(county).rstrip(".")
    out = [county]
    if n in UK_COUNTY_ABBREV:
        out.append(UK_COUNTY_ABBREV[n])
    out += [abbr.title() for abbr, full in UK_COUNTY_ABBREV.items() if normalise(full) == n]
    return list(dict.fromkeys(out))


# English counties by region, for "Region" lists ("South East", "East of England"...).
UK_REGIONS = {
    "North East": ["northumberland", "tyne and wear", "county durham", "durham", "teesside"],
    "North West": ["cumbria", "lancashire", "greater manchester", "merseyside", "cheshire"],
    "Yorkshire and the Humber": ["north yorkshire", "west yorkshire", "south yorkshire", "east riding of yorkshire",
                                 "east yorkshire", "yorkshire", "humberside"],
    "East Midlands": ["derbyshire", "nottinghamshire", "lincolnshire", "leicestershire", "rutland", "northamptonshire"],
    "West Midlands": ["herefordshire", "shropshire", "staffordshire", "warwickshire", "west midlands", "worcestershire"],
    "East of England": ["bedfordshire", "cambridgeshire", "essex", "hertfordshire", "norfolk", "suffolk"],
    "London": ["greater london", "london", "middlesex"],
    "South East": ["berkshire", "buckinghamshire", "east sussex", "west sussex", "sussex", "hampshire", "isle of wight",
                   "kent", "oxfordshire", "surrey"],
    "South West": ["bristol", "cornwall", "devon", "dorset", "gloucestershire", "somerset", "wiltshire"],
}


def region_candidates(p: dict) -> list[str]:
    """For a "Region" list: your county's region of England ("South East"), then your country (some forms' "Region"
    is United Kingdom / Europe / USA)."""
    counties = {normalise(c).rstrip(".") for c in county_candidates(str(p.get("county") or ""))}
    out = []
    for region, names in UK_REGIONS.items():
        if counties & set(names):
            out += [region, f"{region} England", f"{region} of England"]
            if region == "East of England":
                out.append("East Anglia")
    return out + option_candidates("country", p)


def option_candidates(key: str, p: dict) -> list[str]:
    dob = _parse_dob(p)
    if key == "county":
        return county_candidates(str(p.get("county") or ""))
    if key == "country":
        c = p.get("country") or "United Kingdom"
        uk = ["United Kingdom", "UK", "Great Britain", "GB", "England", "United Kingdom of Great Britain and Northern Ireland"]
        return [c] + (uk if normalise(c) in {"united kingdom", "uk", "gb", "great britain", "england"} else [])
    if key == "title":
        t = p.get("title", "")
        return [t, t + "."] if t else []
    if key == "gender":
        g = p.get("gender", "")
        return [g] if g else ["Prefer not to say", "Prefer not to disclose", "Rather not say", "Not specified"]
    if key == "dob_day" and dob:
        return [str(dob.day), f"{dob.day:02d}"]
    if key == "dob_month" and dob:
        m = MONTHS[dob.month - 1]
        return [str(dob.month), f"{dob.month:02d}", m, m[:3]]
    if key == "dob_year" and dob:
        return [str(dob.year)]
    v = profile_value(key, p)
    return [v] if v else []


def _next_answer(ctx: Ctx) -> str | None:
    if not ctx.answers:
        return None
    a = ctx.answers[min(ctx.answer_cursor, len(ctx.answers) - 1)]
    ctx.answer_cursor += 1
    return a


CHANNEL_RE = re.compile(r"^(?:by )?(?:e ?mail|sms|text(?: message)?|post|phone|telephone|whats ?app|online advertisements?|"
                        r"social media|push notifications?)$")


def map_checkbox(f: Field, ctx: Ctx) -> Action:
    label = normalise(f.label)
    # Very short labels ("Email", "MTB") only make sense with the surrounding question.
    d = normalise(f.label + " " + (f.context[:300] if len(label.split()) <= 3 else ""))
    if CHANNEL_RE.match(label):
        # "How would you like to hear from us?  [ ] Email  [ ] SMS ..." - for newsletter comps email *is* the entry.
        want = ctx.want_marketing and label in ("e mail", "email", "by email", "by e mail")
        return Action("check" if want else "skip", source="rule", note="contact channel")
    is_terms, is_mkt, optout = bool(TERMS_RE.search(d)), bool(MARKETING_RE.search(d)), bool(OPTOUT_RE.search(d))
    if is_mkt and is_terms and not TERMS_CORE_RE.search(d):
        is_terms = False  # "I agree that my details may be used for future communication by email" is marketing
    if optout:
        return Action("uncheck" if ctx.want_marketing else "check", source="rule", note="marketing opt-out box")
    if is_mkt and not is_terms:
        if f.required:
            return Action("check", source="rule", note="required marketing consent")
        if THIRD_PARTY_RE.search(d):
            return Action("uncheck", source="rule", note="third-party marketing")
        return Action("check" if ctx.want_marketing else "uncheck", source="rule", note="marketing opt-in")
    if is_terms or f.required:
        return Action("check", source="rule", note="terms / eligibility" if is_terms else "required checkbox")
    return Action("skip", note="optional checkbox")


def map_choice(f: Field, ctx: Ctx) -> Action:
    """Selects and radio groups."""
    kind = "radio" if f.tag == "radio" else "select"
    label_d = normalise(f.label or f.context[:200])
    texts = [normalise(o.get("text", "")) for o in f.options if not is_placeholder_option(o)]
    real = [o for o in f.options if not is_placeholder_option(o)]
    # A lone radio button agreeing to terms / privacy (e.g. Jotform "I agree to the terms and conditions").
    if kind == "radio" and len(real) == 1:
        about = normalise(" ".join([f.label, real[0].get("text", ""), f.context[:300]]))
        if TERMS_RE.search(about) and not (MARKETING_RE.search(about) and not TERMS_RE.search(normalise(f.label))):
            return Action(kind, real[0].get("idx") or real[0].get("value"), source="rule", note="terms / consent")
    key = detect_profile_key(f)
    if key in {"title", "country", "county", "gender", "dob_day", "dob_month", "dob_year"} or (
            key in {"town"} and f.options):
        o = choose_option(f.options, option_candidates(key, ctx.profile))
        if not o and key == "county" and re.search(r"\bregion\b", label_d + " " + normalise(f.name)):
            o = choose_option(f.options, region_candidates(ctx.profile))
        if o:
            return Action(kind, o.get("idx") or o.get("value"), source=key)
        if f.required:
            return Action("unknown", note=f"no matching option for {key}")
        return Action("skip", note=f"no option for {key}")
    yes_no = texts and all(YES_RE.match(t) or NO_RE.match(t) for t in texts)
    if yes_no:
        # The "Yes" option's own wording often carries the meaning ("Yes - I agree to the use of my data").
        yes_text = next((t for t in texts if YES_RE.match(t)), "")
        is_mkt = bool(MARKETING_RE.search(label_d))
        if OPTOUT_RE.search(label_d):
            want_yes = not ctx.want_marketing
        elif is_mkt and not TERMS_RE.search(label_d):
            want_yes = ctx.want_marketing and not THIRD_PARTY_RE.search(label_d)
        elif TERMS_RE.search(label_d):
            want_yes = True
        elif TERMS_RE.search(yes_text) and not MARKETING_RE.search(yes_text):
            want_yes = True
        elif MARKETING_RE.search(yes_text):
            want_yes = ctx.want_marketing and not THIRD_PARTY_RE.search(yes_text)
        else:
            want_yes = None
        if want_yes is not None:
            for o in f.options:
                t = normalise(o.get("text", ""))
                if (want_yes and YES_RE.match(t)) or (not want_yes and NO_RE.match(t)):
                    return Action(kind, o.get("idx") or o.get("value"), source="rule", note="yes/no")
    if HEAR_RE.search(label_d):
        o = choose_option(f.options, HEAR_PREFS, threshold=90) or next(
            (o for o in f.options if not is_placeholder_option(o)), None)
        if o:
            ctx.heard_other = normalise(o.get("text", "")).startswith("other")
            return Action(kind, o.get("idx") or o.get("value"), source="rule", note="how did you hear")
    if ctx.answers:
        o, score = match_answer_option(f.options, ctx.answers)
        if o and score >= 75:
            return Action(kind, o.get("idx") or o.get("value"), source="answer", note=f"answer match {score}")
    if not f.required:
        if looks_like_question(f):  # an unanswered quiz question - the AI may be able to help
            return Action("skip", note="question with no known answer")
        return Action("skip", note="optional choice")
    if kind == "select" and not looks_like_question(f) and not ctx.question:
        o = next((o for o in f.options if not is_placeholder_option(o)), None)
        if o:
            return Action(kind, o.get("idx") or o.get("value"), source="rule", note="first option (guess)")
    return Action("unknown", note="required choice with no known answer")


def map_text(f: Field, ctx: Ctx) -> Action:
    d = f.desc
    if f.type in ("password", "search", "url", "file", "hidden"):
        return Action("unknown" if f.required and f.type == "file" else "skip", note=f"{f.type} field")
    key = None
    label = _label_text(f)
    if MATH_CHECK_RE.search(label) and (not ctx.answers or len(label.split()) <= 6):
        # "9 + 3 equals" is a CAPTCHA by another name, even on a comp whose question has a known answer.
        return Action("unknown", note="captcha: maths anti-spam check")
    if OTHER_PERSON_RE.search(label) and re.search(r"name|e ?mail|phone|number|address", label):
        return Action("unknown" if f.required else "skip", note="asks for someone else's details")
    ac = (f.autocomplete or "").strip().lower().split()
    strong = (ac and ac[-1] in AUTOCOMPLETE_MAP) or f.type in ("email", "tel")
    if strong:
        key = detect_profile_key(f)
    elif looks_like_question(f) and not CREATIVE_RE.search(d):
        ans = _next_answer(ctx)
        if ans:
            return Action("fill", ans, source="answer")
        return Action("unknown" if f.required else "skip", note="question with no known answer")
    elif SKIP_RE.search(d):
        return Action("skip", note="not needed")
    else:
        key = detect_profile_key(f)
    if key:
        val = profile_value(key, ctx.profile, f)
        if val or key == "address3":
            return Action("fill", val, source=key) if val else Action("skip", note="address line 3")
        return Action("unknown" if f.required else "skip", note=f"profile has no {key}")
    if HEAR_SPECIFY_RE.search(label) and (ctx.heard_other or HEAR_RE.search(d)):
        return Action("fill", HEARD_AT, source="rule", note="where you heard (other)")
    if CREATIVE_RE.search(d):
        return Action("unknown" if f.required else "skip", note="creative / tie-breaker answer")
    if ctx.answers and (f.tag == "textarea" or ANSWER_FIELD_RE.search(d)):
        ans = _next_answer(ctx)
        if ans:
            return Action("fill", ans, source="answer")
    return Action("unknown" if f.required else "skip", note="unrecognised field")


def map_field(f: Field, ctx: Ctx) -> Action:
    if f.tag == "input" and f.type == "checkbox":
        return map_checkbox(f, ctx)
    if f.tag in ("select", "radio"):
        return map_choice(f, ctx)
    return map_text(f, ctx)


def plan_form(fields: list[Field], ctx: Ctx) -> list[tuple[Field, Action]]:
    return [(f, map_field(f, ctx)) for f in fields]


AUTOMATION_WORD_RE = re.compile(
    r"\bautomat\w*\s+(?:means|methods?|devices?|software|entr\w+|generated|systems?|tools?|programs?|process\w*|"
    r"scripts?|mechanisms?|computer\w*|or\b)|"
    r"\b(?:generated|made|submitted|entered|completed|created|sent)\s+(?:\w+\s+){0,4}?(?:automat\w*|by (?:a |any )?"
    r"(?:script|macro|bot|robot|computer))|"
    r"\b(?:scripts?|scripted|macros?|bots?|robots?|robotic)\b|computer[- ]generated|entry[- ]software", re.I)
VOID_WORD_RE = re.compile(
    r"\b(void|invalid|disqualif\w*|not (?:be )?(?:accepted|valid|permitted|allowed|eligible|counted)|will not count|"
    r"won'?t (?:be )?(?:accepted|count\w*)|prohibited|forbidden|reject\w*|excluded|ineligible|not be entered)\b", re.I)
ENTRY_WORD_RE = re.compile(r"\bentr(?:y|ies|ants?)\b|\bmeans\b|\bmethods?\b|\bdevices?\b|\bsubmitted\b", re.I)


def find_automation_clause(text: str) -> str | None:
    """Return the T&C sentence that bans automated/scripted entries, if any."""
    if not text:
        return None
    for sentence in re.split(r"(?<=[.;!?])\s+|\n+", text):
        if len(sentence) > 600:
            continue
        # "...using any means to circumvent this condition [one entry per household] such as ... computer-generated
        # entries" is about dodging an entry limit, not a ban on a single automated entry.
        if CIRCUMVENT_LIMIT_RE.search(sentence):
            continue
        if AUTOMATION_WORD_RE.search(sentence) and VOID_WORD_RE.search(sentence) and ENTRY_WORD_RE.search(sentence):
            return re.sub(r"\s+", " ", sentence).strip()
    return None


CIRCUMVENT_LIMIT_RE = re.compile(r"circumvent (?:this|the|these|any such) (?:condition|restriction|limit|rule)s?", re.I)


def apply_llm_action(f: Field, raw: dict, ctx: Ctx) -> Action:
    """Turn an LLM suggestion into a concrete Action (resolving profile keys locally)."""
    if not isinstance(raw, dict) or raw.get("unknown"):
        return Action("unknown", source="llm", note="LLM could not answer")
    if raw.get("skip"):
        return Action("skip", source="llm")
    if "profile" in raw and raw["profile"] in PROFILE_KEYS:
        key = raw["profile"]
        if f.tag in ("select", "radio"):
            o = choose_option(f.options, option_candidates(key, ctx.profile))
            return Action("radio" if f.tag == "radio" else "select", o.get("idx") or o.get("value"), source="llm") if o \
                else Action("unknown", source="llm")
        val = profile_value(key, ctx.profile, f)
        return Action("fill", val, source="llm:" + key) if val else Action("unknown", source="llm")
    if "option" in raw and f.options:
        o = choose_option(f.options, [str(raw["option"])], threshold=80)
        if o:
            return Action("radio" if f.tag == "radio" else "select", o.get("idx") or o.get("value"), source="llm")
    if "check" in raw and f.type == "checkbox":
        return Action("check" if raw["check"] else "uncheck", source="llm")
    if "text" in raw and f.tag in ("input", "textarea"):
        text = str(raw["text"]).strip()
        if f.maxlength:
            text = text[: f.maxlength]
        return Action("fill", text, source="llm")
    return Action("unknown", source="llm")
