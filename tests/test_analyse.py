from datetime import date

from app.analyse import analyse, parse_body, parse_title
from app.config import DEFAULT_SETTINGS

TODAY = date(2026, 10, 31)


def test_title_parsing():
    t = parse_title("E: 06/11 (4:00pm) Win your name on the chequered flag (NL/IG)", TODAY)
    assert t["closes_on"] == date(2026, 11, 6)
    assert t["closes_time"] == "4:00pm"
    assert t["tags"] == ["nl", "ig"]
    assert t["prize"].startswith("Win your name")


def test_year_rollover_and_value():
    t = parse_title("E: 12/02 Win a £1,750 bundle worth £2k", TODAY)
    assert t["closes_on"] == date(2027, 2, 12)
    assert t["prize_value"] == 2000


def test_answer_extraction_variants():
    b = parse_body("<p>Where in France?<br>answer = <strong>Charquemont</strong></p><p>https://example.com/win</p>")
    assert b["answers"] == ["Charquemont"]
    b = parse_body("<p>Count the cats. My answer = 7 (Cats on pages 2, 3, 4, 6, 10)</p>")
    assert b["answers"] == ["7"]
    b = parse_body("<p>1. Which fish species is shown here? Perch</p><p>2. What is this lure called? Chatterbait</p>")
    assert b["answers"] == ["Perch", "Chatterbait"]
    b = parse_body("<p>Q: Unscramble the letters. A: ?????</p>")
    assert b["answers"] == []


def test_web_form_queued():
    a = analyse("E: 30/11 Win a Herbelin watch (NL)",
                '<p><a href="https://www.countryandtownhouse.com/competitions/herbelin-watch/">C&amp;TH</a></p>'
                "<p>Where?<br>answer = <strong>Charquemont</strong></p>", DEFAULT_SETTINGS, TODAY)
    assert (a.method, a.status) == ("web_form", "queued")
    assert a.answers == ["Charquemont"] and a.promoter_domain == "countryandtownhouse.com"


def test_gift_card_url_not_treated_as_terms():
    a = analyse("E: 16/11 Win a £200 gift card (NL)",
                "<p>https://northernlifemagazine.co.uk/win-a-200-galactic-fireworks-gift-card/</p>", DEFAULT_SETTINGS, TODAY)
    assert a.status == "queued" and a.entry_urls


def test_social_skipped_and_widget_manual():
    a = analyse("E: 08/11 Win a box of food packs (IG)", '<p><a href="https://www.instagram.com/p/x">IG</a></p>',
                DEFAULT_SETTINGS, TODAY)
    assert (a.method, a.status) == ("social", "skipped")
    a = analyse("E: 05/11 Win a book bundle (Gleam)", '<p><a href="https://gleam.io/abc/win">g</a></p>',
                DEFAULT_SETTINGS, TODAY)
    assert (a.method, a.status) == ("widget", "manual")


def test_ig_with_homepage_link_is_social():
    a = analyse("E: 31/10 Win 3 months of Scrumbles (IG)",
                '<p><a href="https://www.instagram.com/p/x">IG</a> <a href="https://www.scrumbles.co.uk/">shop</a></p>',
                DEFAULT_SETTINGS, TODAY)
    assert a.method == "social"


def test_email_entry():
    body = ('<p>To enter, name one artist<br>answer: <strong>John Cooper Clarke</strong></p>'
            '<p>send your ANSWER, name, EMAIL and address to <a href="mailto:comps@music-news.com">comps@music-news.com</a>'
            '<br>subject: THIRSTY QUIREBOYS</p>')
    a = analyse("E: 31/12 Win a 7in vinyl (email)", body, DEFAULT_SETTINGS, TODAY)
    assert (a.method, a.status) == ("email", "queued")
    assert a.email_to == "comps@music-news.com" and a.email_subject == "THIRSTY QUIREBOYS"


def test_email_crossword_without_answers_is_manual():
    a = analyse("E: 13/11 Win a £100 Book Token - Radio Times Xword 45",
                "<p>07. FINCH</p><p>Enter by sending an email with your answers to crossword@radiotimes.com</p>",
                DEFAULT_SETTINGS, TODAY)
    assert (a.method, a.status) == ("email", "manual")


def test_expired_and_md_multi_links():
    a = analyse("E: 20/10 Win a thing", "<p>https://example.com/win-thing</p>", DEFAULT_SETTINGS, TODAY)
    assert a.status == "expired"
    body = "".join(f"<p>https://comps.site{i}.co.uk/competition/x/{i}.php</p>" for i in range(3))
    a = analyse("E: 28/11 (MD) Win a day out", body, DEFAULT_SETTINGS, TODAY)
    assert a.is_multi and len(a.entry_urls) == 3


def test_skip_keywords():
    s = dict(DEFAULT_SETTINGS, skip_keywords="leeds, glasgow")
    a = analyse("E: 23/11 Win £200 to spend at a restaurant, Leeds", "<p>https://leeds-list.com/competitions/x</p>", s, TODAY)
    assert a.status == "skipped"


def test_bot_blocking_platform_and_account_sites_go_manual():
    body = "".join(f"<p>https://comps.site{i}.co.uk/competition/x_y/12345{i}.php</p>" for i in range(3))
    a = analyse("E: 28/11 (MD) Win a day out", body, DEFAULT_SETTINGS, TODAY)
    assert a.status == "manual" and "blocks automated entry" in a.reason
    a = analyse("E: 29/11 Win £1,000", "<p>https://comp.smoothradio.com/nescafe-2026/9</p>", DEFAULT_SETTINGS, TODAY)
    assert (a.method, a.status) == ("account", "manual")
    a = analyse("E: 16/11 Win a menu", "<p>https://www.britishairways.com/content/the-british-airways-club/competitions/x</p>",
                DEFAULT_SETTINGS, TODAY)
    assert a.method == "account"


def test_unsafe_prefix_stripped():
    a = analyse("E: 20/11 Win a book", '<p><a href="unsafe:https://www.example-books.co.uk/win-a-book">enter</a></p>',
                DEFAULT_SETTINGS, TODAY)
    assert a.entry_urls == ["https://www.example-books.co.uk/win-a-book"]
