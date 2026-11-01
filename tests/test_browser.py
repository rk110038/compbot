import pytest

from app.config import DEFAULT_SETTINGS, get_config
from app.entry.browser import Browser
from app.entry.forms import Ctx


@pytest.fixture(scope="module")
def browser():
    get_config().screenshot_dir.mkdir(parents=True, exist_ok=True)
    with Browser() as b:
        yield b


def _settings(**over):
    s = dict(DEFAULT_SETTINGS)
    s.update(over)
    return s


def test_full_form_auto_submit(browser, fixture_server, profile):
    ctx = Ctx(profile=profile, answers=["Charquemont"], tags=["nl"])
    res = browser.enter(f"{fixture_server}/comp_form.html", ctx,
                        _settings(entry_mode="auto", respect_automation_clauses=False), "t1", {})
    assert res.result == "entered", (res.message, res.details)
    acts = {f["label"]: f["action"] for f in res.details["fields"]}
    assert any("third parties" in k and v == "uncheck" for k, v in acts.items())
    assert any("newsletter" in k and v == "check" for k, v in acts.items())  # NL comp -> newsletter ticked
    assert not any(k.lower().startswith("website") for k in acts)  # honeypot never touched


def test_dry_run_does_not_submit(browser, fixture_server, profile):
    ctx = Ctx(profile=profile, answers=["Charquemont"])
    res = browser.enter(f"{fixture_server}/comp_form.html", ctx,
                        _settings(entry_mode="dry_run", respect_automation_clauses=False), "t2", {})
    assert res.result == "dry_run_ok", res.message
    assert res.screenshot_before and not res.screenshot_after


def test_terms_clause_blocks(browser, fixture_server, profile):
    ctx = Ctx(profile=profile, answers=["Charquemont"])
    res = browser.enter(f"{fixture_server}/comp_form.html", ctx, _settings(entry_mode="auto"), "t3", {})
    assert res.result == "tc_blocked"
    assert "automated" in res.message


def test_missing_answer_needs_input(browser, fixture_server, profile):
    ctx = Ctx(profile=profile, answers=[])
    res = browser.enter(f"{fixture_server}/comp_form.html", ctx,
                        _settings(entry_mode="auto", respect_automation_clauses=False), "t4", {})
    assert res.result == "needs_input"
    assert "Herbelin" in res.message


def test_captcha_goes_manual(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/captcha.html", Ctx(profile=profile), _settings(entry_mode="auto"), "t5", {})
    assert res.result == "captcha"


def test_reveal_button_and_iframe(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/iframe_outer.html", Ctx(profile=profile, tags=["nl"]),
                        _settings(entry_mode="auto"), "t6", {})
    assert res.result == "entered", (res.message, res.details)


def test_multistep_prefers_comp_form_over_footer_newsletter(browser, fixture_server, profile):
    ctx = Ctx(profile=profile, answers=["I'm a Celebrity...Get Me Out of Here!"])
    res = browser.enter(f"{fixture_server}/multistep.html", ctx, _settings(entry_mode="auto"), "t7", {})
    assert res.result == "entered", (res.message, res.details)
    assert any("step 2" in s for s in res.details["steps"])


def test_dry_run_never_clicks_buttons(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/oneclick.html", Ctx(profile=profile), _settings(entry_mode="dry_run"), "t8", {})
    assert res.details["steps"] == []
    assert res.result != "entered"


def test_private_address_refused(browser, fixture_server, profile, monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_URLS", raising=False)
    res = browser.enter(f"{fixture_server}/comp_form.html", Ctx(profile=profile), _settings(entry_mode="auto"), "t9", {})
    assert res.result == "blocked" and "non-public" in res.message


def test_lazy_iframe_does_not_hang(browser, fixture_server, profile):
    import time
    t0 = time.time()
    res = browser.enter(f"{fixture_server}/lazy_iframe.html", Ctx(profile=profile, tags=["nl"]),
                        _settings(entry_mode="auto"), "t10", {})
    assert res.result == "entered", (res.message, res.details)
    assert time.time() - t0 < 90


def test_isolated_entry_round_trip(fixture_server, profile):
    from app.entry.browser import enter_isolated
    res = enter_isolated(f"{fixture_server}/comp_form.html", Ctx(profile=profile, answers=["Charquemont"]),
                         _settings(entry_mode="dry_run", respect_automation_clauses=False), "t11", {}, timeout=120)
    assert res.result == "dry_run_ok", res.message


def test_isolated_entry_hard_timeout():
    import time
    from app.entry.browser import enter_isolated
    t0 = time.time()
    # Unroutable address: goto would sit for 45 s; the watchdog must cut it off first.
    res = enter_isolated("http://10.255.255.1/", Ctx(profile={}), _settings(entry_mode="auto"), "t12", {}, timeout=4)
    assert res.result == "error" and "Timed out" in res.message
    assert time.time() - t0 < 30


# --- regressions from the first overnight run (1 Nov 2026) ---------------------------------------------------------

def test_staged_fields_revealed_after_answer(browser, fixture_server, profile):
    # Northern Life (Fluent Forms): name/email/consent and the submit button appear only after the answer.
    ctx = Ctx(profile=profile, answers=["2026"], question="What year are the cushions from?", tags=["nl"])
    res = browser.enter(f"{fixture_server}/staged.html", ctx, _settings(entry_mode="auto"), "r1", {})
    assert res.result == "entered", (res.message, res.details)
    assert any("more fields appeared" in s for s in res.details["steps"])


def test_submit_covered_by_overlay(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/overlay.html", Ctx(profile=profile, tags=["nl"]),
                        _settings(entry_mode="auto"), "r2", {})
    assert res.result == "entered", (res.message, res.details)
    assert any("pop-up" in s for s in res.details["steps"])


def test_redrawn_form_is_not_resubmitted(browser, fixture_server, profile):
    # House of Bruar: the same form re-rendered after submitting was treated as "another page" and sent 3 times.
    res = browser.enter(f"{fixture_server}/redraw.html", Ctx(profile=profile), _settings(entry_mode="auto"), "r3", {})
    clicks = [s for s in res.details["steps"] if "clicked" in s]
    assert len(clicks) == 1, res.details["steps"]
    assert res.result == "failed" and "nothing changed" in res.message


def test_consent_gated_embed_form(browser, fixture_server, profile):
    # Gardeners' World: the entry form is a Riddle embed held back until "Accept and continue" is clicked.
    res = browser.enter(f"{fixture_server}/embed_consent.html", Ctx(profile=profile), _settings(entry_mode="auto"), "r4", {})
    assert res.result == "entered", (res.message, res.details)
    assert any("allowed the embedded entry form" in s for s in res.details["steps"]) or res.details.get("cookies")


def test_delayed_error_is_reported_not_unverified(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/delayed_error.html", Ctx(profile=profile), _settings(entry_mode="auto"), "r5", {})
    assert res.result == "failed" and "error occurred" in res.message, res.message


def test_cookie_handler_never_presses_entry_form_buttons(browser, fixture_server, profile):
    # A page whose URL contains "consent"/"cookie" must not make the cookie step click the form's "OK" submit.
    res = browser.enter(f"{fixture_server}/cookie-consent-win.html", Ctx(profile=profile), _settings(entry_mode="dry_run"), "r6", {})
    assert res.result == "dry_run_ok", res.message
    assert not res.details.get("cookies")


def test_invisible_recaptcha_is_not_a_captcha(browser, fixture_server, profile):
    # Sainsbury's Magazine, Mailchimp, Heinz...: only an invisible reCAPTCHA badge - no challenge to solve.
    res = browser.enter(f"{fixture_server}/captcha_invisible.html", Ctx(profile=profile), _settings(entry_mode="auto"), "r7", {})
    assert res.result == "entered", (res.message, res.details)
