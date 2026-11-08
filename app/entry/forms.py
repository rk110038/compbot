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
    picker: bool = False     # a read-only date box filled through its calendar widget
    link_inside: bool = False  # a drawn tick box with links inside it (ticked with a script click, see _apply)
    owns: str = ""           # a drawn drop-down list (role=listbox): the id of the pop-up holding its choices
    toggle_for: str = ""     # a tick box drawn as a clickable toggle beside a hidden input (the input's id)

    @property
    def desc(self) -> str:
        parts = [self.label, _placeholder(self), self.aria, self.name, self.id, self.autocomplete]
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


# Microsoft Forms reads each question out with its number and type ("2. Full name Single line text.", "County Multi
# Line Text.", "Which issue? Single choice."); GoGirl's forms add "(Required.)". Only the question itself matters.
_FORM_TYPE_RE = re.compile(
    r"(?:\s*(?:\(required\.?\)|required to answer\.?|single[- ]line text\.?|multi[- ]?line text\.?|single choice\.?|"
    r"multiple choice\.?|ranking\.?|rating\.?|likert\.?|file upload\.?|net promoter score\.?))+\s*$", re.I)
_FORM_NUMBER_RE = re.compile(r"^\s*(?:\*\s*)?\d{1,2}\s*[.)]\s+(?=\S)")


def clean_label(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    out = _FORM_TYPE_RE.sub("", t)
    out = re.sub(r"(?<=\S) Date\.$", "", out)  # Microsoft Forms' date questions ("Date of birth Date.")
    out = _FORM_NUMBER_RE.sub("", out).strip()
    return out or t


# The placeholder Microsoft and Google Forms put in every box. With a label beside it, it says nothing about the
# field, and its "answer" made every box (name and email included) look like a quiz answer.
GENERIC_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:please\s+)?(?:enter|type|write|add|give|insert)?\s*(?:your|an?|the)?\s*(?:answer|response|reply)s?"
    r"(?:\s+here)?\s*\.{0,3}\s*$", re.I)


def _placeholder(f: "Field") -> str:
    if f.label and f.label.strip() and GENERIC_PLACEHOLDER_RE.match(f.placeholder or ""):
        return ""
    return f.placeholder


AUTOCOMPLETE_MAP = {
    "email": "email", "given-name": "first_name", "family-name": "last_name", "name": "full_name",
    "honorific-prefix": "title", "tel": "phone", "tel-national": "phone", "mobile": "phone", "bday": "dob",
    "bday-day": "dob_day", "bday-month": "dob_month", "bday-year": "dob_year", "address-line1": "address1",
    "street-address": "address1", "address-line2": "address2", "address-line3": "address3",
    "address-level2": "town", "address-level1": "county", "postal-code": "postcode", "country": "country",
    "country-name": "country", "sex": "gender",
}

HONEYPOT_RE = re.compile(r"honey ?pot|leave (this |it )?(field )?(blank|empty)|(do not|don ?t) (fill|complete|enter|change)")
SKIP_RE = re.compile(
    r"company|business|organi[sz]ation|job title|user ?name|password|coupon|promo ?code|voucher code|discount code|"
    r"card number|cvv|captcha|\bsearch\b|\bwebsite\b|\burl\b|referr?al code|" + HONEYPOT_RE.pattern)

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
    r"communications? from|via (e ?mail|sms|text|post|phone)|contact you (about|with|by|via)|"
    # "Receive emails from Silver Origins?" (Cornwall Living), "I'd also like to receive all emails from Yeo Valley"
    r"receive\b.{0,25}\b(e ?mails?|newsletters?|texts?|sms|messages|communications?|information|info)\b|e ?mails? from|"
    # Roborock: "I consent to the transfer of my personal data to Real Madrid C.F. for ... commercial communications"
    r"commercial (communications?|messages|information|purposes)|(transfer|pass|passing|share|sharing|disclos\w*) "
    r"(of |on )?(my |your )?(personal )?(data|details|information) (to|with)\b|\bsponsors?\b")
# What makes a box genuinely about entering (not just "I agree ..." in front of a marketing consent).
TERMS_CORE_RE = re.compile(
    r"terms|t ?& ?c|t s ?& ?c|\btcs\b|conditions|\brules\b|over (the age of )?1[68]|aged? 1[68]|1[68] ?(\+|or over|years)|"
    r"eligible|uk resident|resident (of|in)|i am over")
# A box you tick to *stop* marketing. Boilerplate such as "you can unsubscribe / opt out at any time" on an opt-in box
# doesn't make it one (Yeo Valley: compbot ticked "I'd also like to receive all emails" because of it).
OPTOUT_RE = re.compile(
    r"(?:do not|don[' \u2019]?t|not) (?:wish|want|like) to (?:receive|be (?:contacted|sent|kept|added|signed)|hear|get|"
    r"join|sign up|subscribe)|if you (?:do not|don[' \u2019]?t|would not|wouldn[' \u2019]?t) (?:want|wish|like)|"
    r"(?:^|\b(?:please|i want to|i wish to|tick to|check to|click to) )opt (?:me )?out\b|\bopt (?:me )?out (?:of|from)\b|"
    r"prefer not to (?:receive|be contacted|hear|get)|(?:tick|check) (?:this|the|here)\b.{0,25}\b(?:not|don[' \u2019]?t)"
    r" (?:wish|want|like)")
THIRD_PARTY_RE = re.compile(r"third ?part|partners|carefully selected|other (companies|brands)|associated brands|sister|"
                            r"\bsponsors?\b|(transfer|pass|passing|share|sharing|disclos\w*) (of |on )?(my |your )?"
                            r"(personal )?(data|details|information) (to|with)\b")
# A box that opens by asking whether you'd like marketing is an opt-in, whatever boilerplate follows (Purina's ends
# "You confirm that you are over 18 years old", which on its own reads like an eligibility box).
OPTIN_LEAD_RE = re.compile(
    r"^(?:yes,? )?(?:please,? )?(?:(?:click|tick|check|select)(?: (?:here|this box|the box|this))? (?:if|to confirm "
    r"(?:that )?) )?(?:you|i)(?: would|'d|\u2019d| wish| want)?(?: like)? (?:to )?(?:receive|hear|be (?:kept|contacted|"
    r"sent|added)|get|join|sign up|subscribe|opt in)\b|^(?:yes,? )?please,? (?:keep me|send me|sign me up|add me|"
    r"contact me|e ?mail me)\b|^keep me (?:posted|updated|informed|in the loop)\b|^sign me up\b")
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
# Simple sums used as anti-spam checks are a CAPTCHA by another name, so they're left for a human (never answered by
# compbot or its AI). Read from the label as written: normalise() turns "-" into a space, and sites use en dashes
# ("9 – 5 equals", Outdoors Magic) and minus signs as well as hyphens.
_NUM = (r"(?:\d{1,3}|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
        r"sixteen|seventeen|eighteen|nineteen|twenty)")
_MATH_OP = r"(?:\+|x|\u00d7|\*|\u00f7|plus|add(?:ed to)?|times|multiplied by|divided by|minus|take away|subtract)"
_DASH = r"(?:-|\u2010|\u2011|\u2012|\u2013|\u2014|\u2212)"
MATH_CHECK_RE = re.compile(
    rf"(?<![\w.]){_NUM}\s*{_MATH_OP}\s*{_NUM}(?![\w.])|what(?:'s|\u2019s| is) {_NUM}\b|\bsum of {_NUM}\b", re.I)
# A dash between numbers is also how ranges are written ("Age (18 - 99)"), so it's a sum only when one is asked for.
MATH_DASH_RE = re.compile(rf"(?<![\w.]){_NUM}\s*{_DASH}\s*{_NUM}(?![\w.])", re.I)
MATH_CUE_RE = re.compile(r"=|\bequals?\b|\bwhat(?:'s|\u2019s| is)\b|\?|\bsolve\b|\bsum\b|\banswer\b|\bcalculat", re.I)
# Codes copied from a picture and similar "prove you're human" boxes (The Times: "The security code does not match").
CODE_CHECK_RE = re.compile(
    r"security code|verification code|captcha|enter the (?:code|characters|letters|text|word)s? (?:shown|above|below|"
    r"you see|(?:(?:seen|shown|displayed|written) )?(?:in|on) the (?:image|picture|box))|type the (?:code|characters|"
    r"letters|text)|anti ?-?spam|spam (?:check|protection|question)|human verification|are you (?:a )?human|"
    r"prove you(?:'re| are) (?:human|not a robot)", re.I)
# A box labelled just "Answer" or "Code" under "Enter the characters seen in the image below" (At The Races, My
# Switzerland): the words around it say it's a picture CAPTCHA. Not a mere "protected by reCAPTCHA" notice.
CODE_CONTEXT_RE = re.compile(
    r"enter the (?:characters|letters|code|text|word)s?\b.{0,40}\b(?:image|picture|below|above|shown)|type the "
    r"(?:characters|letters|code|text)\b|audio captcha|characters? (?:seen|shown|displayed) (?:in|on) the|"
    r"security code", re.I)
GENERIC_AUTOCOMPLETE = {"full_name", "address1"}


def _raw_label(f: Field) -> str:
    return re.sub(r"\s+", " ", " ".join(p for p in (f.label, _placeholder(f), f.aria) if p)).strip().lower()


def is_math_check(f: Field, ctx: Ctx | None = None) -> bool:
    raw = _raw_label(f)
    if not raw or (ctx and ctx.answers and len(raw.split()) > 10):
        return False  # a long quiz question that happens to contain numbers
    return bool(MATH_CHECK_RE.search(raw) or (MATH_DASH_RE.search(raw) and MATH_CUE_RE.search(raw)))


def is_code_check(f: Field) -> bool:
    if CODE_CHECK_RE.search(_raw_label(f)):
        return True
    core = normalise(f.label or f.placeholder or f.aria)
    return len(core.split()) <= 3 and bool(CODE_CONTEXT_RE.search(f.context or ""))


# Date boxes: "DD/MM/YYYY", "MM/DD" (Klaviyo's birthday), "YYYY-MM-DD" in the placeholder or label.
DATE_PATTERN_RE = re.compile(r"\b(dd|mm|yyyy|yy)\s*([/\-. ])\s*(dd|mm|yyyy|yy)(?:\s*\2\s*(dd|mm|yyyy|yy))?\b", re.I)
DATE_TOKENS = {"dd": "%d", "mm": "%m", "yyyy": "%Y", "yy": "%y"}
# One box of a date split into three ("DD" [ ] "MM" [ ] "YYYY" [ ] under "Date of birth").
DOB_PARTS = {"dd": "dob_day", "day": "dob_day", "mm": "dob_month", "month": "dob_month", "yyyy": "dob_year",
             "yy": "dob_year", "year": "dob_year"}
BIRTH_RE = re.compile(r"birth|\bdob\b|\bbday\b|\bborn\b|d\.?o\.?b")


def date_format(hint: str) -> str | None:
    """The strftime format a date box asks for, from its placeholder or label ("MM/DD" -> "%m/%d"), else None."""
    m = DATE_PATTERN_RE.search(hint or "")
    if not m:
        return None
    tokens = [t.lower() for t in (m.group(1), m.group(3), m.group(4)) if t]
    if len({t[0] for t in tokens}) != len(tokens):
        return None  # "dd dd" etc.
    return m.group(2).join(DATE_TOKENS[t] for t in tokens)


def _date_hint(f: Field) -> str:
    return " ".join(p for p in (f.placeholder, f.label, f.aria) if p)


def _label_text(f: Field) -> str:
    return normalise(" ".join(p for p in [f.label, _placeholder(f), f.aria] if p))


def _visible_profile_key(f: Field) -> str | None:
    """The profile detail named by what the field shows you (its label or placeholder), if any."""
    vis = normalise(" ".join(p for p in (f.label, _placeholder(f)) if p))
    core = re.sub(r"\s+", " ", re.sub(r"[*?:()]+|\brequired\b", " ", vis)).strip()
    if not core or OTHER_PERSON_RE.search(core):
        return None
    return SPLIT_NAME.get(core) or _key_from_text(core)


def _key_from_text(d: str) -> str | None:
    for key, pat in KEY_PATTERNS:
        if pat.search(d):
            return key
    return None


# A question about you, not a quiz: "Which county do you live in?", "Where are you from?".
ABOUT_YOU_RE = re.compile(r"\b(?:do|did) you (?:live|reside|come from|currently live)\b|\bwhere (?:do|did|are) you\b|"
                          r"\bare you (?:from|based)\b")


def _quiz_label(f: Field) -> bool:
    """A visible label that's a quiz question ("Which county does Bakewell pudding originate from?")."""
    vis = normalise(f.label or "")
    raw = (f.label or "").strip().rstrip("* ").strip()
    if not vis or PERSONAL_Q_RE.search(vis) or ABOUT_YOU_RE.search(vis):
        return False
    return raw.endswith("?") and bool(QUESTION_START_RE.match(vis)) and len(vis.split()) >= 4


def detect_profile_key(f: Field) -> str | None:
    label = _label_text(f)
    core = re.sub(r"[*?:()]+|\brequired\b", " ", label)
    core = re.sub(r"\s+", " ", core).strip()
    if label and OTHER_PERSON_RE.search(label):
        return None
    if _quiz_label(f):
        return None  # "Which county does Bakewell pudding originate from?" is a quiz, not your county
    if core in SPLIT_NAME:
        return SPLIT_NAME[core]
    d = f.desc
    birthish = bool(BIRTH_RE.search(d) or BIRTH_RE.search(normalise(f.context[:300])))
    ph = re.sub(r"\s+", " ", re.sub(r"[*?:()]+", " ", normalise(f.placeholder))).strip()
    part = DOB_PARTS.get(ph) or DOB_PARTS.get(core)
    if part and birthish and f.type not in ("email", "tel"):
        return part  # one box of a date of birth split into three (Cornwall Living)
    # A whole date in the format its placeholder shows ("MM/DD" birthday, 58 & Co), or a "DD-MM-YYYY" box whose only
    # words are "Minimum age: 18" (Visa Cash App RB's Jotform date of birth).
    agey = bool(re.search(r"minimum age|\bage\b|\baged\b|over 1[68]|\b1[68] ?\+", normalise(f.label + " " + f.context[:300])))
    if f.type != "date" and (birthish or agey) and date_format(_date_hint(f)):
        return "dob"
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
    # The last part of the field's name says the most ("dwfrm_billing_billingAddress_country" is a country).
    tail = _name_tail(f)
    tail_key = (SPLIT_NAME.get(tail) or _key_from_text(tail)) if tail else None
    return tail_key or _key_from_text(d)


def _name_tail(f: Field) -> str:
    for raw in (f.name, f.id):
        parts = [p for p in re.split(r"[\[\]_.\-]+|(?<=[a-z])(?=[A-Z])", raw or "") if p]
        if parts:
            return normalise(parts[-1])
    return ""


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
        if f and f.type == "date":
            return dob.isoformat()
        return dob.strftime((date_format(_date_hint(f)) if f else None) or "%d/%m/%Y")
    if key in ("dob_day", "dob_month", "dob_year"):
        if not dob:
            return ""
        hint = _date_hint(f).lower() if f else ""
        if key == "dob_day":
            return f"{dob.day:02d}" if re.search(r"\bdd\b", hint) else str(dob.day)
        if key == "dob_month":
            return f"{dob.month:02d}" if re.search(r"\bmm\b", hint) else str(dob.month)
        if re.search(r"\byy\b", hint) and not re.search(r"\byyyy\b", hint):
            return f"{dob.year % 100:02d}"
        return str(dob.year)
    if key == "age":
        if not dob:
            return ""
        t = date.today()
        return str(t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day)))
    if key == "country":
        return p.get("country") or "United Kingdom"
    if key == "address3":
        return ""
    if key == "address1" and f is not None and f.tag == "textarea" and not re.search(
            r"line ?1|\b1\b|street|house|building|road|first line", _label_text(f)):
        # One box for the whole address (Love It!'s "Your Address"): line 1 alone wouldn't get a prize delivered.
        return "\n".join(str(p.get(k)).strip() for k in ("address1", "address2", "town", "county", "postcode")
                         if str(p.get(k) or "").strip())
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
COUNTRY_NAMES = {
    "united kingdom", "uk", "great britain", "ireland", "france", "germany", "spain", "italy", "portugal", "belgium",
    "netherlands", "luxembourg", "austria", "switzerland", "denmark", "sweden", "norway", "finland", "iceland",
    "poland", "czech republic", "czechia", "slovakia", "hungary", "romania", "bulgaria", "greece", "croatia",
    "slovenia", "estonia", "latvia", "lithuania", "cyprus", "malta", "united states", "usa", "canada", "australia",
    "new zealand", "india", "china", "japan", "brazil", "mexico", "south africa", "afghanistan", "albania", "algeria"}


def looks_like_country_list(options: list[dict]) -> bool:
    texts = {normalise(o.get("text", "")) for o in options}
    return len(texts & COUNTRY_NAMES) >= 6


def dob_part_from_options(options: list[dict]) -> str | None:
    """Which part of a date a list of choices is: days (1-31), months (January... or 1-12) or years (1900-2100)."""
    texts = [normalise(o.get("text", "")) for o in options if normalise(o.get("text", ""))]
    if not texts:
        return None
    if sum(1 for t in texts if t in MONTHS or t in {m[:3] for m in MONTHS}) >= 10:
        return "dob_month"
    nums = [int(t) for t in texts if t.isdigit()]
    if len(nums) < 0.8 * len(texts):
        return None
    if all(1900 <= n <= 2100 for n in nums):
        return "dob_year"
    if max(nums) <= 12 and len(nums) <= 12:
        return "dob_month"
    if max(nums) <= 31 and len(nums) >= 28:
        return "dob_day"
    return None


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
    elif is_mkt and is_terms and OPTIN_LEAD_RE.search(label) and not TERMS_CORE_RE.search(
            normalise(re.split(r"(?<=[.!?])\s+", f.label.strip(), maxsplit=1)[0])):
        is_terms = False  # Purina: "Click if you would like to receive offers... You confirm that you are over 18."
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
    if looks_like_country_list(real):
        # A list of countries is a country list, whatever its label or name: EMP's "Please choose" list is named
        # dwfrm_billing_billingAddress_country, and "Address" made it address line 1 (then Austria was guessed).
        key = "country"
    if key == "dob" and kind == "select":
        # One list of a date of birth split into three, labelled only "Date of Birth" (Johansens' Day list).
        key = dob_part_from_options(real) or key
    if key == "age" or AGE_GROUP_RE.search(label_d):
        # "Age: 18-24 / 25-34 / ..." is answered from your date of birth, never guessed (the AI picked 18-24, 58 & Co).
        bands = [(o, age_band(o.get("text", ""))) for o in real]
        if any(b for _, b in bands):
            age = profile_age(ctx.profile)
            if age is None:
                return Action("unknown", note="profile has no date of birth (for the age group)")
            o = next((o for o, b in bands if b and b[0] <= age <= b[1]), None)
            if o:
                return Action(kind, o.get("idx") or o.get("value"), source="age", note="age group from your date of birth")
            return Action("unknown" if f.required else "skip", note="no age group for your age")
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
    if kind == "select" and key is None and not looks_like_question(f) and not ctx.question:
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
        # A stray aria-label or title ("Enter search query" on every one of Purina's boxes) doesn't outweigh a visible
        # "First name" label - unless the field's own label, placeholder or name says to skip it too, or it's a trap
        # for bots ("leave this blank").
        inner = normalise(" ".join(p for p in (f.label, f.placeholder, f.name, f.id) if p))
        key = None if SKIP_RE.search(inner) or HONEYPOT_RE.search(d) else _visible_profile_key(f)
        if not key:
            return Action("skip", note="not needed")
    else:
        key = detect_profile_key(f)
    if key:
        val = profile_value(key, ctx.profile, f)
        if val or key == "address3":
            return Action("fill", val, source=key) if val else Action("skip", note="address line 3")
        if key == "address2" and f.required and (ctx.profile.get("town") or "").strip():
            # A required second address line, and your address has none: your town is the usual stand-in.
            return Action("fill", ctx.profile["town"].strip(), source="town", note="address line 2 is required")
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
    if is_math_check(f, ctx):
        # "9 + 3 equals" is a CAPTCHA by another name, as a box or a list of answers.
        return Action("unknown", note="captcha: maths anti-spam check")
    if f.tag in ("input", "textarea") and is_code_check(f):
        return Action("unknown", note="captcha: a security code to copy")
    if f.tag in ("select", "radio"):
        return map_choice(f, ctx)
    return map_text(f, ctx)


AGE_GROUP_RE = re.compile(r"^age\b|\bage ?(group|range|bracket|band)s?\b|how old")


def profile_age(p: dict) -> int | None:
    dob = _parse_dob(p)
    if not dob:
        return None
    t = date.today()
    return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))


def age_band(text: str) -> tuple[int, int] | None:
    """'18-24' / '25 to 34' / '65+' / '55 and over' / 'Under 18' -> (lowest, highest) age, else None."""
    t = (text or "").lower()
    m = re.search(r"(\d{1,3})\s*(?:-|\u2010|\u2011|\u2012|\u2013|\u2014|\u2212|to)\s*(\d{1,3})", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,3})\s*\+|(\d{1,3})\s*(?:years?|yrs?)?\s*(?:and|or|&)\s*(?:over|above|older|up)|"
                  r"(?:over|above|older than)\s*(\d{1,3})", t)
    if m:
        return int(next(g for g in m.groups() if g)), 200
    m = re.search(r"(?:under|below|less than|younger than)\s*(\d{1,3})", t)
    if m:
        return 0, int(m.group(1)) - 1
    m = re.fullmatch(r"\s*(\d{1,3})\s*", t)
    return (int(m.group(1)), int(m.group(1))) if m else None


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
