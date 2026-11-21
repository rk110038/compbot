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

def test_inbox_shows_what_each_email_says_and_links_to_it():
    from app.db import Competition, Email, init_db, session_scope
    from app.main import app
    init_db()
    with session_scope() as s:
        c = Competition(source="test", source_id="pages-win-1", thread_url="x", title="Win a Herbelin watch",
                        prize="Herbelin Art Deco watch", method="web_form", status="entered", entry_count=1,
                        entry_urls=["https://www.countryandtownhouse.com/win"], outcome="possible_win")
        s.add(c)
        s.flush()
        e = Email(message_id="<pages-win-1@cth.example>", from_addr="comps@countryandtownhouse.com",
                  from_name="Competitions", from_domain="countryandtownhouse.com",
                  subject="Congratulations - you've won!", category="possible_win", competition_id=c.id,
                  snippet="Dear Test, we are delighted to tell you that you are the winner of our Herbelin watch "
                          "competition. Reply by Friday.", signals=["win:Congratulations"])
        s.add(e)
        s.flush()
        eid = e.id
    with TestClient(app) as client:
        page = client.get("/inbox?category=possible_win", headers=H).text
        assert f'href="/inbox/{eid}"' in page and "<mark>you are the winner</mark>" in page
        assert "site (countryandtownhouse.com)" in page and "rfc822msgid%3Apages-win-1%40cth.example" in page
        one = client.get(f"/inbox/{eid}", headers=H).text
        assert "What it says" in one and "Reply by Friday." in one and f'action="/inbox/{eid}/action"' in one
        assert client.get("/inbox/999999", headers=H).status_code == 404
        wins = client.get("/wins", headers=H).text
        assert f'href="/inbox/{eid}"' in wins and "from Competitions &lt;comps@countryandtownhouse.com&gt;" in wins

def test_inbox_guesses_the_competition_and_finds_others_by_typing():
    from datetime import timedelta

    from app.db import Competition, Email, init_db, session_scope, utcnow
    from app.main import app
    init_db()
    ago = utcnow() - timedelta(days=30)
    with session_scope() as s:
        heinz = Competition(source="test", source_id="guess-heinz", thread_url="x", method="web_form",
                            title="E: 21/11 Win a £250 Honest Burgers voucher, a Halloween party, gift hamper or "
                                  "Heinz to Home vouchers", status="entered", entry_count=1, entered_at=ago,
                            last_entered_at=ago, entry_urls=["https://www.heinz.com/en-GB/competition/halloween"],
                            promoter_domain="heinz.com")
        wine = Competition(source="test", source_id="guess-wine", thread_url="x", method="web_form",
                           title="Win a £250 voucher to spend on fine wine at Squelch", status="entered",
                           entry_count=1, entered_at=ago, last_entered_at=ago)
        s.add_all([heinz, wine])
        s.flush()
        e = Email(message_id="<guess-heinz@kraftheinz.example>", from_addr="consumercare@kraftheinz.com",
                  from_name="Consumer Services UK & I", from_domain="kraftheinz.com", received_at=utcnow(),
                  subject="You've won a £50 Voucher for Heinz to Home", category="possible_win",
                  snippet="Congratulations! We are delighted to confirm that you have won a £50 Heinz to Home voucher!",
                  signals=["win:Congratulations"])
        linked = Email(message_id="<guess-linked@squelch.example>", from_addr="hello@squelch.co.uk",
                       from_domain="squelch.co.uk", subject="Your Squelch wine voucher", category="possible_win",
                       competition_id=wine.id, snippet="Congratulations, you won.", signals=["win:Congratulations"])
        s.add_all([e, linked])
        s.flush()
        promo = Email(message_id="<guess-promo@kraftheinz.example>", from_addr="news@kraftheinz.com",
                      from_domain="kraftheinz.com", received_at=utcnow(), category="promo",
                      subject="Heinz to Home: new voucher offers", snippet="Shop Heinz to Home vouchers now.")
        s.add(promo)
        s.flush()
        hid, eid, lid, pid = heinz.id, e.id, linked.id, promo.id
        heinz.outcome = "possible_win"
    with TestClient(app) as client:
        # A clear favourite is shown on a promo, but only ticked on a possible win or a loss.
        promo_page = client.get("/inbox?category=promo", headers=H).text
        promo_row = promo_page[promo_page.index(f'id="email-{pid}"'):]
        assert "Best guess:" in promo_row and f'value="{hid}">' in promo_row and f'value="{hid}" checked' not in promo_row
        # "Not a win" never resets a competition just because it was the guess sent along with it.
        client.post(f"/inbox/{pid}/action", data={"action": "not_win", "competition_id": str(hid)},
                    headers={**H, "Origin": "http://testserver"}, follow_redirects=False)
        with session_scope() as s:
            assert s.get(Competition, hid).outcome == "possible_win" and s.get(Email, pid).competition_id is None
        page = client.get("/inbox?category=possible_win", headers=H).text
        row = page[page.index(f'id="email-{eid}"'):page.index(f'id="email-{lid}"')]
        assert '<select name="competition_id"' not in page
        assert "Best guess, ticked for you:" in row and f'value="{hid}" checked' in row
        assert "the sender, kraftheinz.com, looks like the competition&#39;s site (heinz.com)" in row
        assert 'class="find"' in row and "/inbox/find-comp?q=" in page
        # An email already linked keeps its link unless you choose another.
        other = page[page.index(f'id="email-{lid}"'):]
        assert "Wrong competition? Change it" in other and "Keep it linked to" in other
        one = client.get(f"/inbox/{eid}", headers=H).text
        assert f'value="{hid}" checked' in one and "Best guess, ticked for you:" in one
        found = client.get("/inbox/find-comp?q=heinz home", headers=H).json()["results"]
        assert [r["id"] for r in found] == [hid] and found[0]["site"] == "heinz.com" and found[0]["entered"]
        assert client.get("/inbox/find-comp?q=zzzzqq", headers=H).json() == {"results": []}
        assert client.get("/inbox/find-comp?q=heinz").status_code == 401
        r = client.post(f"/inbox/{eid}/action", data={"action": "confirm_win", "competition_id": str(hid)},
                        headers={**H, "Origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 303
    with session_scope() as s:
        assert s.get(Email, eid).competition_id == hid and s.get(Competition, hid).outcome == "won"
        assert s.get(Email, eid).category == "won"
    with TestClient(app) as client:
        assert f'id="email-{eid}"' not in client.get("/inbox?category=possible_win", headers=H).text
        won_tab = client.get("/inbox?category=won", headers=H).text
        assert f'id="email-{eid}"' in won_tab and ">won (" in won_tab


def test_a_win_you_confirm_leaves_the_possible_win_tab():
    from app.db import Competition, Email, init_db, session_scope
    from app.main import app
    init_db()
    with session_scope() as s:
        comps = [Competition(source="test", source_id=f"settle-{i}", thread_url="x", title=f"Win prize {i}",
                             method="web_form", status="entered", entry_count=1, outcome=o)
                 for i, o in enumerate(["possible_win", "possible_win", "won"])]
        s.add_all(comps)
        s.flush()
        mails = [Email(message_id=f"<settle-{i}@x>", subject=f"You won {i}", category="possible_win",
                       competition_id=cid, reviewed=rev)
                 for i, (cid, rev) in enumerate([(comps[0].id, False), (comps[1].id, False), (comps[2].id, True),
                                                 (None, True), (None, False)])]
        s.add_all(mails)
        s.flush()
        cids, eids = [c.id for c in comps], [e.id for e in mails]
    # Already confirmed before "won" existed: filed under it when compbot starts.
    init_db()
    with session_scope() as s:
        assert [s.get(Email, i).category for i in eids[2:]] == ["won", "won", "possible_win"]
    # The Wins page's buttons file the emails behind a possible win too.
    with TestClient(app) as client:
        for cid, action in [(cids[0], "won"), (cids[1], "pending")]:
            client.post(f"/competitions/{cid}/action", data={"action": action},
                        headers={**H, "Origin": "http://testserver"}, follow_redirects=False)
    with session_scope() as s:
        assert [s.get(Email, i).category for i in eids[:2]] == ["won", "other"]
        assert all(s.get(Email, i).reviewed for i in eids[:2])


def _comp(i, title, urls=(), closes=None, status="manual"):
    from datetime import date
    from types import SimpleNamespace
    return SimpleNamespace(id=i, title=title, prize=re.sub(r"^E: \S+ (\(MD\) )?", "", title),
                           entry_urls=list(urls), promoter_domain=None, status=status,
                           closes_on=closes or date(2026, 11, 21))


def test_closing_soon_shows_one_row_per_prize_with_its_sites():
    from datetime import date

    from app.main import closing_rows
    hb = "tickets_to_south_east_homebuilding__renovating_show"
    comps = [
        _comp(216, "E: 21/11 (MD) Win tickets to South East Homebuilding & Renovating Show in Farnborough",
              [f"https://comps.marieclaire.co.uk/competition/{hb}_marieclaire/123703.php"]),
        _comp(220, "E: 21/11 (MD) Win tickets to South East Homebuilding & Renovating Show + an e-book x10, Farnborough",
              [f"https://competitions.goodto.com/competition/{hb}_goodtoknow/123700.php",
               f"https://comps.pickmypostcode.com/competition/{hb}_pickmypostcode/123704.php"]),
        _comp(231, "E: 21/11 (MD) Win tickets to South East Homebuilding & Renovating Show",
              [f"https://comps.trustedreviews.com/competition/{hb}_trustedreviews/123707.php"], status="queued"),
        _comp(240, "E: 21/11 (MD) Win 6 Months' Worth of Bio&Me x Bowel Cancer UK Breakfasts",
              ["https://comps.celebsnow.co.uk/competition/x/1.php", "https://comps.whatsontv.co.uk/competition/x/2.php"]),
        _comp(247, "E: 21/11 Win a family ticket to see Beauty and the Beast at Leatherhead Theatre",
              ["https://vantagepointmag.co.uk/a"]),
        _comp(251, "E: 21/11 Win a family ticket to see Beauty and the Beast at Godalming", ["https://vantagepointmag.co.uk/b"]),
        _comp(300, "E: 21/11 Win a £100 Dobbies Garden Centre gift voucher", ["https://a.example/1"]),
        _comp(301, "E: 21/11 Win a £200 Dobbies Garden Centre gift voucher", ["https://b.example/1"]),
        _comp(2388, "E: 21/11 Win £50"),
        _comp(2389, "E: 21/11 Win £50"),
        _comp(3000, "E: 22/11 Win tickets to South East Homebuilding & Renovating Show", ["https://c.example/1"],
              closes=date(2026, 11, 22)),
    ]
    rows = closing_rows(comps)
    show = rows[0]
    assert show["name"] == "Win tickets to South East Homebuilding & Renovating Show"
    assert show["note"] == "3 listings on 4 sites" and show["statuses"] == ["manual", "queued"]
    assert show["href"] == "/competitions?sort=closing&ids=216,220,231"
    assert rows[1]["note"] == "On 2 sites" and rows[1]["href"] == "/competitions/240"
    assert [r["note"] for r in rows[2:4]] == ["vantagepointmag.co.uk"] * 2  # different shows stay apart
    assert [r["href"] for r in rows[4:6]] == ["/competitions/300", "/competitions/301"]  # different £ amounts
    assert [r["href"] for r in rows[6:8]] == ["/competitions/2388", "/competitions/2389"] and rows[6]["note"] == ""
    assert len(rows) == 8  # the 22 Nov show is another round, and there's no room left for it
    # Once the card is full, more listings of a prize already shown still join its row.
    late = _comp(3001, "E: 21/11 (MD) Win tickets to South East Homebuilding & Renovating Show",
                 ["https://comps.stuff.tv/competition/x/9.php"])
    assert closing_rows(comps[:10] + [late])[0]["note"] == "4 listings on 5 sites"


def test_competitions_page_lists_the_listings_picked_on_the_dashboard_and_win_rate_counts_every_entry():
    from sqlalchemy import func, or_, select

    from app.db import Competition, init_db, session_scope
    from app.main import app
    init_db()
    with session_scope() as s:
        picked = [Competition(source="test", source_id=f"ids-{i}", thread_url="x", title=f"Win a picked lamp {i}",
                              method="web_form", status="manual", entry_urls=[f"https://lamp{i}.example/win"])
                  for i in range(2)]
        other = Competition(source="test", source_id="ids-other", thread_url="x", title="Win a picked lamp 9",
                            method="web_form", status="entered", entry_count=1, outcome="won")
        s.add_all(picked + [other])
        s.flush()
        a, b, o = picked[0].id, picked[1].id, other.id
    with TestClient(app) as client:
        page = client.get(f"/competitions?ids={a},{b}&sort=closing", headers=H).text
        assert f'href="/competitions/{a}"' in page and f'href="/competitions/{b}"' in page
        assert f'href="/competitions/{o}"' not in page and f'name="ids" value="{a},{b}"' in page
        assert "lamp0.example" in page  # each row names its site
        dash = client.get("/", headers=H).text
    with session_scope() as s:
        entered = s.scalar(select(func.count(Competition.id)).where(
            or_(Competition.status == "entered", Competition.entry_count > 0)))
        won = s.scalar(select(func.count(Competition.id)).where(Competition.outcome == "won"))
    assert f"{100 * won / entered:.1f}%" in dash and f"{won} won of {entered} entered" in dash
