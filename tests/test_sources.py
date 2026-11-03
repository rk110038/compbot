import dataclasses
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from app.analyse import analyse
from app.config import DEFAULT_SETTINGS, get_config
from app.sources import cdb, ct
from app.sources.common import as_post, relative_close

CT_PAGE = """
<div data-entry-options="marketing" data-ignored="false" id="500-dissh-vouchers" class="cc2-card border overflow-hidden mb-4 ">
  <div class="cc2-titlebar d-flex"><h3 class="cc2-title mb-0">&#xA3;500 Dissh Vouchers</h3>
    <div><a class="cc2-website text-decoration-none" title="View all" href="/comps-from/sheerluxe_com">sheerluxe.com</a></div>
    <div data-entry-options-toggle="89161" class="float-end my-2 d-none"></div></div>
  <div class="cc2-body " data-entry-options-content="89161">
    <div class="cc2-closes"><div class="cc2-label">Closes:</div>
      <div class="d-flex"><span class="cc2-date-text"> 4th Nov 2026</span><span>tomorrow</span></div></div>
    <a class="cc2-tag text-decoration-none mt-0" href="/fashion-comps/fashion-vouchers-tag">[Fashion Vouchers]</a>
    <div class="cc2-added d-inline-flex align-items-center gap-1"><svg viewBox="0 0 1 1"><path d="M0"/></svg>
                    Added  3rd Nov 2026
                </div>
    <div class="cc2-quote-body"><p>Win &#xA3;500 to spend at Dissh. See https://dissh.example for more.</p></div>
    <div class="cc2-details mt-2"><div class="cc2-label mb-2">Details Required:</div>
      <div class="d-flex"><span class="cc2-pill">
                                Name
                            </span><span class="cc2-pill">
                                Email
                            </span></div></div>
    <div class="mt-3 px-2 text-center"><div class="cc2-label">Question:</div>
      <span class="d-block">What value is the voucher prize?</span></div>
    <div class="my-3 px-2 text-center"><div class="cc2-label">Suggested Answer:</div>
      <span class="d-block fw-bold">&#xA3;500</span></div>
    <a class="cc2-enter-btn  d-block" target="_blank" href="/redir/89161">Enter Competition</a>
  </div>
</div>
<div data-entry-options="registration" data-ignored="false" id="boots-gift-guide" class="cc2-card border mb-4 ">
  <div class="cc2-titlebar"><h3 class="cc2-title mb-0">Boots Gift Guide</h3><div></div></div>
  <div class="cc2-body">
    <span class="cc2-date-text"> 24th Dec 2026</span>
    <a class="cc2-tag" href="/beauty-comps/gift-tag">[Gifts]</a>
    <a class="cc2-enter-btn" href="/redir/90386">Enter Competition</a>
  </div>
</div>
"""


def test_competitions_time_cards():
    a, b = ct.parse_cards(CT_PAGE)
    assert a["source_id"] == "ct:89161" and a["title"] == "£500 Dissh Vouchers"
    assert a["closes_on"] == date(2026, 11, 4) and a["posted_at"] == datetime(2026, 11, 3)
    assert a["promoter_domain"] == "sheerluxe.com" and a["tags"] == ["nl"] and a["answers"] == ["£500"]
    assert a["question"] == "What value is the voucher prize?" and a["notes"] == ["Details required: Name, Email"]
    assert a["url"] == "https://www.competitions-time.co.uk/comps-from/sheerluxe_com#500-dissh-vouchers"
    assert a["manual_link"] == "https://www.competitions-time.co.uk/redir/89161" and a["entry_urls"] == []
    assert b["source_id"] == "ct:90386" and b["tags"] == ["account"] and b["promoter_domain"] is None
    assert b["url"] == "https://www.competitions-time.co.uk/beauty-comps#boots-gift-guide"


def test_listing_reads_like_an_mse_post():
    a = ct.parse_cards(CT_PAGE)[0]
    title, body = as_post(a)
    assert title == "E: 04/11 Win £500 Dissh Vouchers (NL)"
    r = analyse(title, body, dict(DEFAULT_SETTINGS), date(2026, 11, 3))
    assert r.closes_on == date(2026, 11, 4) and r.answers == ["£500"] and "nl" in r.tags
    assert r.entry_urls == []  # a link in the description is not the entry page
    r = analyse(*as_post(dict(a, entry_urls=["https://sheerluxe.com/win/dissh"])), dict(DEFAULT_SETTINGS),
                date(2026, 11, 3))
    assert r.entry_urls == ["https://sheerluxe.com/win/dissh"] and r.method == "web_form" and r.status == "queued"
    b = ct.parse_cards(CT_PAGE)[1]
    assert analyse(*as_post(b), dict(DEFAULT_SETTINGS), date(2026, 11, 3)).method == "account"
    assert as_post({"title": "Prize", "company": "Magic Radio", "closes_on": date(2027, 1, 1),
                    "closes_precise": False})[0] == "E: 01/01? Win Prize - Magic Radio"


def test_relative_closing_dates():
    now = datetime(2026, 11, 3, 16, 0, tzinfo=ZoneInfo("Europe/London"))
    assert relative_close("Competition  closes in 24 days and 8 hours", now) == (date(2026, 11, 28), True)
    assert relative_close("closes in 9 hours and 24 minutes", now) == (date(2026, 11, 4), True)
    assert relative_close("closes in 1 month and 28 days", now) == (date(2027, 1, 1), False)
    assert relative_close("closes in 1 year and 1 month", now)[1] is False
    assert relative_close("", now) == (None, False)


def _cdb_card(cid, title, closes, href):
    return f"""<div class="comp_slot" id="comp_slot_1"><div class="panel panel-default panel-competition">
  <div class="panel-heading competition_panel_heading"><a href="/view/{cid}/Slug/">{title}</a>
  <div style="font-size:0.8em;">Competition  closes in {closes}</div></div>
  <div id="competition_pane_{cid}_main"><p><b>Website / Blog / Other</b></p>
  <a rel="nofollow" class="btn btn-info" href="{href}" target="_blank">Enter Here</a><p>Simple Form</p></div></div></div>"""


def _cdb_page(cards, page, more=True):
    nxt = f'<a href="/quick-links/simple-form-competitions/{page + 1}/5/easy-to-enter-free-competitions/">next</a>'
    return "<html>Displaying results <b>1</b> to <b>5</b> of <b>409</b>" + "".join(cards) + (nxt if more else "") + "</html>"


def test_competition_database_reads_newest_first_and_stops_at_known(monkeypatch):
    p1 = _cdb_page([
        _cdb_card("aaa1", "Sheerluxe - VIP Table For 4 At Tattu", "9 hours and 24 minutes", "https://www.sheerluxe.com/win/tattu"),
        _cdb_card("bbb2", "Magic Radio - &#xA3;1,000 Gift Card", "24 days and 8 hours", "/go/bbb2/"),
        _cdb_card("ccc3", "Competition Database - Our Christmas Hamper", "20 days and 1 hour", "/giveaway/ccc3/"),
    ], 1)
    p2 = _cdb_page([_cdb_card("aaa1", "Sheerluxe - VIP Table For 4 At Tattu", "9 hours and 24 minutes",
                              "https://www.sheerluxe.com/win/tattu"),  # pushed down a page by a newer comp
                    _cdb_card("old1", "A - Old prize", "3 days and 1 hour", "https://a.example/old")], 2)
    p3 = _cdb_page([_cdb_card("old2", "B - Older prize", "2 days and 1 hour", "https://b.example/old")], 3)
    p4 = _cdb_page([_cdb_card("new4", "C - Never reached", "2 days and 1 hour", "https://c.example/x")], 4, more=False)
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        path = req.url.path
        if path == "/login/" and req.method == "GET":
            return httpx.Response(200, text='<form id="logInForm"><input type="hidden" name="user_action_id" value="TOK"></form>')
        if path == "/login/":
            assert b"user_action_id=TOK" in req.content and b"user_username=me" in req.content
            return httpx.Response(200, text="welcome")
        if path == "/go/bbb2/":
            return httpx.Response(302, headers={"location": "https://hellorayo.co.uk/magic/win-1000"})
        if "/simple-form-competitions/" in path:
            n = int(path.split("/simple-form-competitions/")[1].split("/")[0])
            if req.method == "POST":
                assert b"filter_sort=4" in req.content
            return httpx.Response(200, text={1: p1, 2: p2, 3: p3, 4: p4}[n])
        return httpx.Response(404)

    monkeypatch.setattr(cdb, "get_config", lambda: dataclasses.replace(get_config(), cdb_username="me",
                                                                         cdb_password="pw"))
    monkeypatch.setattr(cdb, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler),
                                                             follow_redirects=True))
    monkeypatch.setattr(cdb.time, "sleep", lambda *_: None)
    out = cdb.fetch_listings({"cdb:old1", "cdb:old2"})
    assert [x["source_id"] for x in out] == ["cdb:aaa1", "cdb:bbb2"]  # not its own giveaway; stopped before page 4
    assert out[0]["entry_urls"] == ["https://www.sheerluxe.com/win/tattu"] and out[0]["company"] == "Sheerluxe"
    assert out[1]["entry_urls"] == ["https://hellorayo.co.uk/magic/win-1000"]
    assert out[1]["promoter_domain"] == "hellorayo.co.uk" and out[1]["title"] == "£1,000 Gift Card"
    assert not any("/simple-form-competitions/4/" in s for s in seen)


def test_competition_database_rejects_a_visitor_view(monkeypatch):
    visitor = _cdb_page([_cdb_card("aaa1", "X - Y", "1 day and 1 hour", "/register/from_entry/")], 1)

    def handler(req):
        if req.url.path == "/login/":
            return httpx.Response(200, text="<form id='logInForm'></form>")
        return httpx.Response(200, text=visitor)

    monkeypatch.setattr(cdb, "get_config", lambda: dataclasses.replace(get_config(), cdb_username="me",
                                                                         cdb_password="bad"))
    monkeypatch.setattr(cdb, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(cdb.time, "sleep", lambda *_: None)
    ok, msg = cdb.self_test()
    assert not ok and "didn't accept the sign-in" in msg
    monkeypatch.setattr(cdb, "get_config", lambda: dataclasses.replace(get_config(), cdb_username=None))
    assert "Not set up" in cdb.self_test()[1]


def test_competitions_time_reads_every_page(monkeypatch):
    page1 = CT_PAGE + '<a href="/page/2">2</a>'
    page2 = CT_PAGE.replace("89161", "89999").replace("90386", "90999")

    def handler(req):
        return httpx.Response(200, text={"/": page1, "/page/2": page2}.get(req.url.path, ""))

    real = httpx.Client
    monkeypatch.setattr(ct.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(ct.time, "sleep", lambda *_: None)
    out = ct.fetch_listings({"ct:90386"})
    assert sorted(x["source_id"] for x in out) == ["ct:89161", "ct:89999", "ct:90999"]
