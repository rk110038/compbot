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
