from datetime import date
from types import SimpleNamespace as NS

from app.dedup import Candidate, Matcher, canonical_url, is_generic_path, money_amounts, title_similarity, title_tokens

D = date(2026, 11, 7)


def comp(cid, title, urls=(), domain=None, closes=D, source="mse"):
    return NS(id=cid, title=title, entry_urls=list(urls), promoter_domain=domain, closes_on=closes, source=source)


def test_canonical_url_ignores_what_doesnt_change_the_page():
    a = canonical_url("https://www.heart.co.uk/competitions/win-1000/?utm_source=mse&fbclid=x#enter")
    assert a == canonical_url("http://heart.co.uk/competitions/win-1000") == "heart.co.uk/competitions/win-1000"
    assert canonical_url("https://x.example/comp?id=7&utm_medium=a") == "x.example/comp?id=7"
    assert canonical_url("https://x.example/comp?id=7") != canonical_url("https://x.example/comp?id=8")


def test_hub_pages_are_not_one_competition():
    for u in ["https://www.kennedypublishing.co.uk/competitions", "https://vouchercloud.com/competitions/",
              "https://account.johnlewis.com/rewards", "https://example.com/", "https://www.futureplc.com/competition-rules"]:
        assert is_generic_path(u), u
    assert not is_generic_path("https://www.heart.co.uk/competitions/win-a-car/")


def test_title_tokens_keep_what_tells_comps_apart():
    assert title_tokens("E: 04/11 (MD) Win a £250 Sainsbury's voucher (NL)") == {"250", "sainsbury", "voucher"}
    # bracketed notes that aren't tags distinguish puzzles in the same issue
    assert title_tokens("E: 04/11 Win £100 store vouchers (find the dog)") != \
        title_tokens("E: 04/11 Win £100 store vouchers (fuzzy celeb)")
    assert money_amounts("Win £1,000 or £2k") == {1000, 2000}
    assert title_similarity("Estella Bartlett X Opi Bundle", "E: 05/11 Win a bundle | Estella Bartlett x OPI") == 1


def test_same_specific_entry_page_is_the_same_comp_however_worded():
    # 3 Nov: MSE had this Magic Radio comp twice
    m = Matcher([comp(1, "E: 27/11 Win a gift card worth £1,000",
                      ["https://hellorayo.co.uk/magic/competitions/win/win-a-gift-card-worth-pound1000"])])
    hit, why = m.find(Candidate("E: 27/11 win a £1,000 gift card", entry_urls=[
        "https://www.hellorayo.co.uk/magic/competitions/win/win-a-gift-card-worth-pound1000/?utm_source=x"]))
    assert hit.id == 1 and why == "same entry page"


def test_different_entry_pages_are_different_entries():
    # an (MD) comp on two magazine sites can be entered once on each
    m = Matcher([comp(1, "E: 21/11 (MD) Win tickets to the Homebuilding Show", ["https://comps.mag-a.example/c/1"])])
    assert m.find(Candidate("E: 21/11 (MD) Win tickets to the Homebuilding Show",
                            entry_urls=["https://comps.mag-b.example/c/1"]))[0] is None


def test_hub_page_shared_by_different_comps_is_not_a_match():
    jl = "https://account.johnlewis.com/rewards"
    m = Matcher([comp(1, "E: 23/11 Win a £3000 Peloton Tread", [jl]), comp(2, "E: 23/11 Win an AEG washing machine", [jl])])
    assert m.find(Candidate("E: 23/11 Win a £600 spa treatment for 2", entry_urls=[jl]))[0] is None


def test_listing_without_a_link_matches_on_site_title_and_date():
    m = Matcher([
        comp(1, "E: 06/11 (9:00am) Win a Liverpool shirt signed by van Dijk, Munoz & Mamardashvili",
             ["https://www.liverpoolfc.com/competitions/vandijk"], "liverpoolfc.com", date(2026, 11, 6)),
        comp(2, "E: 07/11 Win a Liverpool shirt signed by Alisson Becker",
             ["https://www.liverpoolfc.com/competitions/alisson"], "liverpoolfc.com", date(2026, 11, 7)),
        comp(3, "E: 04/11 Win a £250 Sainsburys voucher", ["https://www.sainsburysmagazine.co.uk/win/250"],
             "sainsburysmagazine.co.uk", date(2026, 11, 4)),
    ])
    hit, _ = m.find(Candidate("Liverpool Shirt Signed By Alisson Becker", source="ct", closes_on=date(2026, 11, 7)))
    assert hit.id == 2
    hit, why = m.find(Candidate("£250 Sainsburys Voucher", source="ct", promoter_domain="sainsburysmagazine.co.uk",
                                closes_on=date(2026, 11, 4)))
    assert hit.id == 3 and "same site" in why
    # a different amount or closing date is a different comp
    assert m.find(Candidate("£500 Sainsburys Voucher", source="ct", promoter_domain="sainsburysmagazine.co.uk",
                            closes_on=date(2026, 11, 4)))[0] is None
    assert m.find(Candidate("£250 Sainsburys Voucher", source="ct", promoter_domain="sainsburysmagazine.co.uk",
                            closes_on=date(2026, 12, 4)))[0] is None


def test_lookalike_comps_on_one_site_stay_separate():
    # in testing, loose matching merged these MSE comps: different shows, players, prizes and puzzles. In a real list
    # "tickets", "see" and "voucher" are everywhere, so the act or amount decides.
    filler = [comp(100 + i, f"E: 10/12 Win two tickets to see Act {i} plus a voucher", [], f"site{i}.example")
              for i in range(30)]
    m = Matcher(filler + [comp(1, "E: 04/11 3pm - WIN TWO TICKETS TO SEE KEHLANI", [], "globalplayer.com"),
                          comp(2, "E: 30/11 8pm Win a £200 voucher of your choice (Daily)", [], "freespindaily.co.uk")])
    assert m.find(Candidate("E: 04/11 3pm - WIN TWO TICKETS TO SEE PLACEBO", source="ct",
                            promoter_domain="globalplayer.com"))[0] is None
    assert m.find(Candidate("E: 04/11 3pm - Win two tickets to see Kehlani", source="ct",
                            promoter_domain="globalplayer.com"))[0].id == 1
    assert m.find(Candidate("Win a £100 voucher of your choice", source="ct", promoter_domain="freespindaily.co.uk",
                            closes_on=D))[0] is None
    # from the same site, only the same entry page counts: MSE posts never merge on titles alone
    assert m.find(Candidate("E: 04/11 3pm - Win two tickets to see Kehlani", source="mse",
                            promoter_domain="globalplayer.com"))[0] is None


def test_title_only_match_needs_a_very_similar_title_on_the_same_day():
    m = Matcher([comp(1, "E: 04/11 Win a Tremendously Bettys Hamper & Signed David Baldacci books",
                      ["https://bettys.example/win"], "bettys.example", date(2026, 11, 4))])
    assert m.find(Candidate("Bettys Hamper & Signed David Baldacci Books", source="ct",
                            closes_on=date(2026, 11, 4)))[0].id == 1
    assert m.find(Candidate("Bettys Hamper & Signed David Baldacci Books", source="ct",
                            closes_on=date(2026, 11, 20)))[0] is None


def test_company_from_competition_database_helps_match():
    m = Matcher([comp(1, "E: 03/11 Win a £500 VIP table for 4 at Tattu Canary Wharf",
                      ["https://www.sheerluxe.com/win/tattu"], "sheerluxe.com", date(2026, 11, 3))])
    hit, _ = m.find(Candidate("VIP Table For 4 At The Tattu Canary Wharf Launch", source="cdb", extra_title="Sheerluxe",
                              entry_urls=["https://sheerluxe.com/win/tattu?ref=cdb"], closes_on=date(2026, 11, 3)))
    assert hit.id == 1


def test_another_round_on_the_same_page_is_a_new_comp():
    # a weekly/monthly comp that reuses its page: same link, new closing date or prize
    page = "https://radio.example/competitions/this-week"
    m = Matcher([comp(10, "E: 01/11 Win a £100 Radio Example cash prize", [page], closes=date(2026, 11, 1))])
    assert m.find(Candidate("E: 08/11 Win a Radio Example cash prize", entry_urls=[page],
                            closes_on=date(2026, 11, 8)))[0] is None
    assert m.find(Candidate("E: 01/11 Win a £250 Radio Example cash prize", entry_urls=[page],
                            closes_on=date(2026, 11, 1)))[0] is None
    assert m.find(Candidate("E: 01/11 Win £100 Radio Example cash", entry_urls=[page],
                            closes_on=date(2026, 11, 1)))[0].id == 10