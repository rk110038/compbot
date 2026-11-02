"""Web look-ups for competition questions the page doesn't answer (optional).

Two back ends, set in the environment:
  SEARXNG_URL     your own SearXNG (docker-compose.searxng.yml runs one at http://searxng:8080). Free, no account:
                  it asks Google, Brave, DuckDuckGo and others on compbot's behalf.
  OLLAMA_API_KEY  Ollama's hosted web search (needs an ollama.com account and key: https://ollama.com/settings/keys).
If both are set, SearXNG is tried first and Ollama's search is the fallback.

Only the search words leave your network. The AI writes them from the competition question and page, and it never
sees your personal details. Result pages are not opened: the AI reads the search snippets only. Results are cached
for a fortnight, so retries and repeated questions don't search again.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import httpx

from .config import get_config

log = logging.getLogger("compbot.websearch")
_last = threading.local()  # why the last search() on this thread found nothing
STATS = {"searches": 0, "seconds": 0.0}  # searches made by this process (cache hits don't count)

OLLAMA_SEARCH_URL = "https://ollama.com/api/web_search"
TIMEOUT = 20.0
CACHE_DAYS = 14
CACHE_MAX = 500
LABELS = {"searxng": "SearXNG", "ollama": "Ollama web search"}


def providers() -> list[str]:
    cfg = get_config()
    return [p for p, on in (("searxng", cfg.searxng_url), ("ollama", cfg.ollama_api_key)) if on]


def describe() -> str | None:
    """'SearXNG at http://searxng:8080, then Ollama web search', or None when no search is set up."""
    cfg = get_config()
    names = [f"SearXNG at {cfg.searxng_url}" if p == "searxng" else LABELS[p] for p in providers()]
    return ", then ".join(names) or None


def enabled(settings: dict | None = None) -> bool:
    return bool(providers()) and bool((settings or {}).get("ai_web_search", True))


def last_error() -> str | None:
    return getattr(_last, "error", None)


# --------------------------------------------------------------------------- cache

def _cache_path() -> Path:
    return get_config().data_dir / "web_search_cache.json"


def _cache_load() -> dict:
    try:
        d = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _cache_get(key: str) -> list[dict] | None:
    hit = _cache_load().get(key)
    if isinstance(hit, dict) and hit.get("t", 0) > time.time() - CACHE_DAYS * 86400:
        return hit.get("results") or None
    return None


def _cache_put(key: str, results: list[dict]) -> None:
    now = time.time()
    d = {k: v for k, v in _cache_load().items()
         if isinstance(v, dict) and v.get("t", 0) > now - CACHE_DAYS * 86400}
    d[key] = {"t": now, "results": results}
    if len(d) > CACHE_MAX:
        d = dict(sorted(d.items(), key=lambda kv: kv[1]["t"])[-CACHE_MAX:])
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


# --------------------------------------------------------------------------- back ends

def _searxng(query: str, n: int) -> list[dict]:
    r = httpx.get(f"{get_config().searxng_url}/search", timeout=TIMEOUT, headers={"Accept": "application/json"},
                  params={"q": query, "format": "json", "language": "en-GB", "safesearch": 0})
    if r.status_code == 403:
        raise RuntimeError("SearXNG refused to answer in JSON (HTTP 403): add json to search.formats in its "
                           "settings.yml, as docker-compose.searxng.yml does")
    if r.status_code >= 400:
        raise RuntimeError(f"SearXNG said HTTP {r.status_code}: {r.text.strip()[:200]}")
    d = r.json()
    out = []
    for a in d.get("answers") or []:  # instant answers: plain strings, or {"answer": ..., "url": ...}
        text = a.get("answer") if isinstance(a, dict) else a
        if text:
            out.append({"title": "Instant answer", "url": a.get("url") if isinstance(a, dict) else "", "content": text})
    for box in d.get("infoboxes") or []:
        if box.get("content"):
            out.append({"title": box.get("infobox") or "Infobox", "url": box.get("id") or "", "content": box["content"]})
    out += [{"title": x.get("title"), "url": x.get("url"), "content": x.get("content")} for x in d.get("results") or []]
    if not out and d.get("unresponsive_engines"):
        raise RuntimeError("SearXNG found nothing; engines not answering: "
                           + ", ".join(str(e[0]) if isinstance(e, list) and e else str(e)
                                       for e in d["unresponsive_engines"][:5]))
    return out


def _ollama(query: str, n: int) -> list[dict]:
    r = httpx.post(OLLAMA_SEARCH_URL, timeout=TIMEOUT, json={"query": query, "max_results": min(n, 10)},
                   headers={"Authorization": f"Bearer {get_config().ollama_api_key}"})
    if r.status_code in (401, 403):
        raise RuntimeError(f"Ollama web search said HTTP {r.status_code}: check OLLAMA_API_KEY")
    if r.status_code == 429:
        raise RuntimeError("Ollama web search said HTTP 429: rate limit or usage credits used up")
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama web search said HTTP {r.status_code}: {r.text.strip()[:200]}")
    return list(r.json().get("results") or [])


def _clean(r: dict) -> dict | None:
    url = str(r.get("url") or "").strip()
    if url and not re.match(r"https?://", url, re.I):
        url = ""  # shown as a link in the GUI, so only ever http(s)
    content = re.sub(r"\s+", " ", str(r.get("content") or "")).strip()
    title = re.sub(r"\s+", " ", str(r.get("title") or "")).strip()
    if not (content or title):
        return None
    return {"title": title[:150], "url": url[:300], "content": content[:600]}


def _search_with(provider: str, query: str, n: int) -> list[dict]:
    t0 = time.monotonic()
    try:
        raw = (_searxng if provider == "searxng" else _ollama)(query, n)
    finally:
        STATS["searches"] += 1
        STATS["seconds"] += time.monotonic() - t0
    return [c for c in (_clean(r) for r in raw if isinstance(r, dict)) if c][:n]


def search(query: str, n: int = 6, use_cache: bool = True) -> list[dict]:
    """Up to n results [{title, url, content}] for the query, from the first back end that finds any."""
    _last.error = None
    q = re.sub(r"\s+", " ", query or "").strip()[:200]
    if not q:
        return []
    key = q.lower()
    if use_cache:
        hit = _cache_get(key)
        if hit:
            return hit[:n]
    errors = []
    for p in providers():
        try:
            results = _search_with(p, q, n)
        except httpx.TimeoutException:
            errors.append(f"{LABELS[p]}: no reply within {TIMEOUT:.0f}s")
            continue
        except httpx.HTTPError as exc:
            errors.append(f"{LABELS[p]}: can't reach it ({exc})")
            continue
        except (RuntimeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if results:
            _cache_put(key, results)
            return results
        errors.append(f"{LABELS[p]}: no results")
    _last.error = "; ".join(errors)[:400] or "no web search set up"
    log.warning("web search for %r found nothing: %s", q, _last.error)
    return []


def self_test() -> tuple[bool, str]:
    """Settings page check: one uncached search with each back end. (all worked, message)."""
    if not providers():
        return False, ("No web search set up: add the SearXNG service (docker-compose.searxng.yml, or the block in "
                       "truenas-app.yaml), or set OLLAMA_API_KEY. See the README.")
    lines, ok_all = [], True
    for p in providers():
        t0 = time.monotonic()
        try:
            res = _search_with(p, "capital city of Scotland", 3)
            err = None if res else "no results"
        except httpx.TimeoutException:
            res, err = [], f"no reply within {TIMEOUT:.0f}s"
        except httpx.HTTPError as exc:
            res, err = [], f"can't reach it ({exc})"
        except (RuntimeError, ValueError) as exc:
            res, err = [], str(exc)
        secs = time.monotonic() - t0
        if err:
            ok_all = False
            lines.append(f"{LABELS[p]} failed after {secs:.1f}s: {err}")
        else:
            lines.append(f"{LABELS[p]} found {len(res)} results in {secs:.1f}s, e.g. \u201c{res[0]['title'][:80]}\u201d")
    return ok_all, ". ".join(lines)
