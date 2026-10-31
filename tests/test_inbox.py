from email.message import EmailMessage
from types import SimpleNamespace

from app.inbox import classify, parse_message


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
