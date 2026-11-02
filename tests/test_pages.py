"""Page shell: browsers (notably Safari on iPhone) must never keep using a previous release's stylesheet."""
import base64
import hashlib
import re
from pathlib import Path

from fastapi.testclient import TestClient

H = {"Authorization": "Basic " + base64.b64encode(b"admin:test").decode()}
CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"


def test_stylesheet_url_follows_its_content_and_is_revalidated():
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/", headers=H).text
        href = re.search(r'href="(/static/style\.css\?v=([0-9a-f]+))"', page)
        assert href and href.group(2) == hashlib.sha256(CSS.read_bytes()).hexdigest()[:10]
        r = c.get(href.group(1), headers=H)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
        again = c.get(href.group(1), headers={**H, "If-None-Match": r.headers["etag"]})
        assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"


def test_burger_menu_script_stands_alone():
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/", headers=H).text
    blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
    burger = [b for b in blocks if "nav-toggle" in b]
    assert len(burger) == 1 and "theme-btn" not in burger[0] and "touchstart" in burger[0]
