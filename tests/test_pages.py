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


def test_pages_show_where_a_competition_was_found():
    from app.db import Competition, Listing, init_db, session_scope
    from app.main import app
    init_db()
    with session_scope() as s:
        c = Competition(source="mse", source_id="pages-found-1", thread_url="https://mse.example/t/1", title="Win a lamp",
                        method="web_form", status="manual", status_reason="No entry link: test",
                        other_links=["https://www.competitions-time.co.uk/redir/77"])
        s.add(c)
        s.flush()
        s.add_all([Listing(competition_id=c.id, source="mse", source_id="pages-found-1", url="https://mse.example/t/1"),
                   Listing(competition_id=c.id, source="ct", source_id="ct:pages-77",
                           url="https://www.competitions-time.co.uk/x#lamp")])
        cid = c.id
    with TestClient(app) as client:
        page = client.get(f"/competitions/{cid}", headers=H).text
        assert "MSE thread ↗" in page and "Competitions-Time ↗" in page and "Found on" in page
        assert 'href="https://www.competitions-time.co.uk/redir/77"' in page
        listed = client.get("/competitions?source=ct", headers=H).text
        assert f'href="/competitions/{cid}"' in listed
        assert f'href="/competitions/{cid}"' not in client.get("/competitions?source=cdb", headers=H).text
        settings = client.get("/settings", headers=H).text
        assert 'name="s_source_ct"' in settings and "CDB_USERNAME" in settings
        assert "No entry link" in client.get("/manual?reason=nolink", headers=H).text