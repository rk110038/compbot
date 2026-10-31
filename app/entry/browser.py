"""Playwright-driven web form entry.

Deliberately honest automation: no CAPTCHA solving, no stealth/bot-detection evasion, no social media logins.
Anything that needs those is handed to the manual queue with a reason.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from .. import llm
from ..config import get_config
from ..netguard import guarded_client, is_public_url
from .forms import PROFILE_KEYS, Action, Ctx, Field, apply_llm_action, find_automation_clause, plan_form

log = logging.getLogger("compbot.browser")


@dataclass
class AttemptResult:
    result: str               # entered | unverified | dry_run_ok | failed | captcha | blocked | tc_blocked | needs_input | closed | error
    message: str = ""
    details: dict = field(default_factory=dict)
    screenshot_before: str | None = None
    screenshot_after: str | None = None
    final_url: str | None = None


# --------------------------------------------------------------------------- page scripts

JS_HELPERS = r"""
const __cbVis = (el) => {
  if (!el || !el.getBoundingClientRect) return false;
  const s = getComputedStyle(el);
  if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') < 0.05) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;
  if (r.right + window.scrollX < 0 || r.bottom + window.scrollY < 0) return false;
  let p = el.parentElement;
  while (p && p !== document.body) {
    const ps = getComputedStyle(p);
    if (ps.display === 'none' || ps.visibility === 'hidden' || parseFloat(ps.opacity || '1') < 0.05) return false;
    p = p.parentElement;
  }
  return true;
};
const __cbTxt = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
"""

JS_EXTRACT = "(prefix) => {\n" + JS_HELPERS + r"""
  const vis = __cbVis, txt = __cbTxt;
  const labelFor = (el) => {
    let t = '';
    if (el.labels && el.labels.length) t = Array.from(el.labels).map(txt).join(' ');
    if (!t) { const lb = el.getAttribute('aria-labelledby'); if (lb) t = lb.split(/\s+/).map(id => txt(document.getElementById(id))).join(' '); }
    if (!t) { const c = el.closest('label'); if (c) t = txt(c); }
    if (!t) {
      let prev = el.previousElementSibling, hops = 0;
      while (prev && hops < 3 && !t) {
        const pt = txt(prev);
        if (pt && pt.length < 200 && !prev.querySelector('input,select,textarea')) t = pt;
        prev = prev.previousElementSibling; hops++;
      }
    }
    if (!t && el.parentElement) { const pt = txt(el.parentElement); if (pt.length < 150) t = pt; }
    return t.slice(0, 300);
  };
  const ctxText = (el) => { let n = el.parentElement; for (let i = 0; i < 4 && n; i++) { const t = txt(n); if (t.length > 40) return t.slice(0, 400); n = n.parentElement; } return ''; };
  const isReq = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$|\(required\)/i.test(label || '');
  const choiceVis = (el) => vis(el) || (el.labels && Array.from(el.labels).some(vis)) || vis(el.closest('label')) || vis(el.parentElement);
  let counter = 0;
  const all = Array.from(document.querySelectorAll('[data-cb-idx]')).map(e => e.getAttribute('data-cb-idx'));
  for (const a of all) { const n = parseInt(String(a).split('_').pop(), 10); if (!isNaN(n) && n >= counter) counter = n + 1; }
  const forms = Array.from(document.querySelectorAll('form'));
  const roots = forms.concat([document.body]);
  const out = [];
  for (const root of roots) {
    if (!root) continue;
    const fields = [], groups = {};
    for (const el of root.querySelectorAll('input, select, textarea')) {
      if (root === document.body && el.form) continue;
      const type = (el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text') : el.tagName).toLowerCase();
      if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
      if (el.disabled || el.readOnly) continue;
      const choice = type === 'radio' || type === 'checkbox';
      if (choice ? !choiceVis(el) : !vis(el)) continue;
      let idx = el.getAttribute('data-cb-idx');
      if (!idx) { idx = prefix + (counter++); el.setAttribute('data-cb-idx', idx); }
      const label = labelFor(el);
      if (type === 'radio') {
        const key = el.name || idx;
        if (!groups[key]) {
          let gl = '';
          const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) gl = txt(lg); }
          const rg = el.closest('[role=radiogroup]');
          if (!gl && rg) { gl = rg.getAttribute('aria-label') || ''; const lb = rg.getAttribute('aria-labelledby'); if (!gl && lb) gl = txt(document.getElementById(lb)); }
          if (!gl) {
            let n = el.parentElement;
            for (let i = 0; i < 6 && n; i++) {
              const rs = Array.from(n.querySelectorAll('input[type=radio]'));
              if (rs.length > 1 && rs.every(r => r.name === el.name)) {
                const pt = txt(n.previousElementSibling);
                gl = (pt && pt.length < 300) ? pt : txt(n).slice(0, 300);
                break;
              }
              n = n.parentElement;
            }
          }
          groups[key] = {idx, tag: 'radio', type: 'radio', name: el.name || '', id: el.id || '', label: gl,
            placeholder: '', autocomplete: '', aria: el.getAttribute('aria-label') || '', required: false,
            options: [], context: ctxText(el), maxlength: null, checked: false};
          fields.push(groups[key]);
        }
        groups[key].options.push({value: el.value, text: label || el.value, idx});
        if (isReq(el, groups[key].label)) groups[key].required = true;
        if (el.checked) groups[key].checked = true;
        continue;
      }
      const f = {idx, tag: el.tagName.toLowerCase(), type, name: el.name || '', id: el.id || '', label,
        placeholder: el.getAttribute('placeholder') || '', autocomplete: el.getAttribute('autocomplete') || '',
        aria: el.getAttribute('aria-label') || '', required: isReq(el, label), options: [], context: ctxText(el),
        maxlength: el.maxLength > 0 ? el.maxLength : null, checked: !!el.checked};
      if (el.tagName === 'SELECT') f.options = Array.from(el.options).map(o => ({value: o.value, text: (o.text || '').trim()}));
      fields.push(f);
    }
    const btns = Array.from(root.querySelectorAll('button, input[type=submit], input[type=image], [role=button], a.button, a.btn')).filter(vis);
    let heading = '';
    if (root !== document.body) {
      let n = root;
      outer: for (let i = 0; i < 4 && n; i++) {
        let p = n.previousElementSibling, hops = 0;
        while (p && hops < 4) {
          const h = p.matches('h1,h2,h3,h4,legend') ? p : p.querySelector('h1,h2,h3,h4');
          if (h) { heading = txt(h).slice(0, 160); break outer; }
          p = p.previousElementSibling; hops++;
        }
        n = n.parentElement;
      }
    }
    const meta = (root === document.body) ? '' : ((root.id || '') + ' ' + (typeof root.className === 'string' ? root.className : '') + ' ' + (root.getAttribute('action') || '')).toLowerCase();
    out.push({isForm: root.tagName === 'FORM', fields, hasPassword: !!root.querySelector('input[type=password]'),
      submitTexts: btns.map(b => (txt(b) || b.value || b.getAttribute('aria-label') || '').slice(0, 60)),
      text: txt(root).slice(0, 1500), heading, meta,
      inFooter: root !== document.body && !!root.closest('footer, [class*=footer i], [id*=footer i]')});
  }
  return out;
}
"""

JS_MARK_SUBMIT = "(idx) => {\n" + JS_HELPERS + r"""
  const vis = __cbVis, txt = __cbTxt;
  document.querySelectorAll('[data-cb-submit]').forEach(e => e.removeAttribute('data-cb-submit'));
  const el = document.querySelector(`[data-cb-idx="${idx}"]`);
  let root = el && el.form ? el.form : null;
  if (!root && el) { let n = el.parentElement; while (n && !n.querySelector('button, input[type=submit], [role=button]')) n = n.parentElement; root = n; }
  if (!root) root = document.body;
  const good = /^(enter|submit|send|sign ?up|subscribe|register|join|go|count me in|take part|enter now|enter competition|enter the (competition|draw|prize draw)|submit entry|enter draw|i'?m in|done|continue|next|confirm|win|enter to win|let'?s go|play)\b/i;
  const bad = /cancel|reset|back|close|log ?in|sign ?in|search|menu|cookie|accept all|reject|share|facebook|twitter|instagram|print|basket|cart/i;
  let best = null, bestScore = -99;
  for (const b of root.querySelectorAll('button, input[type=submit], input[type=image], [role=button], a.button, a.btn')) {
    if (!vis(b)) continue;
    const t = (txt(b) || b.value || b.getAttribute('aria-label') || '').trim();
    let s = 0;
    if ((b.getAttribute('type') || '').toLowerCase() === 'submit' || b.tagName === 'INPUT') s += 3;
    if (good.test(t)) s += 4;
    if (bad.test(t)) s -= 6;
    if (b.tagName === 'A') s -= 1;
    if (s > bestScore) { best = b; bestScore = s; }
  }
  if (!best || bestScore < 0) return null;
  best.setAttribute('data-cb-submit', '1');
  return (txt(best) || best.value || 'submit').slice(0, 60);
}
"""

JS_SET_CHECKED = r"""
([idx, want]) => {
  const el = document.querySelector(`[data-cb-idx="${idx}"]`);
  if (!el) return false;
  if (el.checked !== want) { const l = el.labels && el.labels[0]; (l || el).click(); }
  if (el.checked !== want && el.type === 'checkbox') { el.click(); }
  if (el.checked !== want) {
    el.checked = want;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  return el.checked === want;
}
"""

JS_VALIDITY = r"""
(idx) => {
  const el = document.querySelector(`[data-cb-idx="${idx}"]`);
  const form = el && el.form;
  if (!form) return [];
  return Array.from(form.elements).filter(e => e.willValidate && !e.checkValidity())
    .map(e => ((e.labels && e.labels[0] ? e.labels[0].innerText : '') || e.name || e.id || e.type).trim().slice(0, 80));
}
"""

JS_COOKIES = "() => {\n" + JS_HELPERS + r"""
  const vis = __cbVis;
  const ids = ['onetrust-reject-all-handler', 'onetrust-accept-btn-handler', 'CybotCookiebotDialogBodyButtonDecline',
    'CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll', 'didomi-notice-disagree-button', 'didomi-notice-agree-button',
    'truste-consent-required', 'truste-consent-button', 'cookie-reject', 'cookie-accept'];
  for (const id of ids) { const b = document.getElementById(id); if (b && vis(b)) { b.click(); return id; } }
  const reject = /^(reject all|reject|reject all cookies|decline|decline all|necessary only|only necessary|essential only|use necessary cookies only|refuse all|deny|disagree|continue without accepting)$/i;
  const accept = /^(accept all|accept|accept all cookies|accept cookies|i accept|agree|i agree|allow all|allow all cookies|allow cookies|got it|ok|okay|agree and close|accept & close|accept and close|yes,? i'?m happy)$/i;
  const banner = '[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],[id*=gdpr i],[class*=gdpr i],[class*=cmp i],[id*=cmp i],[aria-label*=cookie i],[aria-label*=consent i],[id*=privacy i],[class*=privacy i],[role=dialog],[aria-modal=true]';
  const framey = /sp_message|consent|cookie|privacy-mgmt|cmp/i.test(location.href);
  const btns = Array.from(document.querySelectorAll('button, [role=button], a, input[type=button]')).filter(vis);
  for (const re of [reject, accept]) {
    for (const b of btns) {
      const t = (b.innerText || b.value || '').replace(/\s+/g, ' ').trim();
      if (re.test(t) && (framey || b.closest(banner))) { b.click(); return t; }
    }
  }
  return null;
}
"""

JS_CAPTCHA = r"""
() => {
  const sels = ['iframe[src*="recaptcha/api2/anchor"]', 'iframe[src*="recaptcha/enterprise/anchor"]',
    'iframe[src*="recaptcha/api2/bframe"]', 'iframe[src*="hcaptcha.com"]', 'iframe[src*="challenges.cloudflare.com"]',
    '.g-recaptcha:not([data-size="invisible"])', '.h-captcha', '.cf-turnstile', 'iframe[title*="captcha" i]',
    '#px-captcha', '.frc-captcha', '[data-sitekey]:not([data-size="invisible"])', '#captcha', '.captcha'];
  for (const s of sels) {
    for (const el of document.querySelectorAll(s)) {
      const r = el.getBoundingClientRect(), st = getComputedStyle(el);
      if (r.width > 30 && r.height > 30 && st.visibility !== 'hidden' && st.display !== 'none') return s;
    }
  }
  const head = (document.title + ' ' + (document.body ? document.body.innerText.slice(0, 600) : '')).toLowerCase();
  if (/just a moment|verify you are human|checking your browser|are you a robot|attention required|access denied|request blocked|unusual traffic|verification failed|403 forbidden|pardon our interruption/.test(head)) return 'challenge-page';
  return null;
}
"""

JS_TERMS_LINKS = r"""
() => Array.from(document.querySelectorAll('a[href]'))
  .filter(a => /terms|t\s*&\s*c|t&cs|rules|conditions/i.test((a.innerText || '') + ' ' + a.getAttribute('href')))
  .map(a => a.href).filter(h => /^https?:/i.test(h)).slice(0, 3)
"""

JS_REVEAL = "(linksOnly) => {\n" + JS_HELPERS + r"""
  const vis = __cbVis, txt = __cbTxt;
  const re = /^(enter( now| here| the competition| competition| the (prize )?draw| to win| now for free)?|click (here )?to enter|take part|get started|start|begin|play now|enter giveaway|i want to win)!?$/i;
  for (const b of document.querySelectorAll('button, a, [role=button], input[type=button]')) {
    if (!vis(b)) continue;
    // Never press anything that could submit a form; in dry-run only follow plain links.
    if (b.closest('form') || (b.getAttribute('type') || '').toLowerCase() === 'submit') continue;
    if (linksOnly && !(b.tagName === 'A' && /^https?:/i.test(b.href || ''))) continue;
    const t = (txt(b) || b.value || '').trim();
    if (re.test(t)) { b.setAttribute('data-cb-reveal', '1'); return t; }
  }
  return null;
}
"""

SUCCESS_RE = re.compile(
    r"thank(?:s| you)\b.{0,40}\b(?:enter|entry|entering|taking part|submitting|submission|signing up|subscrib|"
    r"registering|participat)|(?:entry|submission|details) (?:has been |have been |was )?(?:received|submitted|"
    r"confirmed|successful|complete)|you(?:'re| are| have been) (?:now )?(?:entered|in the (?:prize )?draw|"
    r"successfully)|successfully (?:entered|submitted|subscribed|registered|signed up)|good luck|check your "
    r"(?:e-?mail|inbox)|we(?:'ve| have) received your|confirmation e-?mail|you(?:'re| are) in\b|almost there|"
    r"nearly there|please confirm your|thanks for signing up|thank you for subscribing|thank you!?$", re.I | re.M)
ALREADY_RE = re.compile(
    r"already (?:been )?entered|already (?:subscribed|registered|signed up|a subscriber)|only (?:one|1) entry|you "
    r"have already|duplicate entry|already taken part|already on (?:our|the) list", re.I)
ERROR_RE = re.compile(
    r"(?:is|are) required|this field is required|please (?:enter|fill|complete|select|check|provide|tick|accept)|"
    r"\binvalid\b|not (?:a )?valid|something went wrong|please try again|must (?:be|accept|agree)|error", re.I)
CLOSED_RE = re.compile(
    r"(?:competition|promotion|prize draw|giveaway|this comp\w*) (?:is |has )?(?:now )?(?:closed|ended|finished)|"
    r"no longer (?:accepting|open|available)|entries (?:are|have) (?:now )?closed|has now closed|this competition "
    r"has expired", re.I)


# --------------------------------------------------------------------------- helpers

def _all_frames(page):
    return [f for f in page.frames if not f.is_detached()]


def _safe_eval(frame, script, arg=None, default=None):
    try:
        return frame.evaluate(script, arg) if arg is not None else frame.evaluate(script)
    except Exception as exc:  # noqa: BLE001
        log.debug("evaluate failed in %s: %s", getattr(frame, "url", "?"), exc)
        return default


def _page_text(page) -> str:
    parts = []
    for fr in _all_frames(page):
        t = _safe_eval(fr, "() => document.body ? document.body.innerText : ''", default="")
        if t:
            parts.append(t)
    return "\n".join(parts)


def dismiss_cookies(page) -> list[str]:
    clicked = []
    for _ in range(2):
        for fr in _all_frames(page):
            r = _safe_eval(fr, JS_COOKIES)
            if r:
                clicked.append(str(r))
        if clicked:
            time.sleep(1.2)
        else:
            break
    return clicked


def detect_captcha(page) -> str | None:
    for fr in _all_frames(page):
        r = _safe_eval(fr, JS_CAPTCHA)
        if r:
            return r
    return None


ENTRY_HEADING_RE = re.compile(
    r"enter (?:this|the|our) (?:competition|comp|prize draw|giveaway|draw)|entry form|enter (?:now|here) to win|"
    r"competition entry|enter (?:the )?competition|how to enter|to enter", re.I)
NEWSLETTER_RE = re.compile(
    r"newsletter|subscribe|mailing ?list|sign up (?:for|to) (?:our|the)|straight to your inbox|latest .{0,40}inbox|"
    r"klaviyo|mailchimp|mc-embedded", re.I)


def _score_container(c: dict, ctx: Ctx | None = None) -> float:
    fields = c.get("fields") or []
    if not fields:
        return -100
    s = min(len(fields), 15)
    if c.get("hasPassword"):
        s -= 25
    joined = " ".join((f.get("label", "") + " " + f.get("name", "") + " " + f.get("placeholder", "") + " " +
                       f.get("type", "")).lower() for f in fields)
    if re.search(r"e-?mail", joined):
        s += 6
    if re.search(r"name", joined):
        s += 2
    if any(re.search(r"enter|submit|send|sign ?up|subscribe|register|join|count me in|take part|win|next", t, re.I)
           for t in c.get("submitTexts", [])):
        s += 3
    if re.search(r"competition|prize|giveaway|\bwin\b|enter|draw", c.get("text", ""), re.I):
        s += 2
    if ENTRY_HEADING_RE.search(c.get("heading", "") + " " + c.get("text", "")[:400]):
        s += 6
    if ctx and ctx.answers and any(re.search(r"answer|question|\?\s*\*?$", f.get("label", ""), re.I) for f in fields):
        s += 6
    newsletterish = NEWSLETTER_RE.search(c.get("meta", "") + " " + c.get("heading", "") + " " + c.get("text", "")[:300])
    if newsletterish:
        s -= 1 if (ctx and ctx.is_newsletter) else 6
    if c.get("inFooter"):
        s -= 6
    if len(fields) == 1 and re.search(r"search|^q$", fields[0].get("name", "") + " " + fields[0].get("type", ""), re.I):
        s -= 25
    if not c.get("isForm"):
        s -= 1
    return s


def find_best_form(page, ctx: Ctx | None = None, gen: int = 0) -> tuple[object, dict, float] | None:
    best = None
    for i, fr in enumerate(_all_frames(page)):
        containers = _safe_eval(fr, JS_EXTRACT, f"g{gen}f{i}_", default=[]) or []
        for c in containers:
            sc = _score_container(c, ctx)
            if best is None or sc > best[2]:
                best = (fr, c, sc)
    return best


def fetch_terms_text(page) -> str:
    links: list[str] = []
    for fr in _all_frames(page):
        links += _safe_eval(fr, JS_TERMS_LINKS, default=[]) or []
    texts = []
    with guarded_client(headers={"User-Agent": get_config().user_agent}, timeout=15) as client:
        for href in list(dict.fromkeys(links))[:3]:
            try:
                resp = client.get(href)
                ctype = (resp.headers.get("content-type") or "").lower()
                if resp.is_success and "html" in ctype:
                    texts.append(BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)[:200000])
            except Exception as exc:  # noqa: BLE001
                log.debug("terms fetch failed %s: %s", href, exc)
    return "\n".join(texts)


def _to_fields(container: dict) -> list[Field]:
    out = []
    for f in container.get("fields", []):
        out.append(Field(
            idx=f["idx"], tag=f["tag"], type=f.get("type", "text"), name=f.get("name", ""), id=f.get("id", ""),
            label=f.get("label", ""), placeholder=f.get("placeholder", ""), autocomplete=f.get("autocomplete", ""),
            aria=f.get("aria", ""), required=bool(f.get("required")), options=f.get("options") or [],
            context=f.get("context", ""), maxlength=f.get("maxlength"), checked=bool(f.get("checked")),
        ))
    return out


def _apply(frame, f: Field, a: Action) -> bool:
    loc = frame.locator(f'[data-cb-idx="{f.idx}"]').first
    try:
        if a.kind == "fill":
            try:
                loc.fill(a.value or "", timeout=6000)
            except Exception:  # noqa: BLE001
                loc.click(timeout=4000)
                loc.press_sequentially(a.value or "", delay=20, timeout=15000)
            return True
        if a.kind == "select":
            loc.select_option(value=a.value, timeout=6000)
            return True
        if a.kind == "radio":
            target = frame.locator(f'[data-cb-idx="{a.value}"]').first
            try:
                target.check(timeout=4000)
            except Exception:  # noqa: BLE001
                return bool(_safe_eval(frame, JS_SET_CHECKED, [a.value, True], default=False))
            return True
        if a.kind in ("check", "uncheck"):
            want = a.kind == "check"
            if f.checked == want:
                return True
            try:
                loc.set_checked(want, timeout=4000)
                return True
            except Exception:  # noqa: BLE001
                return bool(_safe_eval(frame, JS_SET_CHECKED, [f.idx, want], default=False))
    except Exception as exc:  # noqa: BLE001
        log.debug("apply failed for %s: %s", f.human_label, exc)
        return False
    return True


def _shot(page, path: Path) -> str | None:
    try:
        page.screenshot(path=str(path), full_page=True, type="jpeg", quality=55, timeout=20000)
        return path.name
    except Exception:  # noqa: BLE001
        try:
            page.screenshot(path=str(path), type="jpeg", quality=55, timeout=10000)
            return path.name
        except Exception:  # noqa: BLE001
            return None


def _wait_settle(page, ms: int = 8000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- main entry routine

class Browser:
    """One Chromium instance reused for a whole entry run."""

    def __init__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=get_config().headless, args=["--disable-dev-shm-usage"])

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def enter(self, url: str, ctx: Ctx, settings: dict, shot_prefix: str, comp_context: dict) -> AttemptResult:
        cfg = get_config()
        dry_run = settings.get("entry_mode", "dry_run") != "auto"
        res = AttemptResult(result="error")
        details: dict = {"steps": [], "fields": []}
        if not is_public_url(url):
            res.result, res.message = "blocked", f"Refused to open a non-public address: {url[:200]}"
            res.details = details
            return res
        context = self.browser.new_context(locale="en-GB", timezone_id=cfg.tz, viewport={"width": 1280, "height": 1500})
        context.set_default_timeout(15000)
        # Untrusted pages must never reach the NAS/LAN: abort any request (incl. subresources/redirects) to them.
        context.route("**/*", lambda route: route.continue_() if is_public_url(route.request.url, allow_inline=True)
                      else route.abort("blockedbyclient"))
        page = context.new_page()
        gen = [0]
        checked_urls: set[str] = set()

        def best_form():
            gen[0] += 1
            return find_best_form(page, ctx, gen[0])

        def clause_check() -> str | None:
            """Automation-ban check for the current page (once per URL)."""
            if not settings.get("respect_automation_clauses", True):
                return None
            key = page.url.split("#")[0]
            if key in checked_urls:
                return None
            checked_urls.add(key)
            return find_automation_clause(_page_text(page)) or find_automation_clause(fetch_terms_text(page))

        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if resp is not None and resp.status in (401, 403, 429):
                res.result, res.message = "blocked", f"Site refused automated access (HTTP {resp.status})"
                res.final_url = page.url
                res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_blocked.jpg")
                return res
            if resp is not None and resp.status == 404:
                res.result, res.message = "closed", "Competition page not found (HTTP 404)"
                return res
            _wait_settle(page)
            details["cookies"] = dismiss_cookies(page)
            res.final_url = page.url

            cap = detect_captcha(page)
            if cap == "challenge-page":
                res.result, res.message = "blocked", "Site showed a bot-check / access-denied page"
                res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_blocked.jpg")
                return res

            clause = clause_check()
            if clause:
                res.result = "tc_blocked"
                res.message = f"T&Cs ban automated entries: \u201c{clause[:300]}\u201d"
                return res

            best = best_form()
            if not best or best[2] < 4:
                # Click an "Enter now"-style reveal. Never a submit button; in dry-run only plain links.
                for fr in _all_frames(page):
                    label = _safe_eval(fr, JS_REVEAL, dry_run)
                    if label:
                        details["steps"].append(f"clicked '{label}'")
                        n_pages = len(context.pages)
                        try:
                            fr.locator('[data-cb-reveal="1"]').first.click(timeout=5000)
                        except Exception:  # noqa: BLE001
                            pass
                        time.sleep(2.5)
                        if len(context.pages) > n_pages:
                            page = context.pages[-1]
                            page.wait_for_load_state("domcontentloaded")
                        _wait_settle(page, 6000)
                        dismiss_cookies(page)
                        clause = clause_check()
                        if clause:
                            res.result = "tc_blocked"
                            res.message = f"T&Cs ban automated entries: \u201c{clause[:300]}\u201d"
                            return res
                        best = best_form()
                        break
            if not best or best[2] < 4:
                if CLOSED_RE.search(_page_text(page)):
                    res.result, res.message = "closed", "Page says the competition has closed"
                else:
                    res.result, res.message = "failed", "No entry form found on the page"
                res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_noform.jpg")
                return res

            cap = detect_captcha(page)
            if cap:
                res.result, res.message = "captcha", f"CAPTCHA on the form ({cap}) - enter manually"
                res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_captcha.jpg")
                return res

            done_idx: set[str] = set()
            for step in range(1, 4):
                frame, container, score = best
                fields = [f for f in _to_fields(container) if f.idx not in done_idx]
                plan = plan_form(fields, ctx)
                unknown = [(f, a) for f, a in plan if a.kind == "unknown"]
                if unknown and llm.enabled() and settings.get("llm_for_forms", True):
                    payload = [{"id": f.idx, "type": f.type if f.tag != "radio" else "radio", "label": f.human_label,
                                "required": f.required, "context": f.context[:200],
                                "options": [o.get("text") for o in f.options][:40]} for f, _ in unknown]
                    suggestions = llm.map_fields(payload, comp_context, PROFILE_KEYS)
                    new_plan = []
                    for f, a in plan:
                        if a.kind == "unknown" and f.idx in suggestions:
                            a = apply_llm_action(f, suggestions[f.idx], ctx)
                        new_plan.append((f, a))
                    plan = new_plan
                    unknown = [(f, a) for f, a in plan if a.kind == "unknown"]
                if unknown:
                    res.result = "needs_input"
                    res.message = "Couldn't fill: " + "; ".join(f"{f.human_label} ({a.note})" for f, a in unknown)[:900]
                    details["fields"] += [{"label": f.human_label, "action": a.kind, "source": a.source,
                                           "note": a.note} for f, a in plan]
                    res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_needsinput.jpg")
                    return res

                failed = []
                for f, a in plan:
                    if a.kind == "skip":
                        continue
                    ok = _apply(frame, f, a)
                    details["fields"].append({"label": f.human_label, "action": a.kind, "source": a.source,
                                              "note": a.note, "ok": ok,
                                              "value": a.value if a.source in ("answer", "llm") else None})
                    if not ok and (f.required or a.source == "answer"):
                        failed.append(f.human_label)
                    done_idx.add(f.idx)
                if failed:
                    res.result, res.message = "failed", "Couldn't set: " + ", ".join(failed)
                    res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_fillfail.jpg")
                    return res

                sample_idx = fields[0].idx if fields else None
                invalid = _safe_eval(frame, JS_VALIDITY, sample_idx, default=[]) if sample_idx else []
                if invalid:
                    res.result, res.message = "needs_input", "Form still invalid: " + ", ".join(invalid)[:500]
                    res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_invalid.jpg")
                    return res

                res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_s{step}_before.jpg")
                if dry_run:
                    res.result = "dry_run_ok"
                    res.message = f"Dry run: filled {sum(1 for _, a in plan if a.kind != 'skip')} fields; not submitted"
                    return res

                submit_label = _safe_eval(frame, JS_MARK_SUBMIT, sample_idx) if sample_idx else None
                if not submit_label:
                    res.result, res.message = "failed", "Couldn't find the submit button"
                    return res
                before_text = set(_page_text(page).splitlines())
                before_url = page.url
                details["steps"].append(f"step {step}: clicked '{submit_label}'")
                n_pages = len(context.pages)
                try:
                    frame.locator('[data-cb-submit="1"]').first.click(timeout=8000)
                except Exception as exc:  # noqa: BLE001
                    res.result, res.message = "failed", f"Submit click failed: {exc}"[:500]
                    return res
                time.sleep(2)
                if len(context.pages) > n_pages:
                    page = context.pages[-1]
                _wait_settle(page, 12000)
                time.sleep(1.5)
                res.final_url = page.url
                after_text = _page_text(page)
                new_text = "\n".join(ln for ln in after_text.splitlines() if ln not in before_text)
                res.screenshot_after = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_s{step}_after.jpg")

                if detect_captcha(page):
                    res.result, res.message = "captcha", "A CAPTCHA challenge appeared after submitting"
                    return res
                if ALREADY_RE.search(new_text):
                    res.result, res.message = "entered", "Site says you've already entered"
                    return res
                if SUCCESS_RE.search(new_text):
                    m = SUCCESS_RE.search(new_text)
                    res.result, res.message = "entered", f"Confirmation seen: \u201c{m.group(0)[:120]}\u201d"
                    return res
                err = ERROR_RE.search(new_text)
                nxt = best_form()
                new_fields = [f for f in (nxt[1]["fields"] if nxt else []) if f["idx"] not in done_idx]
                if new_fields and nxt[2] >= 4 and not err:
                    clause = clause_check()
                    if clause:
                        res.result = "tc_blocked"
                        res.message = f"T&Cs on step {step + 1} ban automated entries: \u201c{clause[:300]}\u201d"
                        return res
                    if detect_captcha(page):
                        res.result, res.message = "captcha", f"CAPTCHA on form step {step + 1} - finish manually"
                        return res
                    best = nxt
                    details["steps"].append(f"step {step + 1}: another form page")
                    continue
                if err:
                    snippet = new_text[max(0, err.start() - 60): err.end() + 80].replace("\n", " ")
                    res.result, res.message = "failed", f"Form reported a problem: \u201c{snippet.strip()}\u201d"
                    return res
                still_there = nxt and any(f["idx"] in done_idx for f in nxt[1]["fields"])
                if page.url.split("#")[0] != before_url.split("#")[0] or not still_there:
                    res.result, res.message = "unverified", "Submitted; page changed but no explicit confirmation text"
                    return res
                res.result, res.message = "failed", "Submitted but nothing changed on the page"
                return res
            res.result, res.message = "unverified", "Gave up after 3 form pages"
            return res
        except Exception as exc:  # noqa: BLE001
            log.exception("entry error for %s", url)
            res.result, res.message = "error", f"{type(exc).__name__}: {exc}"[:800]
            try:
                res.screenshot_after = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_error.jpg")
            except Exception:  # noqa: BLE001
                pass
            return res
        finally:
            res.details = details
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


def result_as_dict(r: AttemptResult) -> dict:
    return asdict(r)
