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
            return self._json({"models": [{"name": "gemma4:12b", "size": 100, "size_vram": 100}]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/chat":
            FakeOllama.chats.append(body)
            content = body["messages"][1]["content"]
            user = json.loads(content) if content.startswith("{") else {}
            q, opts = (user.get("question") or "").lower(), user.get("options") or []
            if "proposed_answer" in user:  # the double-check pass
                bad = "dial" in q and user["proposed_answer"].lower() == "gold"
                ans = {"verdict": "incorrect" if bad else "correct", "reason": "checked"}
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
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    cfg = get_config()
    monkeypatch.setattr(cfg, "ollama_url", url)
    monkeypatch.setattr(cfg, "llm_base_url", url + "/v1")
    yield url
    srv.shutdown()


AI = dict(DEFAULT_SETTINGS, llm_model="gemma4:12b", ai_answers=True, ai_min_confidence=75, entry_mode="auto",
          respect_automation_clauses=False)


def test_status_and_answer_use_native_api(fake_ollama):
    st = llm.ollama_status()
    assert st["ok"] and st["models"][0]["name"] == "gemma4:12b" and st["models"][0]["gpu_pct"] == 100
    res = llm.answer_question("What colour is the watch case?", None, "a gold case", {"title": "Win"}, AI)
    assert res["answer"] == "Gold" and res["confidence"] == 95
    sent = FakeOllama.chats[-1]
    assert sent["model"] == "gemma4:12b" and sent["think"] is False and sent["format"]["type"] == "object"
    assert "test.person@example.com" not in json.dumps(FakeOllama.chats)  # no profile data in prompts


def test_model_comes_from_settings_and_ai_off_without_model(fake_ollama):
    assert llm.enabled(AI)
    assert not llm.enabled(dict(AI, llm_model=""))  # no env default in tests


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
