"""Optional OpenAI-compatible LLM client (works with Ollama, OpenAI, Azure OpenAI v1, OpenRouter, LM Studio...).

Personal details are never sent: prompts only contain field descriptions, profile *key names* and public
competition text. Email classification (opt-in) sends the sender, subject and a short snippet.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from .config import get_config

log = logging.getLogger("compbot.llm")


def enabled() -> bool:
    return get_config().llm_enabled


def chat_json(system: str, user: str, timeout: float = 90.0) -> dict | None:
    cfg = get_config()
    if not cfg.llm_enabled:
        return None
    url = cfg.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.llm_api_key:
        headers["Authorization"] = f"Bearer {cfg.llm_api_key}"
        headers["api-key"] = cfg.llm_api_key
    payload = {
        "model": cfg.llm_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        if r.status_code == 400 and "response_format" in r.text:
            payload.pop("response_format")
            r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", content, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM call failed: %s", exc)
        return None


POST_SYSTEM = (
    "You analyse UK competition forum posts. Return JSON with keys: "
    "entry_url (string or null - the page where the entry form is), answers (list of strings - answers to any "
    "competition question, empty if unknown), question (string or null), method (one of web_form, email, social, "
    "widget, account, purchase, unknown), email_to (string or null), email_subject (string or null). "
    "Only use information present in the post. Never guess an answer."
)


def analyse_post(title: str, text: str, links: list[str]) -> dict | None:
    user = f"TITLE: {title}\n\nPOST:\n{text[:6000]}\n\nLINKS:\n" + "\n".join(links[:30])
    return chat_json(POST_SYSTEM, user)


FORM_SYSTEM = (
    "You help fill UK competition entry forms. You get a JSON list of form fields that simple rules could not map, "
    "plus competition context. For each field id return an action. Allowed actions: "
    '{"profile": "<key>"} where key is one of PROFILE_KEYS; {"text": "<literal text>"} for answers to the '
    'competition question or short tie-breakers (max 25 words, positive, family friendly); {"option": "<exact option '
    'text>"} for selects/radio groups; {"check": true|false} for checkboxes; {"skip": true} if the field is optional '
    "and irrelevant. Only answer the competition question with the supplied known answers when they fit; if the "
    'question cannot be answered from context return {"unknown": true}. Respond as {"actions": {"<id>": {...}}}.'
)


def map_fields(fields: list[dict], context: dict, profile_keys: list[str]) -> dict:
    user = json.dumps({"PROFILE_KEYS": profile_keys, "competition": context, "fields": fields}, ensure_ascii=False)
    res = chat_json(FORM_SYSTEM, user[:12000]) or {}
    actions = res.get("actions") if isinstance(res, dict) else None
    return actions if isinstance(actions, dict) else {}


EMAIL_SYSTEM = (
    "Classify an email received by a UK competition entrant. Return JSON {\"category\": one of possible_win, loss, "
    "confirm, entry_ack, promo, scam_suspect, other, \"reason\": short string}. possible_win means the email says "
    "the recipient HAS won a specific prize (not 'chance to win' marketing). scam_suspect means it asks for fees, "
    "bank details or looks like phishing."
)


def classify_email(from_addr: str, subject: str, snippet: str, matched_comp: str | None) -> dict | None:
    user = json.dumps({"from": from_addr, "subject": subject, "snippet": snippet[:1500],
                       "matched_competition": matched_comp}, ensure_ascii=False)
    return chat_json(EMAIL_SYSTEM, user)
