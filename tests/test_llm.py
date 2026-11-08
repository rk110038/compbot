"""AI helper tests against a fake Ollama server (no model download needed)."""
import base64
import http.server
import json
import threading

import pytest

from app import llm
from app.analyse import analyse
from app.config import DEFAULT_SETTINGS, get_config
from app.entry.browser import Browser
from app.entry.forms import Ctx


class FakeOllama(http.server.BaseHTTPRequestHandler):
    chats: list = []
    chat_error: tuple[int, dict] | None = None  # make /api/chat fail like a real Ollama would
    ps_vram = 100  # bytes of the loaded model (size 100) that are on the GPU
    models = [{"name": "gemma4:12b", "size": 7_700_000_000,
               "details": {"parameter_size": "12B", "quantization_level": "Q4_K_M"}}]

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/version":
            return self._json({"version": "0.99.0"})
        if self.path == "/api/tags":
            return self._json({"models": self.models})
        if self.path == "/api/ps":
            return self._json({"models": [{"name": "gemma4:12b", "size": 100, "size_vram": FakeOllama.ps_vram}]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/chat":
            FakeOllama.chats.append(body)
            if FakeOllama.chat_error:
                return self._json(FakeOllama.chat_error[1], FakeOllama.chat_error[0])
            content = body["messages"][1]["content"]
            user = json.loads(content) if content.startswith("{") else {}
            q, opts = (user.get("question") or "").lower(), user.get("options") or []
            if "proposed_answer" in user:  # the double-check pass
                bad = "dial" in q and user["proposed_answer"].lower() == "gold"
                ans = {"verdict": "incorrect" if bad else "correct", "reason": "checked"}
            elif "founded" in q:  # not on the page: needs a web look-up
                web = " ".join(r.get("snippet") or "" for r in user.get("web_results") or [])
                ans = ({"answer": "1989", "confidence": 92, "source": "web", "reason": "two results say 1989"}
                       if "1989" in web else
                       {"answer": "1977", "confidence": 60, "source": "knowledge", "reason": "I think",
                        "search_query": "Stormele watches founded year"})
            elif "colour" in q:
                ans = {"answer": "Gold", "confidence": 95, "source": "page", "reason": "the page says gold case"}
            elif "unsure" in q:
                ans = {"answer": "Maybe", "confidence": 30, "source": "knowledge", "reason": "a guess"}
            elif opts:
                pick = next((o for o in opts if o in ("Alpine", "Abu Dhabi", "Edinburgh")), opts[0])
                ans = {"answer": pick, "confidence": 90, "source": "knowledge", "reason": "known"}
            elif "prompt" in user:
                ans = {"text": "Sharing it with my family would make our Christmas extra special this year."}
            else:
                ans = {"answer": None, "confidence": 0, "source": "none", "reason": "no idea"}
            return self._json({"message": {"role": "assistant", "content": json.dumps(ans)}, "done": True})
        if self.path == "/api/pull":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for ev in [{"status": "pulling manifest"}, {"status": "downloading", "total": 100, "completed": 50},
                       {"status": "success"}]:
                self.wfile.write((json.dumps(ev) + "\n").encode())
            return
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        return self._json({})


@pytest.fixture
def fake_ollama(monkeypatch):
    FakeOllama.chats = []
    FakeOllama.chat_error = None
    FakeOllama.ps_vram = 100
    llm.clear_ai_pause()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    cfg = get_config()
    monkeypatch.setattr(cfg, "ollama_url", url)
    monkeypatch.setattr(cfg, "llm_base_url", url + "/v1")
    yield url
    srv.shutdown()
    llm.clear_ai_pause()


AI = dict(DEFAULT_SETTINGS, llm_model="gemma4:12b", ai_answers=True, ai_min_confidence=75, entry_mode="auto",
          respect_automation_clauses=False)


def test_status_and_answer_use_native_api(fake_ollama):
    st = llm.ollama_status()
    assert st["ok"] and st["models"][0]["name"] == "gemma4:12b" and st["models"][0]["gpu_pct"] == 100
    res = llm.answer_question("What colour is the watch case?", None, "a gold case", {"title": "Win"}, AI)
    assert res["answer"] == "Gold" and res["confidence"] == 95
    sent = FakeOllama.chats[-1]
    assert sent["model"] == "gemma4:12b" and sent["think"] is False and sent["format"]["type"] == "object"
    assert sent["options"]["draft_num_predict"] == 0  # no speculative draft model (Gemma 4's fails on some GPUs)
    assert sent["options"]["num_predict"] == 1024  # a reply that runs away is cut off, not left to time out
    assert "test.person@example.com" not in json.dumps(FakeOllama.chats)  # no profile data in prompts


def test_model_comes_from_settings_and_ai_off_without_model(fake_ollama):
    assert llm.enabled(AI)
    assert not llm.enabled(dict(AI, llm_model=""))  # no env default in tests


def test_self_test_reports_success(fake_ollama):
    ok, msg = llm.self_test(AI)
    assert ok and "answered \u201cEdinburgh\u201d" in msg and msg.endswith("running on the GPU")


def test_self_test_says_where_the_model_runs(fake_ollama):
    # Seen on the NAS: Ollama silently fell back to the CPU (driver too old for CUDA on a GTX 1070).
    FakeOllama.ps_vram = 0
    ok, msg = llm.self_test(AI)
    assert ok and "running on the CPU only" in msg and "inference compute" in msg
    FakeOllama.ps_vram = 60
    ok, msg = llm.self_test(AI)
    assert ok and "60% on the GPU, the rest on the CPU" in msg


def test_self_test_explains_out_of_memory_kill(fake_ollama):
    # Seen on the NAS: the kernel's OOM killer stopped llama-server while the model loaded into RAM.
    FakeOllama.chat_error = (500, {"error": "llama-server process has terminated: signal: killed"})
    ok, msg = llm.self_test(AI)
    assert not ok and "signal: killed" in msg
    assert "out-of-memory killer" in msg and "ran out of RAM" in msg and "ZFS cache" in msg
    assert "try a smaller one" not in msg  # the generic VRAM hint would point the wrong way


def test_self_test_shows_ollamas_error(fake_ollama):
    FakeOllama.chat_error = (500, {"error": "model requires more system memory (9.8 GiB) than is available (6.1 GiB)"})
    ok, msg = llm.self_test(AI)
    assert not ok
    assert "HTTP 500: model requires more system memory (9.8 GiB)" in msg and "try a smaller one" in msg
    FakeOllama.chat_error = (404, {"error": "model 'gemma4:12b' not found"})
    ok, msg = llm.self_test(AI)
    assert not ok and "not found" in msg and "Download the model" in msg


NOISE = ("llama_init_from_model: failed to initialize the context: Gemma4Assistant requires ctx_other to be set "
         "(this warning is normal during memory fitting)")


def test_self_test_hides_gemma4_draft_warning_noise(fake_ollama, monkeypatch):
    # Seen on the NAS: the repeated warning filled the message and hid the real cause.
    FakeOllama.chat_error = (500, {"error": f"llama-server process has terminated: {NOISE}: {NOISE}: "
                                            "cudaMalloc failed: out of memory"})
    ok, msg = llm.self_test(AI)
    assert not ok and "ctx_other" not in msg
    assert "HTTP 500: llama-server process has terminated: cudaMalloc failed: out of memory" in msg
    assert "try a smaller one" in msg
    # Nothing but the warning: say so, and point at the Ollama version.
    FakeOllama.chat_error = (500, {"error": f"llama-server process has terminated: {NOISE}: {NOISE}"})
    monkeypatch.setattr(llm, "_ollama_version", lambda: "0.40.2")
    ok, msg = llm.self_test(AI)
    assert not ok and "ctx_other" not in msg and llm.DRAFT_WARNING_NOTE in msg
    assert "Your Ollama is 0.40.2" in msg and "pull_policy: always" in msg
    monkeypatch.setattr(llm, "_ollama_version", lambda: "0.40.13")
    ok, msg = llm.self_test(AI)
    assert "Ollama 0.40.13 couldn't load the model on this GPU" in msg and "pull_policy" not in msg


def test_self_test_when_ollama_is_down(monkeypatch):
    monkeypatch.setattr(get_config(), "ollama_url", "http://127.0.0.1:9")
    monkeypatch.setattr(get_config(), "llm_base_url", "http://127.0.0.1:9/v1")
    ok, msg = llm.self_test(AI)
    assert not ok and "can't reach Ollama at http://127.0.0.1:9" in msg
    llm.clear_ai_pause()


def test_ai_pauses_after_model_failure_instead_of_retrying_every_call(fake_ollama):
    FakeOllama.chat_error = (500, {"error": "llama-server process has terminated: cudaMalloc failed"})
    assert llm.answer_question("Q1?", ["A", "B"], "", {}, AI) is None
    assert len(FakeOllama.chats) == 1
    FakeOllama.chat_error = None
    # Later calls, from any entry process, don't wait on Ollama again...
    assert llm.answer_question("Q2?", ["A", "B"], "", {}, AI) is None
    assert len(FakeOllama.chats) == 1 and llm.last_error().startswith("AI paused until ")
    assert "HTTP 500" in llm.ai_paused(AI)["why"]
    # ...a different model isn't affected, and the model test clears the pause and really tries.
    assert llm.ai_paused(dict(AI, llm_model="qwen3.5:9b")) is None
    ok, _ = llm.self_test(AI)
    assert ok and llm.ai_paused(AI) is None and len(FakeOllama.chats) == 2
    # A reply that just isn't usable (bad JSON, 4xx) is not a reason to pause.
    FakeOllama.chat_error = (404, {"error": "model 'gemma4:12b' not found"})
    llm.answer_question("Q3?", None, "", {}, AI)
    assert llm.ai_paused(AI) is None


def test_pull_progress(fake_ollama):
    llm.pull_model("qwen3.5:9b")
    p = llm.pull_progress()["qwen3.5:9b"]
    assert p["done"] and p["status"] == "success" and p["pct"] == 100 and not p["error"]


def test_ai_answers_question_in_browser(fake_ollama, fixture_server, profile):
    with Browser() as b:
        res = b.enter(f"{fixture_server}/ai_question.html", Ctx(profile=profile), AI, "ai1", {"title": "Win a watch"})
    assert res.result == "entered", (res.message, res.details)
    used = [x for x in res.details["ai"] if x["used"]]
    assert used and used[0]["answer"] == "Gold" and used[0]["confidence"] == 95
    t = res.details["timing"]
    assert t["ai_calls"] >= 1 and t["seconds"] >= t["ai_seconds"] >= 0
    # the page text was given to the model
    assert any("gold case" in json.loads(c["messages"][1]["content"]).get("page_text", "") for c in FakeOllama.chats)


def test_low_confidence_ai_answer_not_used(fake_ollama, fixture_server, profile):
    html = (open("tests/fixtures/ai_question.html", encoding="utf-8").read()
            .replace("What colour is the watch case?", "An unsure question?"))
    open("tests/fixtures/ai_unsure.html", "w", encoding="utf-8").write(html)
    try:
        with Browser() as b:
            res = b.enter(f"{fixture_server}/ai_unsure.html", Ctx(profile=profile), AI, "ai2", {})
    finally:
        import os
        os.remove("tests/fixtures/ai_unsure.html")
    assert res.result == "needs_input" and "wasn't sure enough (30% < 75%)" in res.message


def test_ai_answers_quiz_options(fake_ollama, fixture_server, profile):
    with Browser() as b:
        res = b.enter(f"{fixture_server}/quiz.html", Ctx(profile=profile, question="quiz"), AI, "ai3", {})
    assert res.result == "entered", (res.message, res.details)
    assert [x["answer"] for x in res.details["ai"] if x["used"]] == ["Alpine", "Abu Dhabi"]


def test_ai_off_leaves_question_for_you(fake_ollama, fixture_server, profile):
    with Browser() as b:
        res = b.enter(f"{fixture_server}/ai_question.html", Ctx(profile=profile), dict(AI, ai_answers=False,
                                                                                       llm_for_forms=False), "ai4", {})
    assert res.result == "needs_input" and not FakeOllama.chats


def test_discovery_queues_unanswered_question_when_ai_on():
    body = "<p>https://www.example-mag.co.uk/win-a-watch</p><p>Q: What colour is the watch case?</p>"
    a = analyse("E: 30/11 Win a watch", body, dict(DEFAULT_SETTINGS, _ai_answers=True))
    assert a.status == "queued" and "AI" in a.reason
    a = analyse("E: 30/11 Win a watch", body, DEFAULT_SETTINGS)
    assert a.status == "manual"


def test_settings_page_lists_models_and_downloads(fake_ollama):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        h = {"Authorization": "Basic " + base64.b64encode(b"admin:test").decode()}
        page = c.get("/settings", headers=h).text
        assert 'value="gemma4:12b"' in page and "Ollama 0.99.0 is running" in page and "100% on GPU" in page
        assert "AI paused" not in page
        pause = {"until": 0, "until_text": "21:10", "model": "gemma4:12b", "why": "Ollama said HTTP 500: boom"}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(llm, "ai_paused", lambda settings=None: pause)
            page = c.get("/settings", headers=h).text
        assert "AI paused until 21:10 after an error" in page and "HTTP 500: boom" in page
        r = c.post("/ai/pull", data={"model": "qwen3.5:9b"}, headers={**h, "Origin": "http://testserver"},
                   follow_redirects=False)
        assert r.status_code == 303
        r = c.post("/ai/pull", data={"custom": "bad name; rm -rf"}, headers={**h, "Origin": "http://testserver"},
                   follow_redirects=False)
        assert "doesn" in r.headers["location"] or "look" in r.headers["location"]


def test_double_check_rejects_wrong_answer(fake_ollama, fixture_server, profile):
    html = (open("tests/fixtures/ai_question.html", encoding="utf-8").read()
            .replace("What colour is the watch case?", "What colour is the watch dial?"))
    open("tests/fixtures/ai_dial.html", "w", encoding="utf-8").write(html)
    try:
        with Browser() as b:
            res = b.enter(f"{fixture_server}/ai_dial.html", Ctx(profile=profile), AI, "ai5", {})
    finally:
        import os
        os.remove("tests/fixtures/ai_dial.html")
    assert res.result == "needs_input" and "failed its second check" in res.message
    rec = res.details["ai"][0]
    assert rec["check"] == "incorrect" and not rec["used"]


def test_missing_phone_is_not_sent_to_the_ai_and_keeps_its_reason(fake_ollama, fixture_server, profile):
    # Horse&Rider: the AI spent 61 s on a required phone box it can't fill, and its empty reply hid the reason
    # ("Phone number ()"), so the manual queue didn't file it under "Needs a phone number".
    with Browser() as b:
        res = b.enter(f"{fixture_server}/phone_required.html", Ctx(profile=dict(profile, phone="")), AI, "ph1", {})
    assert res.result == "needs_input" and "Phone number (profile has no phone)" in res.message, res.message
    assert not any("PROFILE_KEYS" in c["messages"][1]["content"] for c in FakeOllama.chats)


# --------------------------------------------------------------------------- web look-ups

class FakeSearch(http.server.BaseHTTPRequestHandler):
    """A SearXNG (/search?format=json) and Ollama web search (/api/web_search) stand-in."""
    hits: list = []
    searx_status = 200
    ollama_status = 200
    auth: str | None = None
    RESULTS = [{"title": "Stormele - Wikipedia", "url": "https://example.org/stormele",
                "content": "Stormele is a British watchmaker founded in 1989 in Sheffield."},
               {"title": "About us | Stormele", "url": "https://stormele.example/about",
                "content": "Since 1989 we have made watches by hand."},
               {"title": "Bad link", "url": "javascript:alert(1)", "content": "should lose its link"}]

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        if u.path == "/search":
            q = parse_qs(u.query)
            FakeSearch.hits.append(("searxng", q["q"][0], q.get("format", [""])[0]))
            if FakeSearch.searx_status != 200:
                return self._json({"error": "forbidden"}, FakeSearch.searx_status)
            return self._json({"query": q["q"][0], "results": self.RESULTS, "answers": [], "infoboxes": [],
                               "unresponsive_engines": []})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/web_search":
            FakeSearch.hits.append(("ollama", body["query"], body.get("max_results")))
            FakeSearch.auth = self.headers.get("Authorization")
            if FakeSearch.ollama_status != 200:
                return self._json({"error": "unauthorized"}, FakeSearch.ollama_status)
            return self._json({"results": self.RESULTS[:2]})
        return self._json({"error": "not found"}, 404)


@pytest.fixture
def fake_search(monkeypatch):
    from app import websearch
    FakeSearch.hits, FakeSearch.searx_status, FakeSearch.ollama_status, FakeSearch.auth = [], 200, 200, None
    websearch._cache_path().unlink(missing_ok=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSearch)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(get_config(), "searxng_url", url)
    monkeypatch.setattr(get_config(), "ollama_api_key", None)
    monkeypatch.setattr(websearch, "OLLAMA_SEARCH_URL", url + "/api/web_search")
    yield url
    srv.shutdown()
    websearch._cache_path().unlink(missing_ok=True)


def test_ai_looks_up_what_the_page_doesnt_say(fake_ollama, fake_search, fixture_server, profile):
    with Browser() as b:
        res = b.enter(f"{fixture_server}/ai_lookup.html", Ctx(profile=profile), AI, "web1", {"title": "Win a watch"})
    assert res.result == "entered", (res.message, res.details)
    rec = res.details["ai"][0]
    assert rec["used"] and rec["answer"] == "1989" and rec["source"] == "web" and rec["check"] == "correct"
    assert rec["searched"] == "Stormele watches founded year"  # the AI's query, naming the brand from the page
    assert rec["web_sources"] == ["https://example.org/stormele", "https://stormele.example/about"]  # no javascript:
    assert FakeSearch.hits == [("searxng", "Stormele watches founded year", "json")]
    assert res.details["timing"]["web_searches"] == 1
    check = next(json.loads(c["messages"][1]["content"]) for c in FakeOllama.chats
                 if "proposed_answer" in c["messages"][1]["content"])
    assert "1989" in json.dumps(check["web_results"])  # the double-check sees the same evidence
    assert "test.person@example.com" not in json.dumps(FakeSearch.hits)


def test_answers_read_off_the_page_are_not_looked_up(fake_ollama, fake_search, fixture_server, profile):
    with Browser() as b:
        res = b.enter(f"{fixture_server}/ai_question.html", Ctx(profile=profile), AI, "web2", {})
    assert res.result == "entered" and res.details["ai"][0]["source"] == "page"
    assert not FakeSearch.hits and "searched" not in res.details["ai"][0]


def test_web_search_switched_off_in_settings(fake_ollama, fake_search, fixture_server, profile):
    with Browser() as b:
        res = b.enter(f"{fixture_server}/ai_lookup.html", Ctx(profile=profile), dict(AI, ai_web_search=False), "web3", {})
    assert res.result == "needs_input" and "wasn't sure enough (60% < 75%)" in res.message and not FakeSearch.hits
    assert "web search" not in res.message


def test_search_falls_back_to_ollama_and_caches(fake_search, monkeypatch):
    from app import websearch
    monkeypatch.setattr(get_config(), "ollama_api_key", "k123")
    FakeSearch.searx_status = 403  # SearXNG without JSON switched on
    res = websearch.search("Stormele founded")
    assert [r["url"] for r in res] == ["https://example.org/stormele", "https://stormele.example/about"]
    assert FakeSearch.auth == "Bearer k123" and [h[0] for h in FakeSearch.hits] == ["searxng", "ollama"]
    assert websearch.search("  stormele   FOUNDED ") == res and len(FakeSearch.hits) == 2  # from the cache
    FakeSearch.ollama_status = 401
    assert websearch.search("something else") == []
    err = websearch.last_error()
    assert "HTTP 403" in err and "search.formats" in err and "OLLAMA_API_KEY" in err


def test_lookup_finding_nothing_keeps_the_first_answer(fake_ollama, fake_search):
    FakeSearch.searx_status = 500
    res = llm.answer_with_lookup("What year was Stormele founded?", None, "a watch", {}, AI, 75)
    assert res["answer"] == "1977" and res["confidence"] == 60
    assert res["search"]["query"] == "Stormele watches founded year" and "HTTP 500" in res["search"]["error"]


def test_settings_web_search_test_button(fake_ollama, fake_search, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:test").decode()}
    with TestClient(app) as c:
        page = c.get("/settings", headers=h).text
        assert "Test web search" in page and f"SearXNG at {fake_search}" in page
        r = c.post("/ai/test-search", headers={**h, "Origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 303 and "SearXNG+found+3+results" in r.headers["location"]
        monkeypatch.setattr(get_config(), "searxng_url", None)
        page = c.get("/settings", headers=h).text
        assert "Test web search" not in page and "Not set up" in page


def test_questions_about_your_taste_skip_the_second_check(fake_ollama, fixture_server, profile):
    # "Which The Light venue is your local cinema?": there's no right answer for the second check to find.
    html = (open("tests/fixtures/ai_question.html", encoding="utf-8").read()
            .replace("What colour is the watch case?", "What colour is your favourite watch dial?"))
    open("tests/fixtures/ai_taste.html", "w", encoding="utf-8").write(html)
    try:
        with Browser() as b:
            res = b.enter(f"{fixture_server}/ai_taste.html", Ctx(profile=profile), AI, "ai6", {})
    finally:
        import os
        os.remove("tests/fixtures/ai_taste.html")
    assert res.result == "entered", (res.message, res.details.get("ai"))
    rec = res.details["ai"][0]
    assert rec["used"] and rec["answer"] == "Gold" and "check" not in rec

def test_doubted_ai_answer_is_used_when_the_form_insists(fake_ollama, fixture_server, profile):
    # The second check turns "Gold" down for a dial question; the quiz isn't marked required, so it was left blank and
    # the site said "* Required". Then the AI's own answer beats sending the question blank.
    with Browser() as b:
        res = b.enter(f"{fixture_server}/ai_required_quiz.html", Ctx(profile=profile), AI, "ai7", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    assert any("site said fields were required" in s for s in res.details["steps"])
    assert any(f["source"] == "ai" and "second check doubted" in f["note"] for f in res.details["fields"])