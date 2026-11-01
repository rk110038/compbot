"""AI helper: a local Ollama server (preferred) or any OpenAI-compatible endpoint.

What is sent: competition text, form field *labels* and option lists, and the public text of the competition page.
Your personal details are never sent - the AI only ever names a profile *key* (e.g. "first_name") and compbot fills
the value locally. Email classification (opt-in) sends the sender, subject and a short snippet.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from .config import get_config

log = logging.getLogger("compbot.llm")

# Shown in the Settings model picker. Sizes are the Ollama download sizes (Q4 quantisation).
RECOMMENDED_MODELS: list[dict] = [
    {"name": "gemma4:12b", "size": "7.7 GB", "vram": "10-12 GB GPU",
     "note": "Recommended: best mix of general knowledge and reading comprehension for quiz questions"},
    {"name": "qwen3.5:9b", "size": "6.6 GB", "vram": "8 GB GPU", "note": "Strong reasoning; good choice for 8 GB cards"},
    {"name": "gemma4:e4b", "size": "6.6 GB", "vram": "8 GB GPU", "note": "Lighter Gemma 4, quick"},
    {"name": "qwen3.5:4b", "size": "3.3 GB", "vram": "4-6 GB GPU or CPU", "note": "Small and fast; fine for reading answers off the page"},
    {"name": "gemma4:26b", "size": "16 GB", "vram": "20-24 GB GPU", "note": "Most knowledgeable that fits a 24 GB card"},
    {"name": "qwen3.5:27b", "size": "17 GB", "vram": "24 GB GPU", "note": "Very capable; needs a 24 GB card"},
]


# --------------------------------------------------------------------------- configuration

def model_name(settings: dict | None = None) -> str | None:
    return ((settings or {}).get("llm_model") or "").strip() or get_config().llm_model


def enabled(settings: dict | None = None) -> bool:
    return bool(get_config().llm_enabled and model_name(settings))


def ollama_url() -> str | None:
    cfg = get_config()
    if cfg.ollama_url:
        return cfg.ollama_url
    if cfg.llm_base_url and re.search(r":11434(/|$)", cfg.llm_base_url):
        return re.sub(r"/v1/?$", "", cfg.llm_base_url.rstrip("/"))
    return None


# --------------------------------------------------------------------------- Ollama management

def ollama_status() -> dict:
    """{'ok': bool, 'version': str|None, 'models': [...], 'error': str|None}"""
    base = ollama_url()
    if not base:
        return {"ok": False, "version": None, "models": [], "error": "OLLAMA_URL is not set"}
    try:
        with httpx.Client(timeout=8) as c:
            version = c.get(f"{base}/api/version").json().get("version")
            models = c.get(f"{base}/api/tags").json().get("models", [])
            try:
                loaded = {m.get("name"): m for m in c.get(f"{base}/api/ps").json().get("models", [])}
            except Exception:  # noqa: BLE001
                loaded = {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "version": None, "models": [], "error": f"Can't reach Ollama at {base}: {exc}"}
    out = []
    for m in models:
        d = m.get("details") or {}
        lm = loaded.get(m.get("name")) or {}
        vram, size = lm.get("size_vram"), lm.get("size")
        out.append({"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 1),
                    "params": d.get("parameter_size", ""), "quant": d.get("quantization_level", ""),
                    "loaded": bool(lm),
                    "gpu_pct": round(100 * vram / size) if vram is not None and size else None})
    out.sort(key=lambda m: m["name"])
    return {"ok": True, "version": version, "models": out, "error": None}


_pulls: dict[str, dict] = {}


def pull_progress() -> dict[str, dict]:
    return dict(_pulls)


def pull_model(name: str) -> None:
    """Download a model (blocking; run it in a thread). Progress is exposed via pull_progress()."""
    base = ollama_url()
    _pulls[name] = {"status": "starting", "pct": 0, "done": False, "error": None, "started": time.time()}
    try:
        with httpx.Client(timeout=httpx.Timeout(30, read=600)) as c, c.stream(
                "POST", f"{base}/api/pull", json={"model": name, "stream": True}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.read().decode(errors='replace')[:200]}")
            for line in r.iter_lines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    raise RuntimeError(ev["error"])
                p = _pulls[name]
                p["status"] = ev.get("status", p["status"])
                if ev.get("total"):
                    p["pct"] = int(100 * (ev.get("completed") or 0) / ev["total"])
                if ev.get("status") == "success":
                    p.update(done=True, pct=100)
        _pulls[name].update(done=True, status="success", pct=100)
    except Exception as exc:  # noqa: BLE001
        log.warning("pull %s failed: %s", name, exc)
        _pulls[name].update(done=True, error=str(exc)[:300], status="failed")


def delete_model(name: str) -> str:
    base = ollama_url()
    r = httpx.request("DELETE", f"{base}/api/delete", json={"model": name}, timeout=30)
    return "removed" if r.status_code < 300 else f"HTTP {r.status_code}: {r.text[:200]}"


# --------------------------------------------------------------------------- chat

def chat_json(system: str, user: str, settings: dict | None = None, schema: dict | None = None,
              timeout: float = 150.0) -> dict | None:
    """One JSON-only chat turn. Uses Ollama's native API when available (JSON schema output, no 'thinking')."""
    cfg = get_config()
    model = model_name(settings)
    if not (cfg.llm_enabled and model):
        return None
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    base = ollama_url()
    try:
        if base:
            payload = {"model": model, "messages": messages, "stream": False, "format": schema or "json",
                       "think": False, "keep_alive": "30m", "options": {"temperature": 0, "num_ctx": 8192}}
            r = httpx.post(f"{base}/api/chat", json=payload, timeout=timeout)
            if r.status_code == 400 and "think" in r.text.lower():
                payload.pop("think")
                r = httpx.post(f"{base}/api/chat", json=payload, timeout=timeout)
            r.raise_for_status()
            content = (r.json().get("message") or {}).get("content") or ""
        else:
            headers = {"Content-Type": "application/json"}
            if cfg.llm_api_key:
                headers["Authorization"] = "Bearer " + cfg.llm_api_key
                headers["api-key"] = cfg.llm_api_key
            payload = {"model": model, "messages": messages, "temperature": 0,
                       "response_format": {"type": "json_object"}}
            url = cfg.llm_base_url.rstrip("/") + "/chat/completions"
            r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code == 400 and "response_format" in r.text:
                payload.pop("response_format")
                r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
        m = re.search(r"\{.*\}", content, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM call failed: %s", exc)
        return None


# --------------------------------------------------------------------------- tasks

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": ["string", "null"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "source": {"type": "string", "enum": ["page", "knowledge", "none"]},
        "reason": {"type": "string"},
    },
    "required": ["answer", "confidence", "source", "reason"],
}

ANSWER_SYSTEM = (
    "You answer the question on a UK consumer competition entry form. Use the competition page text first (the "
    "answer is very often stated there, e.g. in the description of the prize or the promoter), then your general "
    "knowledge. If options are given, the answer MUST be exactly one of the options, copied verbatim. Keep free-text "
    "answers short (a word, name, number or short phrase). confidence is your honest probability (0-100) that the "
    "answer is correct. If you cannot work it out, return answer null with low confidence - never guess wildly. "
    'Reply as JSON: {"answer": ..., "confidence": ..., "source": "page"|"knowledge"|"none", "reason": "<short>"}'
)


def answer_question(question: str, options: list[str] | None, page_text: str, context: dict,
                    settings: dict | None = None) -> dict | None:
    user = json.dumps({
        "question": question[:500],
        "options": [o for o in (options or []) if o][:30] or None,
        "competition": {"title": context.get("title"), "post_excerpt": (context.get("post_excerpt") or "")[:800]},
        "page_text": page_text[:7000],
    }, ensure_ascii=False)
    res = chat_json(ANSWER_SYSTEM, user, settings, schema=ANSWER_SCHEMA)
    if not isinstance(res, dict):
        return None
    ans = res.get("answer")
    if ans is not None and not isinstance(ans, str):
        ans = str(ans)
    if ans and ans.strip().lower() in {"null", "none", "unknown", "n/a", "na", "not stated", "-"}:
        ans = None
    try:
        conf = int(res.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0
    return {"answer": (ans or "").strip() or None, "confidence": max(0, min(100, conf)),
            "source": res.get("source") or "none", "reason": str(res.get("reason") or "")[:200]}


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["correct", "incorrect", "unsure"]},
                   "reason": {"type": "string"}},
    "required": ["verdict", "reason"],
}

VERIFY_SYSTEM = (
    "You double-check a proposed answer to a UK competition question. Read the question carefully and check the "
    "answer addresses exactly what is asked (e.g. the watch case, not the dial; the town, not the county). Use the "
    "page text and your general knowledge. If the page simply doesn't mention it and you can't tell from knowledge, "
    "say unsure - only say incorrect when you have a reason to think the answer is wrong. "
    'Reply as JSON: {"verdict": "correct"|"incorrect"|"unsure", "reason": "<short>"}'
)


def verify_answer(question: str, answer: str, options: list[str] | None, page_text: str,
                  settings: dict | None = None) -> dict | None:
    """Independent second look at an answer before it's used (catches 'right fact, wrong part' mistakes)."""
    user = json.dumps({"question": question[:500], "proposed_answer": answer, "options": options or None,
                       "page_text": page_text[:7000]}, ensure_ascii=False)
    res = chat_json(VERIFY_SYSTEM, user, settings, schema=VERIFY_SCHEMA)
    if not isinstance(res, dict) or res.get("verdict") not in ("correct", "incorrect", "unsure"):
        return None
    return {"verdict": res["verdict"], "reason": str(res.get("reason") or "")[:200]}


TIEBREAK_SYSTEM = (
    "Write a short, warm, original answer for a UK competition tie-breaker or 'tell us why' box. Plain text, no "
    "emojis, no hashtags, no personal details or names, positive and family friendly. Respect the word limit. "
    'Reply as JSON: {"text": "..."}'
)


def write_tiebreaker(prompt: str, context: dict, max_words: int, settings: dict | None = None) -> str | None:
    user = json.dumps({"prompt": prompt[:400], "prize": context.get("title"),
                       "max_words": max(4, max_words - 3)}, ensure_ascii=False)
    res = chat_json(TIEBREAK_SYSTEM, user, settings, schema={"type": "object", "properties": {"text": {"type": "string"}},
                                                             "required": ["text"]})
    text = re.sub(r"\s+", " ", str((res or {}).get("text") or "")).strip()
    if not text:
        return None
    words = text.split()
    if len(words) <= max_words:
        return text
    # Too long: keep whole sentences within the limit rather than stopping mid-sentence.
    cut = " ".join(words[:max_words])
    end = max(cut.rfind("."), cut.rfind("!"))
    return cut[: end + 1] if end > len(cut) // 2 else cut.rstrip(",;:- ") + "."


POST_SYSTEM = (
    "You analyse UK competition forum posts. Return JSON with keys: "
    "entry_url (string or null - the page where the entry form is), answers (list of strings - answers to any "
    "competition question, empty if unknown), question (string or null), method (one of web_form, email, social, "
    "widget, account, purchase, unknown), email_to (string or null), email_subject (string or null). "
    "Only use information present in the post. Never guess an answer."
)


def analyse_post(title: str, text: str, links: list[str], settings: dict | None = None) -> dict | None:
    user = f"TITLE: {title}\n\nPOST:\n{text[:6000]}\n\nLINKS:\n" + "\n".join(links[:30])
    return chat_json(POST_SYSTEM, user, settings)


FORM_SYSTEM = (
    "You help fill UK competition entry forms. You get a JSON list of form fields that simple rules could not map, "
    "plus competition context. For each field id return an action. Allowed actions: "
    '{"profile": "<key>"} where key is one of PROFILE_KEYS; {"text": "<literal text>"} for answers to the '
    'competition question or short tie-breakers (max 25 words, positive, family friendly); {"option": "<exact option '
    'text>"} for selects/radio groups; {"check": true|false} for checkboxes; {"skip": true} if the field is optional '
    "and irrelevant. Only answer the competition question with the supplied known answers when they fit; if the "
    'question cannot be answered from context return {"unknown": true}. Respond as {"actions": {"<id>": {...}}}.'
)


def map_fields(fields: list[dict], context: dict, profile_keys: list[str], settings: dict | None = None) -> dict:
    user = json.dumps({"PROFILE_KEYS": profile_keys, "competition": context, "fields": fields}, ensure_ascii=False)
    res = chat_json(FORM_SYSTEM, user[:12000], settings) or {}
    actions = res.get("actions") if isinstance(res, dict) else None
    return actions if isinstance(actions, dict) else {}


EMAIL_SYSTEM = (
    "Classify an email received by a UK competition entrant. Return JSON {\"category\": one of possible_win, loss, "
    "confirm, entry_ack, promo, scam_suspect, other, \"reason\": short string}. possible_win means the email says "
    "the recipient HAS won a specific prize (not 'chance to win' marketing). scam_suspect means it asks for fees, "
    "bank details or looks like phishing."
)


def classify_email(from_addr: str, subject: str, snippet: str, matched_comp: str | None,
                   settings: dict | None = None) -> dict | None:
    user = json.dumps({"from": from_addr, "subject": subject, "snippet": snippet[:1500],
                       "matched_competition": matched_comp}, ensure_ascii=False)
    return chat_json(EMAIL_SYSTEM, user, settings)


def self_test(settings: dict | None = None) -> str:
    """Quick end-to-end check used by the Settings page."""
    t0 = time.time()
    res = answer_question("What is the capital city of Scotland?", ["Glasgow", "Edinburgh", "Aberdeen"],
                          "Win a weekend break in Scotland's capital city.", {"title": "Test"}, settings)
    secs = time.time() - t0
    if not res:
        return (f"No usable reply from {model_name(settings)} after {secs:.0f}s - check the model is downloaded "
                "and Ollama is running")
    return (f"{model_name(settings)} answered \u201c{res['answer']}\u201d ({res['confidence']}% sure, from "
            f"{res['source']}) in {secs:.1f}s" + (" - the first call includes loading the model" if secs > 8 else ""))
