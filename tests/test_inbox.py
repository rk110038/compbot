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