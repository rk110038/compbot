"""Live view endpoints (watch-only MJPEG stream of the browser)."""
import base64
import json
import time

from fastapi.testclient import TestClient

H = {"Authorization": "Basic " + base64.b64encode(b"admin:test").decode()}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 2000 + b"\xff\xd9"


def test_live_page_status_frame_and_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_DIR", str(tmp_path))
    from app.main import app
    with TestClient(app) as c:
        assert "Waiting for the next entry" in c.get("/live", headers=H).text
        s = c.get("/live/status", headers=H).json()
        assert s["running"] is False and s["frame_age"] is None
        assert c.get("/live/frame.jpg", headers=H).status_code == 404

        (tmp_path / "frame.jpg").write_bytes(JPEG)
        (tmp_path / "status.json").write_text(json.dumps({"phase": "Filling 3 field(s)", "title": "Win a watch",
                                                          "url": "https://example.com/win", "updated": time.time()}))
        s = c.get("/live/status", headers=H).json()
        assert s["phase"] == "Filling 3 field(s)" and s["title"] == "Win a watch" and s["frame_age"] < 5
        assert c.get("/live/frame.jpg", headers=H).content == JPEG
        with c.stream("GET", "/live/stream?max_frames=1", headers=H) as r:
            assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
            body = r.read()
        assert body.startswith(b"--frame\r\nContent-Type: image/jpeg") and JPEG in body


def test_live_requires_login(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_DIR", str(tmp_path))
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/live").status_code == 401
        assert c.get("/live/stream?max_frames=1").status_code == 401


def test_manual_page_breakdown_and_retry_route(tmp_path, monkeypatch):
    from app.db import Competition, EntryAttempt, init_db, session_scope
    from app.main import app
    init_db()
    with session_scope() as s:
        c = Competition(source="t", source_id="live-man-1", thread_url="x", title="Win a lamp", method="web_form",
                        status="manual", entry_urls=["https://e.example/win-lamp"], status_reason="Needs you: CAPTCHA")
        s.add(c)
        s.flush()
        s.add(EntryAttempt(competition_id=c.id, url="https://e.example/win-lamp", mode="auto", result="captcha",
                           message="CAPTCHA on the form"))
        cid = c.id
    with TestClient(app) as cl:
        page = cl.get("/manual", headers=H).text
        assert "Retry automatically" in page and "CAPTCHA" in page and 'value="captcha"' in page
        assert "Win a lamp" in cl.get("/manual?reason=captcha", headers=H).text
        r = cl.post("/manual/retry", data={"cat": ["captcha"]}, headers={**H, "Origin": "http://testserver"},
                    follow_redirects=False)
        assert r.status_code == 303 and "Re-queued" in r.headers["location"].replace("+", " ")
    with session_scope() as s:
        assert s.get(Competition, cid).status == "queued"


def test_live_status_says_what_happens_between_entries(tmp_path, monkeypatch):
    from app import jobs
    monkeypatch.setenv("LIVE_DIR", str(tmp_path))
    from app.main import app
    lock = jobs._locks["enter"]
    assert lock.acquire(blocking=False)  # as if an entry run were going
    try:
        with TestClient(app) as c:
            assert "s apart (see Settings)" in c.get("/live", headers=H).text
            jobs._step("enter", "Waiting before the next competition", 40)
            b = c.get("/live/status", headers=H).json()["between"]
            assert b["what"] == "Waiting before the next competition" and 38 <= b["left"] <= 40

            jobs._step("enter", "Starting the browser")
            since = jobs.between("enter")["since"]
            (tmp_path / "status.json").write_text(json.dumps({"phase": "Finished", "updated": since - 5}))
            b = c.get("/live/status", headers=H).json()["between"]  # the last comp's "Finished": not this one's
            assert b["what"] == "Starting the browser" and b["left"] is None
            (tmp_path / "status.json").write_text(json.dumps({"phase": "Finished", "updated": time.time()}))
            assert c.get("/live/status", headers=H).json()["between"]["what"] == "Closing the browser"
    finally:
        jobs._between.clear()
        lock.release()


def test_stop_buttons(tmp_path, monkeypatch):
    from urllib.parse import unquote_plus

    from app import jobs
    monkeypatch.setenv("LIVE_DIR", str(tmp_path))
    from app.main import app
    P = {**H, "Origin": "http://testserver"}
    with TestClient(app) as c:
        r = c.post("/stop/inbox", headers=P, follow_redirects=False)
        assert r.status_code == 303 and "isn't running" in unquote_plus(r.headers["location"])
        assert c.post("/stop/nonsense", headers=P).status_code == 404
        lock = jobs._locks["inbox"]
        assert lock.acquire(blocking=False)  # as if a Gmail check were going
        try:
            page = c.get("/", headers=H).text
            assert 'action="/stop/inbox"' in page and 'action="/run/inbox"' not in page
            r = c.post("/stop/inbox", headers=P, follow_redirects=False)
            assert "stopping after the email" in unquote_plus(r.headers["location"])
            assert jobs.stop_requested("inbox")
            page = c.get("/", headers=H).text
            assert "Stopping…" in page and ">Stopping<" in page  # the button and the header pill
            assert c.get("/live/status", headers=H).json()["stopping"] is False  # only the entry run's state
        finally:
            jobs._stops["inbox"].clear()
            lock.release()
        assert 'action="/run/inbox"' in c.get("/", headers=H).text
        assert 'id="stop-form"' in c.get("/live", headers=H).text


def test_dashboard_says_why_comps_are_still_queued():
    from app.db import Competition, init_db, session_scope
    from app.main import app
    init_db()
    with session_scope() as s:
        s.add(Competition(source="t", source_id="dash-retry-1", thread_url="x", title="Win a kettle", method="web_form",
                          status="queued", entry_urls=["https://k.example/win"], failed_attempts=1,
                          status_reason="Attempt 1 failed - will retry"))
    with TestClient(app) as c:
        page = c.get("/", headers=H).text
        assert 'href="/competitions?status=queued"' in page and "to retry after a failed try" in page
        assert "Attempt 1 failed - will retry" in c.get("/competitions?status=queued", headers=H).text
