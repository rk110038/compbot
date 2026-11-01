"""Playwright-driven web form entry.

Deliberately honest automation: no CAPTCHA solving, no stealth/bot-detection evasion, no social media logins.
Anything that needs those is handed to the manual queue with a reason.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from .. import llm
from ..config import get_config
from ..netguard import guarded_client, is_public_url
from .forms import (
    PROFILE_KEYS,
    THIRD_PARTY_RE,
    Action,
    Ctx,
    Field,
    apply_llm_action,
    choose_option,
    find_automation_clause,
    is_placeholder_option,
    normalise,
    plan_form,
)

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
  let clip = s.position !== 'fixed';
  let p = el.parentElement;
  while (p && p !== document.body) {
    const ps = getComputedStyle(p);
    if (ps.display === 'none' || ps.visibility === 'hidden' || parseFloat(ps.opacity || '1') < 0.05) return false;
    if (clip) {
      // Cut off by an overflow:hidden box, e.g. the other slides of a carousel quiz (or a collapsed panel).
      const cx = /hidden|clip/.test(ps.overflowX), cy = /hidden|clip/.test(ps.overflowY);
      if (cx || cy) {
        const pr = p.getBoundingClientRect();
        if (cx && (r.right <= pr.left + 1 || r.left >= pr.right - 1)) return false;
        if (cy && (r.bottom <= pr.top + 1 || r.top >= pr.bottom - 1)) return false;
      }
      if (ps.position === 'fixed') clip = false;
    }
    p = p.parentElement;
  }
  return true;
};
const __cbTxt = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
// Web-component widgets (e.g. Viral Loops' <form-widget>) keep their buttons and entry form inside an open shadow
// root, which document.querySelectorAll can't see. These search inside shadow roots too.
const __cbAll = (root, sel) => {
  const out = Array.from(root.querySelectorAll(sel));
  const walk = r => { for (const h of r.querySelectorAll('*')) if (h.shadowRoot) { out.push(...h.shadowRoot.querySelectorAll(sel)); walk(h.shadowRoot); } };
  walk(root);
  return out;
};
const __cbOne = (sel) => document.querySelector(sel) || __cbAll(document, sel)[0] || null;
// closest() that carries on outside the shadow root, through its host element.
const __cbClosest = (el, sel) => { while (el) { const c = el.closest(sel); if (c) return c; const r = el.getRootNode(); el = r && r.host; } return null; };
const __cbContains = (box, el) => { while (el) { if (box.contains(el)) return true; const r = el.getRootNode(); el = r && r.host; } return false; };
const __cbById = (el, id) => { const r = el && el.getRootNode && el.getRootNode(); return (r && r.getElementById && r.getElementById(id)) || document.getElementById(id); };
"""

JS_EXTRACT = "(prefix) => {\n" + JS_HELPERS + r"""
  const vis = __cbVis, txt = __cbTxt;
  const labelFor = (el) => {
    let t = '';
    if (el.labels && el.labels.length) t = Array.from(el.labels).map(txt).join(' ');
    if (!t) { const lb = el.getAttribute('aria-labelledby'); if (lb) t = lb.split(/\s+/).map(id => txt(__cbById(el, id))).join(' '); }
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
  const all = __cbAll(document, '[data-cb-idx]').map(e => e.getAttribute('data-cb-idx'));
  for (const a of all) { const n = parseInt(String(a).split('_').pop(), 10); if (!isNaN(n) && n >= counter) counter = n + 1; }
  const forms = __cbAll(document, 'form');
  const roots = forms.concat([document.body]);
  const out = [];
  for (const root of roots) {
    if (!root) continue;
    const fields = [], groups = {};
    for (const el of __cbAll(root, 'input, select, textarea')) {
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
          const rg = el.closest('[role=radiogroup]');
          if (rg) { gl = rg.getAttribute('aria-label') || ''; const lb = rg.getAttribute('aria-labelledby'); if (!gl && lb) gl = txt(__cbById(rg, lb)); }
          if (!gl) {
            // The smallest container holding just this group: use its own question label, not an option label.
            const isOptionLabel = l => l.querySelector('input') || (l.htmlFor && (__cbById(l, l.htmlFor) || {}).type === 'radio') || (l.closest('label') && l.closest('label').querySelector('input[type=radio]'));
            let n = el.parentElement;
            for (let i = 0; i < 6 && n; i++) {
              const rs = Array.from(n.querySelectorAll('input[type=radio]'));
              if (rs.length > 1 && rs.every(r => r.name === el.name)) {
                const lab = Array.from(n.querySelectorAll('legend, label, [class*=label i]')).find(l => !isOptionLabel(l) && !l.querySelector('input[type=radio]') && txt(l));
                if (lab) { gl = txt(lab); break; }
                let prev = n.previousElementSibling;
                if (prev && txt(prev) && txt(prev).length < 300 && !prev.querySelector('input,select,textarea')) { gl = txt(prev); break; }
              }
              if (n.querySelectorAll('input[type=radio]').length && Array.from(n.querySelectorAll('input[type=radio]')).some(r => r.name !== el.name)) break;
              n = n.parentElement;
            }
          }
          if (!gl) {
            const fs = el.closest('fieldset');
            if (fs && Array.from(fs.querySelectorAll('input')).every(r => r.type === 'radio' && r.name === el.name)) {
              const lg = fs.querySelector('legend'); if (lg) gl = txt(lg);
            }
          }
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
      if (type === 'checkbox' && el.name) {
        // A group of checkboxes ("What are you interested in? [ ] MTB [ ] Moto"): use the group's own question.
        const same = Array.from(el.getRootNode().querySelectorAll('input[type=checkbox]')).filter(c => c.name === el.name);
        if (same.length > 1) {
          let n = el.parentElement, q = '';
          for (let i = 0; i < 6 && n && !q; i++) {
            const inside = Array.from(n.querySelectorAll('input[type=checkbox]'));
            if (inside.length >= same.length && inside.every(c => c.name === el.name)) {
              const lab = Array.from(n.querySelectorAll('legend, [class*=label i], p, h3, h4, label')).find(l => !l.querySelector('input') && !(l.closest('label') && l.closest('label').querySelector('input')) && txt(l));
              const prev = n.previousElementSibling;
              q = lab ? txt(lab) : (prev && txt(prev).length < 300 && !prev.querySelector('input') ? txt(prev) : '');
            }
            n = n.parentElement;
          }
          if (!q) { let p = el.closest('label') || el; for (let i = 0; i < 6 && p && !q; i++) { p = p.previousElementSibling; if (p && !p.querySelector('input') && txt(p)) q = txt(p); } }
          if (q) f.context = q.slice(0, 300);
        }
      }
      if (el.tagName === 'SELECT') f.options = Array.from(el.options).map(o => ({value: o.value, text: (o.text || '').trim()}));
      fields.push(f);
    }
    const btns = __cbAll(root, 'button, input[type=submit], input[type=image], input[type=button], [role=button], a.button, a.btn, a[class*=button i], a[class*=btn i], a[class*=cta i]').filter(vis);
    let formId = '';
    if (root !== document.body) {
      // Lets a paged form (Next -> Next -> Submit inside one <form>) be recognised on its later pages.
      formId = root.getAttribute('data-cb-form') || ('f' + Math.random().toString(36).slice(2, 10));
      root.setAttribute('data-cb-form', formId);
    }
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
    out.push({isForm: root.tagName === 'FORM', formId, fields, hasPassword: !!root.querySelector('input[type=password]'),
      submitTexts: btns.map(b => (txt(b) || b.value || b.getAttribute('aria-label') || '').slice(0, 60)),
      text: txt(root).slice(0, 1500), heading, meta,
      inFooter: root !== document.body && !!__cbClosest(root, 'footer, [role=contentinfo], #footer, .footer, [id*=site-footer i], [class*=site-footer i], [class*=page-footer i], [id*=page-footer i]')});
  }
  return out;
}
"""

JS_MARK_SUBMIT = "(idx) => {\n" + JS_HELPERS + r"""
  const vis = __cbVis, txt = __cbTxt;
  __cbAll(document, '[data-cb-submit]').forEach(e => e.removeAttribute('data-cb-submit'));
  const el = __cbOne(`[data-cb-idx="${idx}"]`);
  let root = el && el.form ? el.form : null;
  if (!root && el) { let n = el.parentElement; while (n && !n.querySelector('button, input[type=submit], input[type=button], [role=button]')) n = n.parentElement; root = n; }
  if (!root) root = document.body;
  const good = /^(enter|submit|send|sign ?up|subscribe|register|join|go|count me in|take part|enter now|enter competition|enter the (competition|draw|prize draw)|submit entry|enter draw|i'?m in|done|continue|next|confirm|win|enter to win|let'?s go|play|ok)\b/i;
  const bad = /cancel|reset|back|close|log ?in|sign ?in|search|menu|cookie|accept all|reject|share|facebook|twitter|instagram|print|basket|cart/i;
  let best = null, bestScore = -99;
  // Link-style buttons count too, e.g. Wayin/Cheetah forms' <a class="xCTA xButton">Submit</a> (Future plc comps).
  for (const b of root.querySelectorAll('button, input[type=submit], input[type=image], input[type=button], [role=button], a.button, a.btn, a[class*=button i], a[class*=btn i], a[class*=cta i]')) {
    if (!vis(b)) continue;
    const t = (txt(b) || b.value || b.getAttribute('aria-label') || '').trim();
    const type = (b.getAttribute('type') || '').toLowerCase();
    let s = 0;
    if (type === 'submit' || (b.tagName === 'INPUT' && type !== 'button')) s += 3;
    else if (b.tagName === 'INPUT') s += 1;  // <input type="button" value="Next">, e.g. Dotdigital's paged forms
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

JS_SET_CHECKED = "([idx, want]) => {\n" + JS_HELPERS + r"""
  const el = __cbOne(`[data-cb-idx="${idx}"]`);
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

JS_VALIDITY = "(idx) => {\n" + JS_HELPERS + r"""
  const el = __cbOne(`[data-cb-idx="${idx}"]`);
  const form = el && el.form;
  if (!form || form.noValidate) return [];  // the site validates itself
  // Only what's on screen: a paged form keeps its later pages' required fields in the same <form>, hidden.
  const shown = e => __cbVis(e) || (e.labels && Array.from(e.labels).some(__cbVis)) || __cbVis(e.parentElement);
  return Array.from(form.elements).filter(e => e.willValidate && !e.checkValidity() && shown(e))
    .filter(e => !(e.validity.patternMismatch && e.getAttribute('pattern') === '' && !e.validity.valueMissing))
    .map(e => ((e.labels && e.labels[0] ? e.labels[0].innerText : '') || e.name || e.id || e.type).trim().slice(0, 80));
}
"""

JS_COOKIES = "() => {\n" + JS_HELPERS + r"""
  const vis = __cbVis;
  const ids = ['onetrust-reject-all-handler', 'onetrust-accept-btn-handler', 'CybotCookiebotDialogBodyButtonDecline',
    'CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll', 'didomi-notice-disagree-button', 'didomi-notice-agree-button',
    'truste-consent-required', 'truste-consent-button', 'cookie-reject', 'cookie-accept'];
  const mark = b => { document.querySelectorAll('[data-cb-cookie]').forEach(e => e.removeAttribute('data-cb-cookie')); b.setAttribute('data-cb-cookie', '1'); };
  for (const id of ids) { const b = document.getElementById(id); if (b && vis(b)) { mark(b); return id; } }
  const reject = /^(reject all|reject|reject all cookies|decline|decline all|necessary only|only necessary|essential only|use necessary cookies only|refuse all|deny|disagree|continue without accepting)$/i;
  const accept = /^(accept all|accept|accept all cookies|accept cookies|i accept|agree|i agree|allow all|allow all cookies|allow cookies|got it|ok|okay|agree and close|accept & close|accept and close|yes,? i'?m happy|i understand|understood|ok,? got it|that'?s fine|accept (and|&) continue|accept all (and|&) continue|agree (and|&) continue)$/i;
  const banner = '[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],[id*=gdpr i],[class*=gdpr i],[class*=cmp i],[id*=cmp i],[aria-label*=cookie i],[aria-label*=consent i],[id*=privacy i],[class*=privacy i],[role=dialog],[aria-modal=true]';
  // Only a consent-manager *iframe* counts as "all of this frame is the banner" - never the competition page itself.
  const framey = window !== window.top && /sp_message|consent|cookie|privacy-mgmt|cmp/i.test(location.href);
  // Never press a button belonging to a real form (one with text/email fields): that could submit an entry.
  const inEntryForm = b => { const f = b.closest('form'); return !!(f && f.querySelector('input[type=text],input[type=email],input:not([type]),textarea')); };
  const btns = Array.from(document.querySelectorAll('button, [role=button], a, input[type=button]')).filter(b => vis(b) && !inEntryForm(b));
  for (const re of [reject, accept]) {
    for (const b of btns) {
      const t = (b.innerText || b.value || '').replace(/\s+/g, ' ').trim();
      if (re.test(t) && (framey || b.closest(banner))) { mark(b); return t; }
    }
  }
  return null;
}
"""

JS_CAPTCHA = "() => {\n" + JS_HELPERS + r"""
  // Only *interactive* challenges count. Invisible reCAPTCHA/hCaptcha just score the visit in the background (the
  // site decides); if they escalate to a real challenge that shows up here and compbot stops.
  const shown = el => {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (r.width < 30 || r.height < 30 || st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity || '1') < 0.1) return false;
    if (r.bottom < 0 || r.right < 0) return false;
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ps = getComputedStyle(p);
      if (ps.visibility === 'hidden' || ps.display === 'none' || parseFloat(ps.opacity || '1') < 0.1) return false;
    }
    return true;
  };
  for (const f of __cbAll(document, 'iframe')) {
    const s = f.src || '';
    if (/recaptcha\/(api2|enterprise)\/anchor/.test(s) && !/size=invisible/.test(s) && shown(f)) return 'reCAPTCHA checkbox';
    if (/recaptcha\/(api2|enterprise)\/bframe/.test(s) && shown(f)) return 'reCAPTCHA challenge';
    if (/hcaptcha\.com/.test(s) && !/invisible/.test(s) && shown(f)) return 'hCaptcha';
    if (/challenges\.cloudflare\.com/.test(s) && shown(f)) return 'Cloudflare Turnstile';
    if (/captcha/i.test(f.title || '') && !/invisible|recaptcha|hcaptcha/i.test(s + ' ' + f.title) && shown(f)) return 'CAPTCHA';
  }
  for (const sel of ['.h-captcha:not([data-size="invisible"])', '.cf-turnstile', '#px-captcha', '.frc-captcha',
                     'input[name*="captcha" i]:not([type=hidden])', '#captcha', '.captcha']) {
    for (const el of __cbAll(document, sel)) if (shown(el)) return sel;
  }
  // A bot-check page replaces the whole site, so only the main page counts, and only when it shows no form. Adverts
  // and widgets in their own small frames sometimes say "access denied" / "403 forbidden" (seen on Rayo's pages,
  // which then got wrongly treated as blocking compbot).
  if (window !== window.top) return null;
  const formInputs = __cbAll(document, 'input[type=text], input[type=email], input[type=tel], input:not([type]), textarea').filter(__cbVis);
  if (formInputs.length >= 2) return null;
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

JS_REVEAL = "(dryRun) => {\n" + JS_HELPERS + r"""
  const vis = __cbVis;
  const txt = el => __cbTxt(el).replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{200D}]/gu, '').replace(/\s+/g, ' ').trim();
  // Buttons that only open the entry form or quiz: safe to press even in a dry run.
  const safe = /^((let'?s )?(start|begin|take|do|play|try)( the| our| this| a)?( quiz| game| competition| challenge| test)?( now| here)?|get started|let'?s (go|start|play)|show (the )?(form|questions?)|reveal (the )?(form|question)|continue)!?$/i;
  // Buttons that might themselves submit an entry: only pressed for real entries.
  const enter = /^(enter( now| here| the competition| competition| the (prize )?draw| to win| now for free| the giveaway| giveaway| the quiz| quiz)?|click (here )?to enter|take part|enter (&|and) win|i want to win|join (in|now)|(join|sign ?up|subscribe|register|opt in)( now)? to enter|count me in)!?$/i;
  // A start button greyed out until "I've read the rules" is ticked: that box is the gate.
  const gateRe = /\b(read|understood?|accept|agree|ready|rules|terms|t'?s? ?(&|and) ?c'?s|conditions|over (the age of )?18|18 or over|aged 18)\b/i;
  const notGate = /marketing|newsletter|offers|partners|third|e-?mail me|contact me|keep me (updated|informed)|updates|news\b/i;
  const isDisabled = b => b.disabled || b.getAttribute('aria-disabled') === 'true' || /(^|\s)(is-)?disabled(\s|$)/.test(typeof b.className === 'string' ? b.className : '');
  const findGate = b => {
    let n = b.parentElement;
    for (let i = 0; i < 5 && n; i++, n = (n === document.body ? null : n.parentElement)) {
      for (const c of n.querySelectorAll('input[type=checkbox]')) {
        if (c.checked || c.disabled) continue;
        const lab = (c.labels && c.labels[0]) || c.closest('label');
        if (!(vis(c) || (lab && vis(lab)))) continue;
        const t = txt(lab) || txt(c.parentElement);
        if (gateRe.test(t) && !notGate.test(t)) return c;
      }
    }
    return null;
  };
  const h1 = document.querySelector('h1');
  const top0 = h1 ? h1.getBoundingClientRect().top + scrollY : 0;
  let best = null, bestScore = -1e9, bestGate = null;
  for (const b of __cbAll(document, 'button, a, [role=button], input[type=button]')) {
    if (!vis(b) || b.hasAttribute('data-cb-tried')) continue;
    // Never press anything that could submit a form.
    if (b.closest('form') || (b.getAttribute('type') || '').toLowerCase() === 'submit') continue;
    const t = (txt(b) || b.value || '').trim();
    const isLink = b.tagName === 'A' && /^https?:/i.test(b.href || '');
    const offsite = isLink && new URL(b.href, location.href).host !== location.host;
    const isSafe = safe.test(t), isEnter = enter.test(t);
    if (!isSafe && !isEnter) continue;
    if (isEnter && !isSafe && dryRun && !isLink) continue;
    let gate = null;
    if (isDisabled(b)) { gate = findGate(b); if (!gate) continue; }
    let s = 0;
    if (/quiz|competition|draw|giveaway|enter/i.test(t)) s += 6;
    if (/^(play|play now|start|begin|try)!?$/i.test(t)) s -= 4;          // generic: could be a game or video
    if (/^continue$/i.test(t)) s -= 3;                                    // e.g. a form's welcome screen
    if (offsite && /^(play|start|begin|try|take)/i.test(t)) s -= 8;       // e.g. a fantasy-game promo
    if (__cbClosest(b, 'main, article, [role=main]')) s += 2;
    if (__cbClosest(b, 'header, nav, footer, aside, [role=navigation], [role=banner], [role=contentinfo]')) s -= 8;
    s -= Math.min(6, Math.abs(b.getBoundingClientRect().top + scrollY - top0) / 600);  // nearer the headline
    if (s > bestScore) { best = b; bestScore = s; bestGate = gate; }
  }
  if (!best || bestScore < -6) return null;
  __cbAll(document, '[data-cb-reveal],[data-cb-gate]').forEach(e => { e.removeAttribute('data-cb-reveal'); e.removeAttribute('data-cb-gate'); });
  best.setAttribute('data-cb-reveal', '1');
  best.setAttribute('data-cb-tried', '1');  // if it doesn't lead anywhere, the next-best button gets a turn
  let gateLabel = null;
  if (bestGate) {
    bestGate.setAttribute('data-cb-gate', '1');
    const lab = (bestGate.labels && bestGate.labels[0]) || bestGate.closest('label');
    gateLabel = (txt(lab) || txt(bestGate.parentElement)).slice(0, 120);
  }
  return {label: (txt(best) || best.value || '').trim(), gate: gateLabel};
}
"""

# Click-to-answer quizzes: a question (text with "?") followed by a set of answer buttons/tiles that aren't form
# fields. Marks each group's options [data-cb-opt=gNoM] and the best "next/reveal/see results" button [data-cb-next].
JS_QUIZ_SCAN = "([skip, focus, dryRun]) => {\n" + JS_HELPERS + r"""
  const vis = __cbVis;
  const clean = s => (s || '').replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{200D}\u{20E3}]/gu, ' ').replace(/\s+/g, ' ').trim();
  const txt = el => clean(el ? (el.innerText || el.textContent || '') : '');
  const cls = el => String((el && (typeof el.className === 'string' ? el.className : (el.getAttribute && el.getAttribute('class')))) || '');
  const tok = (el, words) => new RegExp('(^|[\\s_-])(' + words + ')([\\s_-]|$)', 'i').test(cls(el));
  const OFF = 'header, nav, footer, aside, [role=navigation], [role=banner], [role=contentinfo]';
  const DIALOG = '[role=dialog], [aria-modal=true], dialog, [class*=modal i]';
  const CLICKY = 'button, [role=button], [role=radio], [role=option], [role=checkbox], [role=menuitemradio], label, a';
  const CONSENT = /cookie|consent|gdpr|onetrust|didomi|cookiebot|sp_message|privacy-?(mgmt|manager|center|centre)/i;
  const NOT_QUIZ = /\b(was (this|it) (page |article |answer )?(helpful|useful)|feedback|rate (this|our|us)|how did we do|recommend us|cookies?|newsletter|log ?in|sign ?in|search|forgot (your )?password)\b/i;
  const BAD_OPT = /^(share|tweet|facebook|twitter|instagram|whatsapp|e-?mail|copy link|print|close|cancel|back|menu|search|log ?in|sign ?in|accept( all)?|reject( all)?|cookie settings|read more)$/i;
  const WRONG = 'incorrect|wrong|is-wrong|false|error|fail|failed|invalid';
  const RIGHT = 'correct|is-correct|right|success|true|valid';
  const SEL = 'selected|chosen|picked|checked|is-selected|is-active|active';
  const LOCK = 'locked|disabled|answered|complete|completed|done|is-locked|is-disabled|is-answered';
  const entryForm = el => { const f = el.closest('form'); return !!(f && Array.from(f.querySelectorAll('input[type=text], input[type=email], input[type=tel], input:not([type]), textarea')).some(vis)); };
  const isDis = el => !!(el.disabled || el.getAttribute('aria-disabled') === 'true' || tok(el, 'disabled|locked|is-disabled|is-locked') || el.closest('fieldset[disabled]'));
  // Is this the element a person would actually click at its centre? (false = something covers or clips it;
  // null = it's off-screen, so we can't tell)
  const hits = el => {
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + Math.min(r.height / 2, 20);
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;
    const h = document.elementFromPoint(x, y);
    return !!h && (el === h || el.contains(h));
  };
  // Pop-ups that are open now (e.g. the quiz itself, or its "correct! / next question" layer).
  const openDialogs = Array.from(document.querySelectorAll(DIALOG)).filter(d => {
    if (!vis(d)) return false;
    const st = getComputedStyle(d), r = d.getBoundingClientRect();
    return (st.position === 'fixed' || st.position === 'absolute') && r.width * r.height > 0.2 * innerWidth * innerHeight;
  });
  const reachable = el => { const h = hits(el); return h === true || (h === null && (!openDialogs.length || openDialogs.some(d => d.contains(el)))); };
  document.querySelectorAll('[data-cb-opt],[data-cb-next],[data-cb-retry]').forEach(e => { e.removeAttribute('data-cb-opt'); e.removeAttribute('data-cb-next'); e.removeAttribute('data-cb-retry'); });
  if (!document.body) return {groups: [], next: null, retry: null, quizNext: false};

  // Question elements: the block holding a "?" (innermost, so not the whole card).
  const qList = [];
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT,
    {acceptNode: n => n.nodeValue.indexOf('?') >= 0 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT});
  let tn;
  while ((tn = walker.nextNode()) && qList.length < 300) {
    let el = tn.parentElement;
    while (el && /^(SPAN|B|STRONG|EM|I|U|SMALL|MARK|FONT|SUP|SUB)$/.test(el.tagName) && el.parentElement && txt(el.parentElement).length <= 300) el = el.parentElement;
    if (!el || seen.has(el) || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TEXTAREA|OPTION|TITLE)$/.test(el.tagName)) continue;
    seen.add(el);
    if (el.closest(CLICKY) || el.closest(OFF)) continue;
    const t = txt(el);
    if (t.length < 8 || t.length > 300 || t.indexOf('?') < 0 || NOT_QUIZ.test(t) || !vis(el)) continue;
    let bad = false;
    for (let a = el, k = 0; a && k < 10; a = a.parentElement, k++) if (CONSENT.test((a.id || '') + ' ' + cls(a))) { bad = true; break; }
    if (!bad && !entryForm(el)) qList.push(el);
  }

  const explicit = el => el.matches('button, [role=button], [role=radio], [role=option], [role=checkbox], [role=menuitemradio], label, [onclick]') || el.tagName.indexOf('-') > 0 || !!el.querySelector('input[type=radio], input[type=checkbox]');
  const optionLike = el => {
    if (el.matches('input, select, textarea, option, img, svg, path, br, hr, iframe, script, style, source, picture')) return false;
    if (el.querySelector('input:not([type=radio]):not([type=checkbox]):not([type=hidden]), select, textarea')) return false;
    const t = txt(el);
    if (!t || t.length > 160 || BAD_OPT.test(t) || /\?$/.test(t)) return false;
    if (el.hasAttribute('aria-expanded') || el.hasAttribute('aria-haspopup')) return false;  // accordions, menus
    if (el.tagName === 'A') { const h = el.getAttribute('href') || ''; if (h && !/^(#|javascript:)/i.test(h)) return false; }
    if (!vis(el)) return false;
    if (explicit(el) || el.matches('[tabindex]:not([tabindex="-1"])')) return true;
    return getComputedStyle(el).cursor === 'pointer';
  };
  const follows = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  const findGroup = (q, nextQ) => {
    let a = q.parentElement;
    for (let lvl = 0; lvl < 5 && a && a !== document.documentElement; lvl++, a = a.parentElement) {
      const els = a.querySelectorAll('*');
      if (els.length > 2500) break;
      const byParent = new Map();
      for (const e of els) {
        if (!follows(q, e) || q.contains(e) || e.contains(q)) continue;
        if (nextQ && !follows(e, nextQ)) continue;
        const p = e.parentElement;
        if (!p || p.closest(CLICKY)) continue;  // e.g. the letter + text inside one answer button
        if (!optionLike(e)) continue;
        if (!byParent.has(p)) byParent.set(p, []);
        byParent.get(p).push(e);
      }
      let pick = null;
      for (const [p, kids] of byParent) {
        const tag = kids[0].tagName;
        const same = kids.filter(k => k.tagName === tag);
        if (same.length < 2 || same.length > 10) continue;
        const ex = same.every(explicit);
        if (!pick || (ex && !pick.ex)) pick = {parent: p, opts: same, ex};
        if (pick.ex) break;
      }
      if (pick) return pick;
    }
    return null;
  };
  const optText = o => {
    const kids = Array.from(o.children).filter(c => txt(c));
    if (kids.length >= 2 && /^[A-Ha-h1-9][\).:]?$/.test(txt(kids[0]))) { const rest = clean(kids.slice(1).map(txt).join(' ')); if (rest) return rest.slice(0, 160); }
    const t = txt(o), m = t.match(/^([A-Ha-h])[\).:\-]\s+(\S.*)$/);
    return (m ? m[2] : t).slice(0, 160);
  };

  const groups = [];
  let focusEl = null, scrolled = false;
  for (let i = 0; i < qList.length && groups.length < 6; i++) {
    const q = qList[i], question = txt(q);
    if (focus && question === focus) focusEl = q;
    if ((skip || []).includes(question)) continue;
    const g = findGroup(q, qList[i + 1]);
    if (!g) continue;
    let locked = g.opts.every(isDis);
    for (let a = g.parent, k = 0; a && k < 3 && !locked; a = a.parentElement, k++) if (tok(a, LOCK)) locked = true;
    if (!locked && !scrolled) {
      // Bring the question into view by scrolling the window only (scrolling the quiz's own box would shift a
      // carousel to a slide it isn't on), so we can check nothing covers the answers.
      scrolled = true;
      const r = q.getBoundingClientRect(), last = g.opts[g.opts.length - 1].getBoundingClientRect();
      if (r.top < 0 || last.bottom > innerHeight) window.scrollBy(0, r.top - Math.max(40, (innerHeight - (last.bottom - r.top)) / 2));
    }
    const area = g.parent.parentElement || g.parent;
    let rest = txt(area);
    for (const o of g.opts) rest = rest.replace(txt(o), ' ');
    const gi = groups.length;
    const options = g.opts.map((o, k) => {
      o.setAttribute('data-cb-opt', `g${gi}o${k}`);
      const wrong = tok(o, WRONG) || o.getAttribute('aria-invalid') === 'true';
      return {key: `g${gi}o${k}`, text: optText(o), wrong, right: !wrong && tok(o, RIGHT), dis: isDis(o),
              sel: tok(o, SEL) || ['aria-pressed', 'aria-checked', 'aria-selected'].some(x => o.getAttribute(x) === 'true') || !!o.querySelector('input:checked')};
    });
    groups.push({question, options, locked, dialog: !!q.closest(DIALOG),
                 covered: !g.opts.every(reachable),
                 wrongHint: /\b(incorrect|wrong answer|not quite|try again|not (quite )?right|unlucky|oops)\b/i.test(rest.slice(0, 800))});
  }

  // The button that moves the quiz on: next / reveal / see results / (for real entries) enter the draw.
  const NEXT = /^(next( question| step)?|continue|reveal( the)?( next)?( question)?( \d+)?|show( me)?( the)?( next)? question( \d+)?|(tap|click) (here )?to reveal|check( my| the)? answers?|submit( my| your)? answers?|confirm( my)?( answer)?|lock (it |answer )?in|(see|show|get|view|reveal) (my |your |the )?(results?|score)|finish( the)?( quiz)?|done|ok|start( the)? quiz|begin( the)? quiz|let'?s go)!?$/i;
  const ENTER = /^(enter( now| here)?|enter( the)?( prize)? (draw|competition|giveaway)|claim( your)?( prize| entry)?|complete (your )?(prize draw )?entry|go to( the)? (entry|form|draw))!?$/i;
  const RETRY = /^(retry|try again|have another go|another go|go again|try another answer)!?$/i;
  const QUIZY = /question|quiz|reveal|result|score|answer/i;
  const fy = focusEl ? focusEl.getBoundingClientRect().top + scrollY : scrollY + innerHeight / 3;
  let next = null, nextScore = -1e9, retry = null;
  for (const b of document.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit]')) {
    if (b.closest('[data-cb-opt]') || !vis(b) || b.closest(OFF) || entryForm(b)) continue;
    if (b.disabled || b.getAttribute('aria-disabled') === 'true' || !reachable(b)) continue;
    const t = clean(b.innerText || b.value || '');
    if (RETRY.test(t)) { if (!retry) retry = b; continue; }
    const isNext = NEXT.test(t), isEnter = ENTER.test(t);
    if (!isNext && !isEnter) continue;
    const href = b.tagName === 'A' ? (b.getAttribute('href') || '') : '';
    const realLink = !!href && !/^(#|javascript:)/i.test(href);
    const offsite = realLink && new URL(b.href, location.href).host !== location.host;
    if (isEnter && !isNext && dryRun && !realLink) continue;  // might be a one-click entry: dry runs only follow links
    if (isNext && !isEnter && offsite) continue;
    let s = 0;
    if (QUIZY.test(t)) s += 3;
    if (isEnter) s += 2;
    if (b.closest(DIALOG)) s += 2;
    const by = b.getBoundingClientRect().top + scrollY;
    s -= Math.abs(by - fy) / 800;
    if (focusEl && by < fy - 50) s -= 2;  // above (behind) the question just answered
    if (s > nextScore) { next = b; nextScore = s; }
  }
  let nextLabel = null, retryLabel = null;
  if (next) { next.setAttribute('data-cb-next', '1'); nextLabel = clean(next.innerText || next.value || '').slice(0, 60) || 'next'; }
  if (retry) { retry.setAttribute('data-cb-retry', '1'); retryLabel = clean(retry.innerText || retry.value || '').slice(0, 60); }
  return {groups, next: nextLabel, retry: retryLabel, quizNext: !!nextLabel && QUIZY.test(nextLabel)};
}
"""

# A third-party embed (e.g. a Riddle entry form) held back until the visitor allows that specific content.
JS_EMBED_CONSENT = "() => {\n" + JS_HELPERS + r"""
  const vis = __cbVis, txt = __cbTxt;
  const ph = /want to see this content|content (is )?(provided|hosted|supplied) by|this content is (blocked|hidden|not available)|to (view|see|load|display) this content|enable (third[- ]party )?(cookies|content)|requires your consent|consent to (load|display|show)/i;
  const btn = /^(accept( and| &) continue|accept|allow|allow (and|&) continue|allow content|show content|load content|enable|enable content|i agree|agree)$/i;
  for (const el of document.querySelectorAll('div, section, aside, figure, p')) {
    if (!vis(el)) continue;
    const t = txt(el);
    if (t.length > 700 || !ph.test(t)) continue;
    let host = el;
    for (let i = 0; i < 4 && host; i++) {
      const b = Array.from(host.querySelectorAll('button, a, [role=button]')).find(x => vis(x) && btn.test(txt(x)));
      if (b) { b.scrollIntoView({block: 'center'}); document.querySelectorAll('[data-cb-cookie]').forEach(e => e.removeAttribute('data-cb-cookie')); b.setAttribute('data-cb-cookie', '1'); return txt(b); }
      host = host.parentElement;
    }
  }
  return null;
}
"""

# Close marketing pop-ups / sticky bars that sit on top of the submit button (but never the form's own dialog).
JS_CLOSE_POPUPS = "() => {\n" + JS_HELPERS + r"""
  const vis = __cbVis, txt = __cbTxt;
  const sub = __cbOne('[data-cb-submit="1"]');
  const hostOf = b => b.closest('[role=dialog], [aria-modal=true], [class*=modal i], [class*=popup i], [class*=klaviyo i], [class*=overlay i], form') || b.parentElement;
  const safe = b => { const h = hostOf(b); return !(sub && h && __cbContains(h, sub)); };
  let n = 0;
  const sels = ['[aria-label*="close" i]', '[title*="close" i]', '.klaviyo-close-form', 'button.close', '.close-button',
                '.modal-close', '[class*="close-btn" i]', '[class*="closebutton" i]', '[data-testid*="close" i]'];
  for (const s of sels) for (const b of document.querySelectorAll(s)) { if (vis(b) && safe(b)) { try { b.click(); n++; } catch (e) {} } }
  const words = /^(no thanks|no,? thanks|not now|maybe later|close|dismiss|Ã—|âœ•|x)$/i;
  for (const b of document.querySelectorAll('button, a, [role=button]')) {
    if (vis(b) && safe(b) && words.test(txt(b))) { try { b.click(); n++; } catch (e) {} }
  }
  return n;
}
"""

# Last resort when a covering element swallows real clicks: activate the button directly (as its own script would).
JS_CLICK_MARKED = "() => {\n" + JS_HELPERS + r"""
  const b = __cbOne('[data-cb-submit="1"]');
  if (!b) return false;
  if (b.form && (b.type || '').toLowerCase() === 'submit' && b.form.requestSubmit) {
    try { b.form.requestSubmit(b); return true; } catch (e) {}
  }
  b.click();
  return true;
}
"""

# Leave the last-filled field so forms that react on "change"/"blur" (conditional fields) update.
JS_BLUR = "() => { const a = document.activeElement; if (a && a !== document.body && a.blur) a.blur(); return true; }"

# Load lazy iframes now (an entry form is often a lazily-loaded embed further down the page).
JS_PRIME_IFRAMES = r"""
() => {
  const fs = Array.from(document.querySelectorAll('iframe'));
  for (const f of fs) { try { if (f.loading === 'lazy') f.loading = 'eager'; } catch (e) {} }
  for (const f of fs) { try { f.scrollIntoView({block: 'center'}); } catch (e) {} }
  window.scrollTo(0, 0);
  return fs.length;
}
"""

SUCCESS_RE = re.compile(
    r"thank(?:s| you)\b.{0,40}\b(?:enter|entry|entering|taking part|submitting|submission|signing up|subscrib|"
    r"registering|participat|joining)|(?:entry|submission|details) (?:has been |have been |was )?(?:received|"
    r"submitted|confirmed|successful|complete)|you(?:'re| are| have been) (?:now )?(?:entered|in the (?:prize )?draw|"
    r"successfully|subscribed|on the list)|successfully (?:entered|submitted|subscribed|registered|signed up)|good luck|"
    r"check your (?:e-?mail|inbox)|we(?:'ve| have) (?:received|got) your|confirmation e-?mail|you(?:'re| are) in\b|"
    r"almost there|nearly there|almost finished|please confirm your|thanks for signing up|thank you for subscribing|"
    r"welcome to the (?:club|list|family|community)|subscription (?:is )?confirmed|thank you!?$|"
    # Referral widgets (Viral Loops, Gleam...) swap the form for "share your link for more entries" once you're in.
    r"share (?:your|this) (?:unique |personal |referral )*link|(?:earn|get|unlock) (?:more|extra|bonus) entries",
    re.I | re.M)
ALREADY_RE = re.compile(
    r"already (?:been )?entered|already (?:subscribed|registered|signed up|a subscriber)|only (?:one|1) entry|you "
    r"have already|duplicate entry|already taken part|already on (?:our|the) list", re.I)
ERROR_RE = re.compile(
    r"(?:is|are) required|this field is required|please (?:enter|fill|complete|select|check|provide|tick|accept|answer)|"
    r"\binvalid\b|not (?:a )?valid|something went wrong|please try again|must (?:be|accept|agree)|error", re.I)
# Errors that can't be mere instructions on the next page of a form ("please complete the tick boxes below").
HARD_ERROR_RE = re.compile(r"\binvalid\b|not (?:a )?valid|something went wrong|please try again|\berror\b", re.I)
CLOSED_RE = re.compile(
    r"(?:competition|promotion|prize draw|giveaway|this comp\w*) (?:is |has )?(?:now )?(?:closed|ended|finished)|"
    r"no longer (?:accepting|open|available)|entries (?:are|have) (?:now )?closed|has now closed|this competition "
    r"has expired", re.I)

MAX_FORM_PAGES = 8  # multi-page forms and quizzes (one question per page)


# --------------------------------------------------------------------------- helpers

def _pause(page, seconds: float) -> None:
    """Wait while letting Playwright keep processing events (so the live view keeps streaming)."""
    try:
        page.wait_for_timeout(seconds * 1000)
    except Exception:  # noqa: BLE001
        time.sleep(seconds)


class LiveView:
    """Streams the browser's screen (Chrome DevTools screencast) to shared memory for the GUI's Live page.

    Only ever *watched*: frames are JPEGs written to LIVE_DIR (tmpfs, no disk wear); nothing can be controlled.
    """

    def __init__(self, context, enabled: bool, title: str = ""):
        self.context, self.enabled, self.title = context, enabled, title
        self.cdp = None
        self.page = None
        self.dir = get_config().live_dir
        if enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False

    def attach(self, page) -> None:
        if not self.enabled or page is self.page:
            return
        self.detach()
        try:
            self.cdp = self.context.new_cdp_session(page)
            self.cdp.on("Page.screencastFrame", self._frame)
            self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 55, "maxWidth": 1100,
                                                   "maxHeight": 1400, "everyNthFrame": 1})
            self.page = page
        except Exception as exc:  # noqa: BLE001
            log.debug("live view unavailable: %s", exc)
            self.cdp = None

    def _frame(self, params: dict) -> None:
        try:
            tmp = self.dir / "frame.jpg.tmp"
            tmp.write_bytes(base64.b64decode(params["data"]))
            os.replace(tmp, self.dir / "frame.jpg")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:  # noqa: BLE001
            pass

    def status(self, phase: str, result: str | None = None) -> None:
        if not self.enabled:
            return
        try:
            url = self.page.url if self.page else ""
        except Exception:  # noqa: BLE001
            url = ""
        data = {"phase": phase[:300], "url": url[:500], "title": self.title[:200], "updated": time.time(),
                "result": result}
        try:
            tmp = self.dir / "status.json.tmp"
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.dir / "status.json")
        except Exception:  # noqa: BLE001
            pass

    def detach(self) -> None:
        if self.cdp is not None:
            try:
                self.cdp.send("Page.stopScreencast")
                self.cdp.detach()
            except Exception:  # noqa: BLE001
                pass
        self.cdp, self.page = None, None


class _LiveSteps(list):
    """The attempt's step log; each new step is also shown on the Live page."""

    def __init__(self, live: LiveView):
        super().__init__()
        self.live = live

    def append(self, item) -> None:
        super().append(item)
        self.live.status(str(item))

def _all_frames(page):
    # Frames that never navigated (e.g. lazy-loaded iframes off-screen) have no URL and no JS context;
    # evaluating in them waits forever, so leave them out.
    return [f for f in page.frames if not f.is_detached() and f.url]


EVAL_TIMEOUT_MS = 10000


def _safe_eval(frame, script, arg=None, default=None):
    """Run one of our page scripts with a hard timeout (frame.evaluate itself has none)."""
    try:
        wrapped = f"(_root, __arg) => ({script})(__arg)"
        return frame.locator(":root").evaluate(wrapped, arg, timeout=EVAL_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        log.debug("evaluate failed in %s: %s", getattr(frame, "url", "?"), exc)
        return default


def _page_text(page) -> str:
    parts = []
    for fr in _all_frames(page):
        t = _safe_eval(fr, JS_PAGE_TEXT, default="")
        if t:
            parts.append(t)
    return "\n".join(parts)


# The page's visible text, plus what's shown inside web-component widgets (body.innerText leaves shadow roots out,
# so a widget's "Thanks for joining!" would otherwise go unseen).
JS_PAGE_TEXT = r"""
() => {
  const parts = [document.body ? document.body.innerText : ''];
  const walk = r => {
    for (const h of r.querySelectorAll('*')) {
      if (!h.shadowRoot) continue;
      for (const c of h.shadowRoot.children) {
        if (/^(STYLE|SCRIPT|LINK|TEMPLATE)$/.test(c.tagName)) continue;
        if (c.checkVisibility ? !c.checkVisibility() : !c.getClientRects().length) continue;
        const t = c.innerText;
        if (t && t.trim()) parts.push(t);
      }
      walk(h.shadowRoot);
    }
  };
  walk(document);
  return parts.join('\n');
}
"""

# A real click's fallback for elements our scripts marked, wherever they are (inside shadow roots too).
JS_CLICK_SEL = "(sel) => {\n" + JS_HELPERS + r"""
  const b = __cbOne(sel);
  if (b) b.click();
  return !!b;
}
"""


def _click_marked(frame, attr: str = "data-cb-cookie") -> bool:
    """Click the element our page script marked, with a real (trusted) click; fall back to a script click."""
    loc = frame.locator(f'[{attr}="1"]').first
    try:
        loc.click(timeout=4000)
        return True
    except Exception:  # noqa: BLE001
        return bool(_safe_eval(frame, JS_CLICK_SEL, f'[{attr}="1"]', default=False))


def dismiss_cookies(page) -> list[str]:
    clicked = []
    for _ in range(2):
        found = False
        for fr in _all_frames(page):
            r = _safe_eval(fr, JS_COOKIES)
            if r and _click_marked(fr):
                clicked.append(str(r))
                found = True
        if found:
            _pause(page, 1.2)
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
CONSENT_FORM_RE = re.compile(
    r"cookie|consent|gdpr|onetrust|didomi|cookiebot|sp_message|privacy[- _]?(?:settings|preferences|centre|center|"
    r"manager)|manage (?:your )?(?:preferences|consent)", re.I)
CONSENT_TEXT_RE = re.compile(r"\s*(?:we (?:use|value)|this (?:site|website) uses) (?:cookies|your privacy)", re.I)


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
        # For newsletter competitions the footer sign-up is often the entry itself.
        s -= 2 if (ctx and ctx.is_newsletter and newsletterish) else 6
    if CONSENT_FORM_RE.search(c.get("meta", "") + " " + c.get("heading", "")) or CONSENT_TEXT_RE.match(
            c.get("text", "")[:200]):
        s -= 50  # cookie / privacy-preference dialogs are never the entry form
    if len(fields) == 1 and re.search(r"search|^q$", fields[0].get("name", "") + " " + fields[0].get("type", ""), re.I):
        s -= 25
    if all(f.get("type") == "checkbox" for f in fields):
        s -= 8  # just tick boxes (e.g. "I've read the rules" before a quiz): not an entry form on its own
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


def _refresh_container(frame, done_idx: set[str], prefix: str) -> dict | None:
    """Re-read the form we're filling (same frame) to pick up fields revealed by earlier answers."""
    for c in _safe_eval(frame, JS_EXTRACT, prefix, default=[]) or []:
        ids = {f["idx"] for f in c.get("fields", [])}
        ids |= {o.get("idx") for f in c.get("fields", []) for o in (f.get("options") or []) if o.get("idx")}
        if ids & done_idx:
            return c
    return None


def _own_next_page(frame, container: dict, done_idx: set[str], prefix: str) -> dict | None:
    """The <form> we just sent, re-read: returned if it now shows fields we haven't filled (its next page)."""
    fid = container.get("formId")
    if not fid:
        return None
    for c in _safe_eval(frame, JS_EXTRACT, prefix, default=[]) or []:
        if c.get("formId") == fid and any(f["idx"] not in done_idx for f in c.get("fields", [])):
            return c
    return None


def _form_sig(container: dict) -> frozenset:
    # Name *and* label: quiz pages often reuse one field name for every question.
    return frozenset((f.get("type", ""), (f.get("name") or "").strip().lower(),
                      (f.get("label") or f.get("placeholder") or "").strip().lower()[:60])
                     for f in container.get("fields", []))


def _same_form(a: frozenset, b: frozenset) -> bool:
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.6


JS_MAIN_TEXT = r"""() => { const m = document.querySelector('main, article, [role=main]') || document.body;
  return m ? m.innerText.replace(/\n{3,}/g, '\n\n').slice(0, 12000) : ''; }"""
AI_QUESTION_NOTES = ("question with no known answer", "required choice with no known answer")
WORD_LIMIT_RE = re.compile(r"(\d{1,3})\s*words?|(ten|fifteen|twenty|twenty[- ]five|thirty|fifty)\s*words?", re.I)
WORD_NUMS = {"ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25, "twenty-five": 25, "thirty": 30, "fifty": 50}


def _page_context(page, frame) -> str:
    text = _safe_eval(page.main_frame, JS_MAIN_TEXT, default="") or ""
    if frame is not page.main_frame:  # form inside an embed: its own text matters too
        text = (_safe_eval(frame, JS_MAIN_TEXT, default="") or "") + "\n\n" + text
    return text


def _ai_fill(page, frame, plan, settings: dict, comp_context: dict, details: dict):
    """Ask the AI about competition questions the MSE post didn't answer (and, if allowed, tie-breakers)."""
    min_conf = int(settings.get("ai_min_confidence", 75) or 0)
    page_text = None
    out = []
    for f, a in plan:
        is_q = (a.kind == "unknown" and a.note in AI_QUESTION_NOTES) or (
            a.kind == "skip" and a.note == "question with no known answer")
        if is_q and settings.get("ai_answers", True):
            if page_text is None:
                page_text = _page_context(page, frame)
            options = [o.get("text", "") for o in f.options if not is_placeholder_option(o)] if f.options else None
            question = f.human_label if len(f.human_label) > 6 else (f.context[:300] or f.human_label)
            res = llm.answer_question(question, options, page_text, comp_context, settings)
            record = {"question": question[:200], "options": (options or [])[:12]}
            if res:
                record.update(answer=res["answer"], confidence=res["confidence"], source=res["source"],
                              reason=res["reason"], used=False)
            if res and res["answer"] and res["confidence"] >= min_conf and settings.get("ai_double_check", True):
                check = llm.verify_answer(question, res["answer"], options, page_text, settings)
                if check:
                    record["check"] = check["verdict"]
                if check and check["verdict"] == "incorrect":
                    record.update(used=False, reason=f"second check disagreed: {check['reason'][:150]}")
                    details.setdefault("ai", []).append(record)
                    if a.kind == "unknown":
                        a = Action("unknown", note=f"AI answer \u201c{res['answer'][:40]}\u201d failed its second check")
                    out.append((f, a))
                    continue
            if res and res["answer"] and res["confidence"] >= min_conf:
                note = f"AI: \u201c{res['answer'][:60]}\u201d ({res['confidence']}% sure, from {res['source']})"
                if options:
                    o = choose_option(f.options, [res["answer"]], threshold=80)
                    if o:
                        a = Action("radio" if f.tag == "radio" else "select", o.get("idx") or o.get("value"),
                                   source="ai", note=note)
                else:
                    value = res["answer"][: f.maxlength] if f.maxlength else res["answer"]
                    a = Action("fill", value, source="ai", note=note)
                record["used"] = a.source == "ai"
            elif res and a.kind == "unknown":
                a = Action("unknown", note=f"AI wasn't sure enough ({res['confidence']}% < {min_conf}%)"
                           if res["answer"] else "AI couldn't work out the answer")
            details.setdefault("ai", []).append(record)
        elif a.kind == "unknown" and a.note == "creative / tie-breaker answer" and settings.get("ai_tiebreakers"):
            m = WORD_LIMIT_RE.search(f.human_label + " " + f.context[:200])
            limit = int(m.group(1)) if m and m.group(1) else WORD_NUMS.get((m.group(2) or "").lower(), 25) if m else 25
            text = llm.write_tiebreaker(f.human_label, comp_context, max(5, min(limit, 60)), settings)
            if text:
                a = Action("fill", text[: f.maxlength] if f.maxlength else text, source="ai", note="AI-written tie-breaker")
                details.setdefault("ai", []).append({"question": f.human_label[:200], "answer": text, "used": True,
                                                     "source": "written", "confidence": None})
        out.append((f, a))
    return out


REQUIRED_ERR_RE = re.compile(
    r"(?:field|this|option|selection) is required|required field|please (?:select|choose|tick|check|complete|fill)|"
    r"must (?:select|choose|tick|agree|accept|be checked)|select at least one|at least one (?:option|box)", re.I)


def _required_fixes(container: dict) -> list[tuple[Field, Action]]:
    """After a 'this field is required' error: sensible choices for groups compbot left untouched."""
    from collections import defaultdict

    fields = _to_fields(container)
    fixes: list[tuple[Field, Action]] = []
    note = "site said this was required"
    groups: dict[str, list[Field]] = defaultdict(list)
    for f in fields:
        if f.tag == "input" and f.type == "checkbox" and f.name:
            groups[f.name].append(f)
    for fs in groups.values():
        if len(fs) < 2 or any(f.checked for f in fs):
            continue
        labels = [normalise(f.label) for f in fs]
        if any(THIRD_PARTY_RE.search(lb) for lb in labels):
            continue
        pick = next((f for f, lb in zip(fs, labels) if lb in ("email", "e mail", "by email")), fs[0])
        fixes.append((pick, Action("check", source="rule", note=note)))
    for f in fields:
        if f.tag == "radio" and not f.checked:
            o = next((o for o in f.options if not is_placeholder_option(o)), None)
            if o:
                fixes.append((f, Action("radio", o.get("idx") or o.get("value"), source="rule", note=note)))
        elif f.tag == "input" and f.type == "checkbox" and f.required and not f.checked and len(groups.get(f.name, [])) < 2:
            if not THIRD_PARTY_RE.search(normalise(f.label)):
                fixes.append((f, Action("check", source="rule", note=note)))
    return fixes


def _click_submit(page, frame) -> str:
    """Click the marked submit button; cope with pop-ups/sticky bars covering it. Returns how it was pressed."""
    loc = frame.locator('[data-cb-submit="1"]').first
    try:
        if loc.is_disabled(timeout=2000):
            _pause(page, 2.5)
            if loc.is_disabled(timeout=2000):
                return "failed: the submit button stayed disabled (a required field probably wasn't recognised)"
    except Exception:  # noqa: BLE001
        pass
    try:
        loc.click(timeout=6000)
        return "click"
    except Exception as exc:  # noqa: BLE001
        first_error = str(exc).splitlines()[0][:200]
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    for fr in _all_frames(page):
        _safe_eval(fr, JS_CLOSE_POPUPS)
    _pause(page, 0.8)
    try:
        loc.click(timeout=4000)
        return "clicked after closing a pop-up"
    except Exception:  # noqa: BLE001
        pass
    if _safe_eval(frame, JS_CLICK_MARKED, default=False):
        return "pressed directly (the button was covered by a pop-up)"
    return f"failed: {first_error}"


def _click_sel(frame, selector: str, timeout: int = 4000) -> bool:
    """Real click on an element our scripts marked; a script click if something covers it."""
    try:
        frame.locator(selector).first.click(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return bool(_safe_eval(frame, JS_CLICK_SEL, selector, default=False))


def _click_follow(context, page, frame, selector: str, live, wait_s: float = 1.2):
    """Click, then follow the click into a new tab if it opened one (e.g. 'Enter the prize draw' links)."""
    n_pages = len(context.pages)
    ok = _click_sel(frame, selector, timeout=5000)
    _pause(page, wait_s)
    if len(context.pages) > n_pages:
        page = context.pages[-1]
        live.attach(page)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        _wait_settle(page, 6000)
        dismiss_cookies(page)
    return page, ok


def _tick_gate(page, frame) -> bool:
    """Tick the 'I've read the rules' box that unlocks a start button (a real click on the box itself)."""
    loc = frame.locator('[data-cb-gate="1"]').first
    try:
        loc.check(timeout=4000)
        ok = True
    except Exception:  # noqa: BLE001
        ok = bool(_safe_eval(frame, "() => {\n" + JS_HELPERS + "\n const c = __cbOne('[data-cb-gate=\"1\"]'); if (!c) return false;"
                                    " if (!c.checked) c.click(); if (!c.checked) { c.checked = true;"
                                    " c.dispatchEvent(new Event('input', {bubbles: true}));"
                                    " c.dispatchEvent(new Event('change', {bubbles: true})); } return c.checked; }",
                             default=False))
    _pause(page, 0.6)
    return ok


QUIZ_MAX_ACTIONS = 100
QUIZ_BUDGET_S = 420  # a 15-question timed quiz with the AI thinking on a CPU fits; ENTRY_TIMEOUT_S is the hard cap
QUIZ_FAIL_RE = re.compile(
    r"didn'?t (?:quite )?(?:reach|qualify|score enough|get enough)|not (?:quite )?(?:qualif|enough (?:correct|right))|"
    r"you need(?:ed)? to (?:score|get) (?:at least )?\d+|better luck next time|you didn'?t make it", re.I)
_LETTER_RE = re.compile(r"^\(?([A-Ha-h])\)?[.:]?$")
_DIGITS_RE = re.compile(r"\d+")


def _quiz_scan(page, skip=None, focus=None, dry_run: bool = False):
    """Find the quiz on the page (any frame): returns (frame, scan) - scan has 'groups', 'next', 'quizNext'."""
    with_next = (None, None)
    for fr in _all_frames(page):
        s = _safe_eval(fr, JS_QUIZ_SCAN, [list(skip or []), focus, dry_run])
        if not s:
            continue
        if s.get("groups"):
            return fr, s
        if s.get("next") and with_next[0] is None:
            with_next = (fr, s)
    return with_next


def _quiz_present(scan) -> bool:
    return bool(scan and (scan.get("groups") or scan.get("quizNext")))


def _match_quiz_option(texts: list[str], candidates: list[str], fuzzy: int) -> str | None:
    """Pick the option matching an answer. Numbers must match exactly ('2,135 m' is not '2,285 m')."""
    opts = [{"text": t, "value": t} for t in texts]
    for c in candidates:
        m = _LETTER_RE.match(c.strip())
        if m and "abcdefgh".index(m.group(1).lower()) < len(texts):
            return texts["abcdefgh".index(m.group(1).lower())]
        digits = _DIGITS_RE.findall(c.replace(",", ""))
        if digits:
            same = [t for t in texts if _DIGITS_RE.findall(t.replace(",", "")) == digits]
            if len(same) == 1:
                return same[0]
            if any(_DIGITS_RE.search(t) for t in texts):
                continue  # a numeric answer that matches none (or several) of the numeric options: don't guess
        nc = normalise(c)
        for t in texts:
            if normalise(t) == nc:
                return t
        o = choose_option(opts, [c], threshold=fuzzy)
        if o:
            return o["text"]
    return None


def _quiz_order(g: dict, ctx: Ctx, settings: dict, comp_context: dict, page_text: str,
                details: dict) -> tuple[list[str], str]:
    """Options in the order to try them: the known answer (MSE post), else the AI's pick, then the rest."""
    texts = [o["text"] for o in g["options"] if o["text"] and not o["dis"]]
    first, source = None, "a guess"
    if ctx.answers:
        first = _match_quiz_option(texts, list(ctx.answers), fuzzy=92)
        if first:
            source = "the MSE post"
    if first is None and llm.enabled(settings) and settings.get("ai_answers", True):
        res = llm.answer_question(g["question"], texts, page_text, comp_context, settings)
        rec = {"question": g["question"][:200], "options": texts[:12], "used": False}
        if res:
            rec.update(answer=res["answer"], confidence=res["confidence"], source=res["source"], reason=res["reason"])
            if res["answer"]:
                first = _match_quiz_option(texts, [res["answer"]], fuzzy=70)
                if first:
                    # In a click quiz a wrong pick only costs that question, so the AI's best guess is used whatever
                    # its confidence (the confidence is still logged).
                    source = f"AI, {res['confidence']}% sure"
                    rec["used"] = True
        details.setdefault("ai", []).append(rec)
    return ([first] if first else []) + [t for t in texts if t != first], source


def play_quiz(page, context, live, ctx: Ctx, settings: dict, comp_context: dict, details: dict, dry_run: bool,
              done_idx: set[str], deadline: float, gen: list[int]):
    """Play a click-to-answer quiz (answer tiles/buttons rather than form fields) until an entry form appears.

    Answers come from the MSE post, then the AI, then a guess; when the quiz says a pick was wrong and lets you try
    again, the next option is tried. Returns (page, best_form_or_None, summary).
    """
    steps = details["steps"]
    asked: dict[str, dict] = {}
    order: list[str] = []
    last_q: str | None = None
    last_next, streak, idle = None, 0, 0
    just_answered = False
    page_text = None
    stats = details.setdefault("quiz", {"answered": 0, "retries": 0})

    def summary(extra: str = "") -> str:
        s = f"{stats['answered']} question{'s' if stats['answered'] != 1 else ''} answered"
        if stats["retries"]:
            s += f", {stats['retries']} retr{'ies' if stats['retries'] != 1 else 'y'}"
        return s + (f"; {extra}" if extra else "")

    def form_ready():
        gen[0] += 1
        b = find_best_form(page, ctx, gen[0])
        if b and b[2] >= 4 and any(f["idx"] not in done_idx for f in b[1].get("fields", [])):
            return b
        return None

    for _ in range(QUIZ_MAX_ACTIONS):
        if time.time() > deadline:
            return page, None, summary("ran out of time")
        fr, scan = _quiz_scan(page, [q for q in order if q != last_q][-60:], last_q, dry_run)
        groups = (scan or {}).get("groups") or []
        cur = next((g for g in groups if g["question"] == last_q), None)
        if cur and not cur["locked"]:
            st = asked[last_q]
            picked = st["tried"][-1]
            mine = next((o for o in cur["options"] if o["text"] == picked), None)
            wrong = st.get("retrying") or bool(mine and mine["wrong"]) or (
                cur["wrongHint"] and not any(o["right"] for o in cur["options"]))
            if wrong:
                alt = next((t for t in st["order"] if t not in st["tried"]
                            and any(o["text"] == t and not o["dis"] for o in cur["options"])), None)
                if alt and len(st["tried"]) < 6 and cur["covered"] and scan.get("retry") and not st.get("retrying"):
                    # A "wrong - retry" message sits on top of the answers: press its Retry first.
                    steps.append(f"quiz: clicked '{scan['retry']}'")
                    _click_sel(fr, '[data-cb-retry="1"]')
                    st["retrying"] = True
                    _pause(page, 1.0)
                    continue
                if alt and len(st["tried"]) < 6 and not cur["covered"]:
                    key = next(o["key"] for o in cur["options"] if o["text"] == alt)
                    if _click_sel(fr, f'[data-cb-opt="{key}"]'):
                        st["tried"].append(alt)
                        st["retrying"] = False
                        stats["retries"] += 1
                        steps.append(f"quiz: \u201c{picked[:40]}\u201d was wrong - trying \u201c{alt[:40]}\u201d")
                        idle = 0
                        just_answered = True
                        _pause(page, 1.2)
                        continue
        b = form_ready()
        if b:
            return page, b, summary()
        new = [g for g in groups if g["question"] not in asked and not g["locked"] and not g["covered"]]
        if new:
            g = next((g for g in new if g["dialog"]), new[0])
            if page_text is None:
                page_text = _page_context(page, fr)
            live.status(f"Quiz question {stats['answered'] + 1}: {g['question'][:90]}")
            opts, source = _quiz_order(g, ctx, settings, comp_context, page_text, details)
            asked[g["question"]] = {"order": opts, "tried": opts[:1]}
            order.append(g["question"])
            last_q = g["question"]
            if not opts:
                continue
            # The AI may have taken a while: the page could have re-rendered, so find the option again by its text.
            fr2, scan2 = _quiz_scan(page, [q for q in order if q != last_q][-60:], last_q, dry_run)
            g2 = next((x for x in ((scan2 or {}).get("groups") or []) if x["question"] == last_q), None)
            key = next((o["key"] for o in (g2 or {}).get("options", []) if o["text"] == opts[0]), None)
            if not key or not _click_sel(fr2, f'[data-cb-opt="{key}"]'):
                steps.append(f"quiz: couldn't click \u201c{opts[0][:40]}\u201d")
                continue
            stats["answered"] += 1
            idle = streak = 0
            just_answered = True
            steps.append(f"quiz Q{stats['answered']}: \u201c{g['question'][:90]}\u201d \u2192 \u201c{opts[0][:50]}\u201d "
                         f"({source})")
            _pause(page, 1.2)
            continue
        if scan and scan.get("next"):
            if just_answered:
                # Many quizzes move on by themselves after showing the result; pressing "Next" as well would
                # skip a question.
                just_answered = False
                _pause(page, 1.5)
                continue
            label = scan["next"]
            streak = streak + 1 if label == last_next else 1
            last_next = label
            if streak > 3:
                return page, None, summary(f"'{label}' didn't move the quiz on")
            steps.append(f"quiz: clicked '{label}'")
            live.status(f"Quiz: {label}")
            page, _ok = _click_follow(context, page, fr, '[data-cb-next="1"]', live)
            idle = 0
            continue
        idle += 1
        if idle > 4:
            break
        _pause(page, 1.5)
    return page, None, summary()


def reveal_consented_embed(page) -> str | None:
    """If the entry form is a third-party embed waiting for consent ("Want to see this content?"), allow it."""
    for fr in _all_frames(page):
        label = _safe_eval(fr, JS_EMBED_CONSENT)
        if label and _click_marked(fr):
            _pause(page, 2)
            _wait_settle(page, 8000)
            prime_iframes(page)
            return label
    return None


def prime_iframes(page) -> None:
    """Make lazily-loaded iframes load now, then give them a moment, so their forms can be found."""
    n = _safe_eval(page.main_frame, JS_PRIME_IFRAMES, default=0) or 0
    if not n:
        return
    deadline = time.time() + 6
    while time.time() < deadline and any(not f.url for f in page.frames if not f.is_detached()):
        _pause(page, 0.5)
    _wait_settle(page, 5000)


def scroll_through(page) -> None:
    """Scroll down the page like a reader would, so content that loads on view (embeds, forms) appears."""
    try:
        height = page.evaluate("() => document.body ? document.body.scrollHeight : 0") or 0
        y = 0
        while y < min(height, 30000):
            y += 900
            page.mouse.wheel(0, 900)
            _pause(page, 0.25)
        _pause(page, 1.5)
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:  # noqa: BLE001
        pass
    _wait_settle(page, 5000)
    prime_iframes(page)


def terms_links(page) -> list[str]:
    links: list[str] = []
    for fr in _all_frames(page):
        links += _safe_eval(fr, JS_TERMS_LINKS, default=[]) or []
    return list(dict.fromkeys(links))[:3]


def fetch_terms_text(page, links: list[str] | None = None) -> str:
    links = terms_links(page) if links is None else links
    texts = []
    with guarded_client(headers={"User-Agent": get_config().user_agent}, timeout=15) as client:
        for href in links[:3]:
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


def _server_errors(responses: list) -> str:
    """'HTTP 403 from a.klaviyo.com/client/subscriptions: {...}' for error replies seen after submitting."""
    from urllib.parse import urlparse

    out = []
    for r in responses[-3:]:
        try:
            u = urlparse(r.url)
            try:
                body = re.sub(r"\s+", " ", r.text())[:220]
            except Exception:  # noqa: BLE001
                body = ""
            out.append(f"HTTP {r.status} from {u.netloc}{u.path[:60]}" + (f": {body}" if body else ""))
        except Exception:  # noqa: BLE001
            continue
    return "; ".join(out)


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
        t_start, ai_start = time.monotonic(), dict(llm.STATS)
        context.set_default_timeout(15000)
        # Untrusted pages must never reach the NAS/LAN: abort any request (incl. subresources/redirects) to them.
        context.route("**/*", lambda route: route.continue_() if is_public_url(route.request.url, allow_inline=True)
                      else route.abort("blockedbyclient"))
        page = context.new_page()
        # Server errors after we press submit (e.g. a sign-up API refusing the entry): the page itself often only says
        # "An error occurred", so keep the real reply to show with the result.
        sent_errors: list = []
        watching = [False]

        def _on_response(r) -> None:
            try:
                if watching[0] and r.status >= 400 and r.request.resource_type in ("xhr", "fetch", "document"):
                    sent_errors.append(r)
            except Exception:  # noqa: BLE001
                pass

        context.on("response", _on_response)
        live = LiveView(context, bool(settings.get("live_view", True)), str(comp_context.get("title") or ""))
        live.attach(page)
        live.status(f"Opening {url[:120]}")
        details["steps"] = _LiveSteps(live)
        gen = [0]
        checked_urls: set[str] = set()
        quiz_deadline = time.time() + QUIZ_BUDGET_S
        play_quizzes = bool(settings.get("play_quizzes", True))

        def best_form():
            gen[0] += 1
            return find_best_form(page, ctx, gen[0])

        def clause_check() -> str | None:
            """Automation-ban check: page text + linked T&Cs. Re-runs whenever new T&C links have appeared
            (e.g. after scrolling or revealing the form), so a late-loading T&C link is never missed."""
            if not settings.get("respect_automation_clauses", True):
                return None
            links = terms_links(page)
            key = page.url.split("#")[0] + "|" + ",".join(sorted(links))
            if key in checked_urls:
                return None
            checked_urls.add(key)
            return find_automation_clause(_page_text(page)) or find_automation_clause(fetch_terms_text(page, links))

        def tc_blocked(where: str = "") -> AttemptResult:
            res.result = "tc_blocked"
            res.message = f"T&Cs{where} ban automated entries: \u201c{clause[:300]}\u201d"
            return res

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
            live.status("Looking for the entry form")
            prime_iframes(page)
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
                # Consent managers often appear late; answering them can be what loads the entry form.
                late = dismiss_cookies(page)
                if late:
                    details["cookies"] = list(details.get("cookies") or []) + late
                    prime_iframes(page)
                    best = best_form()
            if not best or best[2] < 4:
                allowed = reveal_consented_embed(page)
                if allowed:
                    details["steps"].append(f"allowed the embedded entry form ('{allowed}')")
                    if dismiss_cookies(page):
                        prime_iframes(page)
                    best = best_form()
            if not best or best[2] < 4:
                scroll_through(page)
                best = best_form()
                if best and best[2] >= 4:
                    details["steps"].append("form appeared after scrolling the page")
            quiz_ready = False
            if (not best or best[2] < 4) and play_quizzes:
                quiz_ready = bool((_quiz_scan(page, dry_run=dry_run)[1] or {}).get("groups"))
            if (not best or best[2] < 4) and not quiz_ready:
                # Click an "Enter now"/"Start the quiz"-style reveal. Never a submit button; in dry-run only plain
                # links and quiz/form openers. If one leads nowhere (e.g. it only scrolls), try the next-best one.
                for _attempt in range(3):
                    hit = None
                    for fr in _all_frames(page):
                        r = _safe_eval(fr, JS_REVEAL, dry_run)
                        if r:
                            hit = (fr, r)
                            break
                    if not hit:
                        break
                    fr, r = hit
                    if r.get("gate"):
                        ticked = _tick_gate(page, fr)
                        details["steps"].append(f"ticked '{r['gate'][:80]}' to unlock '{r['label']}'" if ticked
                                                else f"couldn't tick '{r['gate'][:80]}'")
                    details["steps"].append(f"clicked '{r['label']}'")
                    page, _ok = _click_follow(context, page, fr, '[data-cb-reveal="1"]', live, wait_s=2.5)
                    _wait_settle(page, 6000)
                    dismiss_cookies(page)
                    clause = clause_check()
                    if clause:
                        return tc_blocked()
                    best = best_form()
                    if best and best[2] >= 4:
                        break
                    if play_quizzes and _quiz_present(_quiz_scan(page, dry_run=dry_run)[1]):
                        break
            quiz_summary = None
            if (not best or best[2] < 4) and play_quizzes:
                _qf, qscan = _quiz_scan(page, dry_run=dry_run)
                if _quiz_present(qscan):
                    live.status("Playing the quiz")
                    page, qbest, quiz_summary = play_quiz(page, context, live, ctx, settings, comp_context, details,
                                                          dry_run, set(), quiz_deadline, gen)
                    details["steps"].append(f"quiz: {quiz_summary}")
                    if qbest:
                        best = qbest
                        details["steps"].append("the entry form appeared after the quiz")
            if not best or best[2] < 4:
                text = _page_text(page)
                if CLOSED_RE.search(text):
                    res.result, res.message = "closed", "Page says the competition has closed"
                elif quiz_summary is not None and QUIZ_FAIL_RE.search(text):
                    m = QUIZ_FAIL_RE.search(text)
                    res.result = "failed"
                    res.message = f"Quiz score too low to unlock the entry ({quiz_summary}): \u201c{m.group(0)}\u201d"
                elif quiz_summary is not None:
                    res.result, res.message = "failed", f"Played the quiz ({quiz_summary}) but no entry form appeared"
                else:
                    res.result, res.message = "failed", "No entry form found on the page"
                res.final_url = page.url
                res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_noform.jpg")
                return res

            # The form may have appeared late (scroll/reveal/embed) along with new T&C links: check them again.
            clause = clause_check()
            if clause:
                return tc_blocked()

            cap = detect_captcha(page)
            if cap:
                res.result, res.message = "captcha", f"CAPTCHA on the form ({cap}) - enter manually"
                res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_captcha.jpg")
                return res

            done_idx: set[str] = set()
            submitted_sigs: list[frozenset] = []
            retried_required = False
            for step in range(1, MAX_FORM_PAGES + 1):
                frame, container, score = best
                plan_all: list = []
                # Fill in rounds: many forms only reveal later fields (or the submit button) once earlier ones are set.
                for _round in range(4):
                    fields = [f for f in _to_fields(container) if f.idx not in done_idx]
                    if not fields:
                        break
                    plan = plan_form(fields, ctx)
                    if llm.enabled(settings) and (settings.get("ai_answers", True) or settings.get("ai_tiebreakers")):
                        if any(a.note in AI_QUESTION_NOTES or a.note == "question with no known answer"
                               or a.note == "creative / tie-breaker answer" for _, a in plan):
                            live.status("Asking the AI about the competition question")
                        plan = _ai_fill(page, frame, plan, settings, comp_context, details)
                    unknown = [(f, a) for f, a in plan if a.kind == "unknown"]
                    if unknown and llm.enabled(settings) and settings.get("llm_for_forms", True):
                        # A field already recognised (e.g. your phone, which isn't in your profile) is beyond the AI:
                        # it can only map fields to profile details. Don't spend its time on those.
                        payload = [{"id": f.idx, "type": f.type if f.tag != "radio" else "radio",
                                    "label": f.human_label, "required": f.required, "context": f.context[:200],
                                    "options": [o.get("text") for o in f.options][:40]}
                                   for f, a in unknown
                                   if not a.note.startswith(("captcha", "AI ", "profile has no"))]
                        suggestions = llm.map_fields(payload, comp_context, PROFILE_KEYS, settings) if payload else {}
                        new_plan = []
                        for f, a in plan:
                            if a.kind == "unknown" and f.idx in suggestions:
                                suggested = apply_llm_action(f, suggestions[f.idx], ctx)
                                if suggested.kind == "unknown" and not suggested.note:
                                    suggested.note = a.note  # keep the reason the rules gave
                                a = suggested
                            new_plan.append((f, a))
                        plan = new_plan
                        unknown = [(f, a) for f, a in plan if a.kind == "unknown"]
                    if any(a.note.startswith("captcha") for _, a in unknown):
                        res.result, res.message = "captcha", "Anti-spam check on the form (e.g. a sum) - enter manually"
                        res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_captcha.jpg")
                        return res
                    if unknown:
                        res.result = "needs_input"
                        res.message = "Couldn't fill: " + "; ".join(
                            f"{f.human_label} ({a.note})" for f, a in unknown)[:900]
                        details["fields"] += [{"label": f.human_label, "action": a.kind, "source": a.source,
                                               "note": a.note} for f, a in plan]
                        res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_needsinput.jpg")
                        return res
                    live.status(f"Filling {sum(1 for _, a in plan if a.kind != 'skip')} field(s)")
                    failed = []
                    for f, a in plan:
                        done_idx.add(f.idx)
                        if a.kind == "skip":
                            continue
                        ok = _apply(frame, f, a)
                        details["fields"].append({"label": f.human_label, "action": a.kind, "source": a.source,
                                                  "note": a.note, "ok": ok,
                                                  "value": a.value if a.source in ("answer", "llm", "email",
                                                                                   "email_confirm", "ai") else None})
                        if not ok and (f.required or a.source == "answer"):
                            failed.append(f.human_label)
                    if failed:
                        res.result, res.message = "failed", "Couldn't set: " + ", ".join(failed)
                        res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_fillfail.jpg")
                        return res
                    plan_all += plan
                    _safe_eval(frame, JS_BLUR)
                    _pause(page, 0.8)
                    gen[0] += 1
                    refreshed = _refresh_container(frame, done_idx, f"g{gen[0]}r_")
                    if refreshed is None:
                        break
                    container = refreshed
                    if _round:
                        details["steps"].append(f"step {step}: more fields appeared after filling")

                # A field on this page: earlier pages' fields may be hidden now, or gone.
                cur_ids = [f["idx"] for f in container.get("fields", []) if f["idx"] in done_idx]
                sample_idx = cur_ids[0] if cur_ids else next(iter(done_idx), None)
                invalid = _safe_eval(frame, JS_VALIDITY, sample_idx, default=[]) if sample_idx else []
                if invalid:
                    res.result, res.message = "needs_input", "Form still invalid: " + ", ".join(invalid)[:500]
                    res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_invalid.jpg")
                    return res

                res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_s{step}_before.jpg")
                if dry_run:
                    res.result = "dry_run_ok"
                    res.final_url = page.url
                    res.message = f"Dry run: filled {sum(1 for _, a in plan_all if a.kind != 'skip')} fields; not submitted"
                    return res

                submit_label = _safe_eval(frame, JS_MARK_SUBMIT, sample_idx) if sample_idx else None
                if not submit_label:
                    res.result, res.message = "failed", "Couldn't find the submit button"
                    res.screenshot_before = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_nosubmit.jpg")
                    return res
                clause = clause_check()  # last look at the T&Cs before anything is sent
                if clause:
                    return tc_blocked()
                before_text = set(_page_text(page).splitlines())
                before_url = page.url
                submitted_sigs.append(_form_sig(container))
                details["steps"].append(f"step {step}: clicked '{submit_label}'")
                n_pages = len(context.pages)
                sent_errors.clear()
                watching[0] = True
                how = _click_submit(page, frame)
                if how.startswith("failed"):
                    res.result, res.message = "failed", f"Submit click failed: {how[7:]}"[:500]
                    return res
                if how != "click":
                    details["steps"].append(f"step {step}: submit {how}")
                _pause(page, 2)
                if len(context.pages) > n_pages:
                    page = context.pages[-1]
                    live.attach(page)
                _wait_settle(page, 12000)
                _pause(page, 1.5)

                def read_outcome():
                    after = _page_text(page)
                    return "\n".join(ln for ln in after.splitlines() if ln not in before_text)

                new_text = read_outcome()
                if not (SUCCESS_RE.search(new_text) or ALREADY_RE.search(new_text) or ERROR_RE.search(new_text)):
                    _pause(page, 3.5)  # many forms show their result a moment after the request finishes
                    new_text = read_outcome()
                res.final_url = page.url
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
                server = _server_errors(sent_errors)
                if server:
                    details["server_errors"] = server
                nxt = best_form()
                # A paged form (Next -> Next -> Submit) carries on in the same <form>, even when the next page is
                # only tick boxes, which wouldn't look like an entry form on its own.
                gen[0] += 1
                own = _own_next_page(frame, container, done_idx, f"g{gen[0]}o_")
                own_score = _score_container(own, ctx) if own else -100
                paged = own is not None and own_score > -20
                if paged:
                    nxt = (frame, own, own_score)
                nsig = _form_sig(nxt[1]) if nxt else frozenset()
                same_form = bool(nxt) and any(_same_form(nsig, s) for s in submitted_sigs)
                # Fields never seen before (one-question-at-a-time forms keep earlier questions on the page).
                fresh = bool(nsig - frozenset().union(*submitted_sigs))
                new_fields = [f for f in (nxt[1]["fields"] if nxt else []) if f["idx"] not in done_idx]
                # The fields just sent have gone and new ones are showing: that's the next page, not this one with
                # errors, so instructions on it ("please complete the tick boxes", "* are required") aren't errors.
                moved_on = not same_form and not (err and HARD_ERROR_RE.search(new_text))
                if new_fields and (nxt[2] >= 4 or paged) and (moved_on or (fresh and not err)):
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
                if not err and play_quizzes:
                    # The next screen is a click-to-answer question (a quiz, or a "rate us 1-5" step).
                    _qf, qscan = _quiz_scan(page, dry_run=dry_run)
                    if qscan and qscan.get("groups"):
                        details["steps"].append(f"step {step + 1}: questions to answer")
                        page, qbest, qsum = play_quiz(page, context, live, ctx, settings, comp_context, details,
                                                      dry_run, done_idx, quiz_deadline, gen)
                        details["steps"].append(f"quiz: {qsum}")
                        if qbest:
                            clause = clause_check()
                            if clause:
                                return tc_blocked(f" on step {step + 1}")
                            if detect_captcha(page):
                                res.result, res.message = "captcha", f"CAPTCHA on form step {step + 1} - finish manually"
                                return res
                            best = qbest
                            continue
                        new_text = read_outcome()
                        res.final_url = page.url
                        res.screenshot_after = _shot(page, cfg.screenshot_dir / f"{shot_prefix}_s{step}_quiz.jpg")
                        if ALREADY_RE.search(new_text):
                            res.result, res.message = "entered", "Site says you've already entered"
                            return res
                        if SUCCESS_RE.search(new_text):
                            m = SUCCESS_RE.search(new_text)
                            res.result, res.message = "entered", f"Confirmation seen: \u201c{m.group(0)[:120]}\u201d"
                            return res
                        res.result = "unverified"
                        res.message = f"Submitted and answered the follow-up questions ({qsum}); no explicit confirmation"
                        return res
                if err:
                    # "This field is required": tick the obvious option in untouched groups and send once more,
                    # like a person correcting the form.
                    if not retried_required and same_form and REQUIRED_ERR_RE.search(new_text):
                        gen[0] += 1
                        cur = _refresh_container(frame, done_idx, f"g{gen[0]}q_")
                        fixes = _required_fixes(cur) if cur else []
                        applied = []
                        for f, a in fixes:
                            if _apply(frame, f, a):
                                applied.append(f.human_label)
                                details["fields"].append({"label": f.human_label, "action": a.kind, "source": a.source,
                                                          "note": a.note, "ok": True})
                        if applied:
                            retried_required = True
                            _safe_eval(frame, JS_BLUR)
                            details["steps"].append(
                                f"step {step}: site said fields were required - set {', '.join(applied)[:200]}; sending again")
                            submitted_sigs.clear()
                            best = (frame, cur, score)
                            continue
                    snippet = new_text[max(0, err.start() - 60): err.end() + 80].replace("\n", " ")
                    res.result, res.message = "failed", f"Form reported a problem: \u201c{snippet.strip()}\u201d" + (
                        f" - the site's server replied {server}" if server else "")
                    res.message = res.message[:900]
                    return res
                if page.url.split("#")[0] != before_url.split("#")[0] or not same_form:
                    res.result, res.message = "unverified", "Submitted; page changed but no explicit confirmation text"
                    return res
                res.result, res.message = "failed", ("Submitted but nothing changed on the page" + (
                    f" - the site's server replied {server}" if server else ""))[:900]
                return res
            res.result, res.message = "unverified", f"Gave up after {MAX_FORM_PAGES} form pages"
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
            details["steps"] = list(details.get("steps") or [])  # plain list (the live wrapper isn't picklable)
            details["timing"] = {"seconds": round(time.monotonic() - t_start),
                                 "ai_calls": llm.STATS["calls"] - ai_start["calls"],
                                 "ai_seconds": round(llm.STATS["seconds"] - ai_start["seconds"])}
            res.details = details
            live.status("Finished", result=f"{res.result}: {res.message[:200]}")
            _pause(page, 0.6)  # let the last frames through before closing
            live.detach()
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


def result_as_dict(r: AttemptResult) -> dict:
    return asdict(r)


# --------------------------------------------------------------------------- isolation / watchdog

ENTRY_TIMEOUT_S = 600  # includes time for the AI to answer questions (and play whole quizzes) on a CPU
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def enter_isolated(url: str, ctx: Ctx, settings: dict, shot_prefix: str, comp_context: dict,
                   timeout: float = ENTRY_TIMEOUT_S) -> AttemptResult:
    """Run one entry in its own process with a hard time limit, so no single page can stall a whole run."""
    import pickle
    import subprocess
    import sys

    payload = pickle.dumps({"url": url, "ctx": ctx, "settings": settings, "shot_prefix": shot_prefix,
                            "comp_context": comp_context})
    proc = subprocess.Popen([sys.executable, "-m", "app.entry.worker"], cwd=str(_PROJECT_ROOT),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        out, _ = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()  # Playwright's driver sees its pipe close and shuts Chromium down with it
        try:
            proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        log.warning("entry timed out after %ss: %s", timeout, url)
        return AttemptResult(result="error", message=f"Timed out after {int(timeout)}s (the page never finished "
                                                     "responding) - skipped")
    if proc.returncode != 0 or not out:
        return AttemptResult(result="error", message=f"Browser process ended unexpectedly (exit {proc.returncode})")
    try:
        return pickle.loads(out)
    except Exception as exc:  # noqa: BLE001
        return AttemptResult(result="error", message=f"Couldn't read the browser result: {exc}"[:500])
