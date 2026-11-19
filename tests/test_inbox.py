from email.message import EmailMessage
from types import SimpleNamespace

from app.inbox import classify, explain, gmail_link, parse_message, resolve_folders


def test_spam_folder_resolution():
    uk = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Junk) "/" "[Google Mail]/Spam"',
          b'(\\All \\HasNoChildren) "/" "[Google Mail]/All Mail"']
    assert resolve_folders(uk, ["INBOX", "[Gmail]/Spam"]) == ["INBOX", "[Google Mail]/Spam"]
    us = [b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']
    assert resolve_folders(us, ["INBOX", "spam"]) == ["INBOX", "[Gmail]/Spam"]
    assert resolve_folders([], ["INBOX", "[Gmail]/Spam"]) == ["INBOX", "[Gmail]/Spam"]


def _mail(frm, subject, body, html=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "me@gmail.com", subject
    m["Message-ID"] = f"<{abs(hash(subject))}@test>"
    m["Date"] = "Sat, 31 Oct 2026 10:00:00 +0000"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    return parse_message(m.as_bytes(), "INBOX")


COMPS = [SimpleNamespace(id=1, prize="Win a Herbelin Art Deco watch worth £710", title="E: 30/11 Win a Herbelin watch",
                         entry_urls=["https://www.countryandtownhouse.com/competitions/herbelin-watch/"],
                         promoter_domain="countryandtownhouse.com", email_to=None),
         SimpleNamespace(id=2, prize="Win a Nordica Gear Bundle", title="Win a Nordica Gear Bundle",
                         entry_urls=["https://www.fall-line.co.uk/win-a-nordica-gear-bundle/"],
                         promoter_domain="fall-line.co.uk", email_to=None)]


def test_matched_win():
    m = _mail("Competitions <comps@countryandtownhouse.com>", "Congratulations - you've won!",
              "Dear Test, we are delighted to tell you that you are the winner of our Herbelin watch competition. "
              "Please reply to confirm your delivery address.")
    c = classify(m, COMPS)
    assert c.category == "possible_win" and c.competition_id == 1


def test_marketing_is_not_a_win():
    m = _mail("Shop <news@bigshop.com>", "Last chance to win £1000! 20% off everything",
              "Enter now for your chance to win. Shop now. Unsubscribe")
    assert classify(m, COMPS).category == "promo"


def test_scam_flagged():
    m = _mail("Prize Dept <claims@prizes-uk.biz>", "Congratulations, you have won £50,000",
              "You have been selected as a winner. Pay the small processing fee and send your bank details to claim.")
    assert classify(m, COMPS).category == "scam_suspect"


def test_loss_and_confirm_link():
    m = _mail("Fall-Line <hello@fall-line.co.uk>", "Nordica competition results",
              "Unfortunately you were not successful this time. Better luck next time!")
    c = classify(m, COMPS)
    assert c.category == "loss" and c.competition_id == 2
    m = _mail("Fall-Line <hello@fall-line.co.uk>", "Please confirm your subscription",
              "Click the link below to confirm.",
              html='<p>Click below to confirm</p><a href="https://fall-line.co.uk/confirm?t=abc">Confirm my email</a>'
                   '<a href="https://fall-line.co.uk/unsubscribe">Unsubscribe</a>')
    c = classify(m, COMPS)
    assert c.category == "confirm" and c.confirm_link == "https://fall-line.co.uk/confirm?t=abc"


def test_compbots_own_alerts_are_recognised():
    def raw(frm, subject, tagged=False):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, "me@gmail.com", subject, "<own@test>"
        if tagged:
            m["X-Compbot"] = "alert"
        m.set_content("From: winners@prizes.example\nSubject: Congratulations, you've won!")
        return m.as_bytes()

    assert parse_message(raw("me@gmail.com", "[compbot] Possible win: Congratulations", True), "INBOX").own
    # Alerts sent before they were tagged: a "[compbot]" subject from your own address.
    assert parse_message(raw("Me <ME@gmail.com>", "[compbot] Possible competition win!"), "INBOX", "me@gmail.com").own
    assert not parse_message(raw("x@prizes.example", "[compbot] Possible competition win!"), "INBOX",
                             "me@gmail.com").own
    assert not parse_message(raw("me@gmail.com", "Fwd: Congratulations"), "INBOX", "me@gmail.com").own


def test_why_an_email_looks_like_a_win_is_in_its_own_words():
    m = _mail("Competitions <comps@countryandtownhouse.com>", "Congratulations - you've won!",
              "View in browser. Dear Test, thank you for entering. We are delighted to tell you that you are the winner "
              "of our Herbelin watch competition. Please reply to confirm your delivery address.")
    c = classify(m, COMPS)
    assert c.category == "possible_win" and c.competition_id == 1
    assert "win:Congratulations" in c.signals and not any("\\b" in s for s in c.signals)  # words, not patterns
    x = explain(m.subject, m.text, m.from_domain, COMPS[0], c.category, c.signals)
    assert x.why[0].startswith("it says \u201cCongratulations\u201d, \u201cyou've won\u201d")
    assert (x.quote_before, x.quote_hit, x.quote_after) == (
        "We are delighted to tell you that ", "you are the winner", " of our Herbelin watch competition.")
    assert x.match == ["it's from the competition's site (countryandtownhouse.com)",
                       "both mention \u201cherbelin\u201d, \u201cwatch\u201d"]
    loss = _mail("Fall-Line <hello@fall-line.co.uk>", "Nordica competition results",
                 "Hello. Unfortunately you were not successful this time. Better luck next time!")
    x = explain(loss.subject, loss.text, loss.from_domain, COMPS[1], "loss")
    # A short sentence carries on into the next one.
    assert x.quote_hit == "Unfortunately you were not" and x.quote_after == " successful this time. Better luck next time!"


def test_gmail_link_opens_the_email_itself():
    assert gmail_link("<abc+1=x@mail.example.com>", "me@gmail.com") == (
        "https://mail.google.com/mail/?authuser=me%40gmail.com#search/rfc822msgid%3Aabc%2B1%3Dx%40mail.example.com")
    assert gmail_link("<no-id-0123>") is None and gmail_link("") is None

def test_alerts_are_skipped_from_their_headers_alone():
    from email import message_from_bytes

    from app.inbox import _own_alert_headers

    # Encoded subjects are read as words: "[compbot] Possible win: £500".
    head = message_from_bytes(b"Message-ID: <a@b>\r\nFrom: Me <me@gmail.com>\r\n"
                              b"Subject: =?utf-8?q?=5Bcompbot=5D_Possible_win=3A_=C2=A3500?=\r\n\r\n")
    assert _own_alert_headers(head, "me@gmail.com")
    assert _own_alert_headers(message_from_bytes(b"From: x@y.com\r\nSubject: Hi\r\nX-Compbot: alert\r\n\r\n"), None)
    assert not _own_alert_headers(message_from_bytes(b"From: x@y.com\r\nSubject: [compbot] hi\r\n\r\n"),
                                  "me@gmail.com")

# --------------------------------------------------------------------------- guessing which entry an email is about

def _entered(i, title, prize="", urls=(), domain=None, days_ago=20, email_to=None):
    from datetime import datetime, timedelta
    when = datetime(2026, 11, 18) - timedelta(days=days_ago)
    return SimpleNamespace(id=i, title=title, prize=prize, entry_urls=list(urls), promoter_domain=domain,
                           email_to=email_to, entered_at=when, last_entered_at=when)


# Titles from the Inbox's old drop-down (18 Nov 2026), plus the Heinz comp a win email came from and a decoy that
# shares its "£50" and "home".
ENTERED = [_entered(i, t) for i, t in enumerate([
    "Win a £250 voucher to spend on fine wine at Squelch", "Win a £250 voucher for Signor Sassi restaurant, Knightsbridge",
    "Win a £100 voucher for Rose & Caramel HQ beauty salon in Bolton",
    "Win a 3-night winter break for 2 in your choice of 3 Swiss towns",
    "Win a £7,700 London weekend for 2 inc 2-nights at Hotel Café Royal",
    "Win 1 of 5 £30 birdfeeder & birdhouse bundles from Elho", "Win a bundle of 12 Carol Ann Duffy festive poetry books",
    "Win Christopher Nolan's The Odyssey on 4K UHD", "Win 1 of 2 Gruffalo Party Bags",
    "Win a £250 Waitrose voucher with Jus-Rol", "Win Daisy May & Charlie Cooper's Nightwatch on Blu-ray", "Win £500",
    "Win a pair of XP-Pro Rogue Nebula gaming glasses", "Win a Collection of Stephen King Retro Paperback Editions",
    "Win an England shirt signed by Will Joseph", "Win Various Prizes - Your Healthy Living Mag",
    "Win a Champagne Afternoon Tea for 4 at Pan Pacific London", "Win a tin of Honeybuns Cakes"], start=1)] + [
    _entered(50, "E: 21/11 Win a £250 Honest Burgers voucher, a Halloween party, gift hamper or Heinz to Home vouchers",
             urls=["https://www.heinz.com/en-GB/competition/halloween"], domain="heinz.com", days_ago=40),
    _entered(51, "Win a £50 Home Bargains gift card", urls=["https://www.homebargains.co.uk/win"], days_ago=5)]

HEINZ = ("Congratulations! We are delighted to confirm that you have won a £50 Heinz to Home voucher! Your unique "
         "code is HZ-1234. Please redeem it before 31 December 2026. Kind regards, Consumer Services UK & I")


def test_best_guess_finds_the_competition_a_win_email_is_about():
    from datetime import datetime

    from app.inbox import CompIndex

    g = CompIndex(ENTERED).rank("You've won a £50 Voucher for Heinz to Home", HEINZ, "kraftheinz.com",
                                "Consumer Services UK & I", datetime(2026, 11, 18, 8, 8))
    assert g.guesses[0].comp.id == 50 and g.confident and g.pool == len(ENTERED)
    why = "; ".join(g.guesses[0].reasons)
    assert "the sender, kraftheinz.com, looks like the competition's site (heinz.com)" in why
    assert "\u201cheinz\u201d" in why and "\u201chome\u201d" in why
    assert all(x.comp.id != 50 for x in g.guesses[1:])


def test_a_guess_needs_to_stand_out_before_it_is_ticked():
    from datetime import datetime

    from app.inbox import CompIndex

    idx = CompIndex(ENTERED)
    # Nothing in it names a prize: no guess worth showing.
    g = idx.rank("Congratulations!", "You have won a prize in our draw. Reply with your address.", "prizes.example")
    assert not g.confident and not g.guesses
    # "£250 voucher" fits three entries equally: show them, tick none.
    g = idx.rank("You won a £250 voucher", "Congratulations, your £250 voucher is on its way.", "vouchers.example")
    assert not g.confident and len(g.guesses) == 3
    # Two rare words in the subject are enough on their own, from any sender.
    g = idx.rank("You've won Gruffalo party bags!", "Well done.", "gmail.com", "Jo Bloggs", datetime(2026, 11, 18))
    assert g.confident and g.guesses[0].comp.id == 9
    # A free email address is never "the competition's site", even if a comp's entry email used the same provider.
    by_mail = CompIndex([_entered(1, "Win a hamper", email_to="comps@gmail.com"),
                         _entered(2, "Win a kettle")])
    assert not by_mail.rank("Hello", "Hi there", "gmail.com").guesses


def test_a_competition_entered_after_the_email_is_not_a_guess():
    from datetime import datetime

    from app.inbox import CompIndex

    later = [_entered(1, "Win 1 of 2 Gruffalo Party Bags", days_ago=-3)]
    assert not CompIndex(later).rank("Gruffalo party bags", "You won", "", "", datetime(2026, 11, 18)).guesses
    assert CompIndex(later).rank("Gruffalo party bags", "You won", "", "", None).guesses  # date unknown: allowed


def test_typing_a_few_words_finds_an_entry():
    from app.inbox import CompIndex

    idx = CompIndex(ENTERED)
    assert [c.id for c in idx.search("heinz")] == [50]
    assert [c.id for c in idx.search("  Squelch WINE ")] == [1]
    assert [c.id for c in idx.search("gruff")] == [9]          # the start of a word is enough
    assert [c.id for c in idx.search("cafe royal")] == [5]     # accents don't matter
    assert [c.id for c in idx.search("nolans")] == [8]         # nor apostrophes
    assert [c.id for c in idx.search("#51")] == [51]
    assert [c.id for c in idx.search("£250")][:1] and all(c.id in {1, 2, 10, 50} for c in idx.search("£250"))
    assert idx.search("homebargains")[0].id == 51              # the site counts too
    assert idx.search("") == [] and idx.search("zzzz") == []


def test_the_sender_can_name_a_competition_found_without_a_site():
    from app.inbox import CompIndex

    idx = CompIndex([_entered(1, "Win Heinz to Home vouchers"), _entered(2, "Win a kettle"),
                     _entered(3, "Win a toaster")])
    g = idx.rank("Your prize", "Here is your code.", "kraftheinz.com")
    assert g.guesses and g.guesses[0].comp.id == 1
    assert g.guesses[0].reasons == ["the sender, kraftheinz.com, has \u201cheinz\u201d in its name"]


def test_a_runner_up_sharing_only_common_words_still_stops_a_tick():
    from app.inbox import CompIndex

    # In 300 entries "voucher" is in 100, "gift" in 90 and "family" in 40: all common, "family" a little less so.
    filler = [_entered(100 + i, f"Win a {w} {'voucher ' if i < 98 else ''}{'gift ' if 98 <= i < 186 else ''}"
                                f"{'family ' if 186 <= i < 225 else ''}prize")
              for i, w in enumerate(f"thing{j:03d}" for j in range(298))]
    pool = filler + [_entered(1, "Win a £100 gift voucher for the family"), _entered(2, "Win a £100 gift voucher")]
    g = CompIndex(pool).rank("You've won a £100 family gift voucher", "Congratulations.", "rewards-mailer.com")
    assert [x.comp.id for x in g.guesses[:2]] == [1, 2] and not g.confident
