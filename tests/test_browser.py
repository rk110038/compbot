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
    assert isinstance(res.details["timing"]["shutdown_seconds"], float)


def test_result_is_handed_back_before_the_page_closes(browser, fixture_server, profile):
    seen = []
    res = browser.enter(f"{fixture_server}/comp_form.html", Ctx(profile=profile, answers=["Charquemont"]),
                        _settings(entry_mode="dry_run", respect_automation_clauses=False), "t14", {},
                        on_result=lambda r: seen.append((r.result, len(browser.browser.contexts))))
    assert seen == [("dry_run_ok", 1)] and res.result == "dry_run_ok"  # sent while the entry's page was still open
    assert not browser.browser.contexts


def test_stuck_browser_shutdown_costs_seconds_not_the_whole_time_limit(monkeypatch):
    # 2 Nov: the Live view sat on "Finished" for minutes; closing the browser had no time limit of its own, and the
    # result only came back once it was closed (or the 10-minute watchdog fired and threw the result away).
    import time
    from app.entry import browser as br
    monkeypatch.setattr(br, "_WORKER_MODULE", "tests.fixtures.slow_close_worker")
    monkeypatch.setattr(br, "SHUTDOWN_GRACE_S", 2)
    t0 = time.time()
    res = br.enter_isolated("https://example.com/", Ctx(profile={}), _settings(), "t15", {}, timeout=90)
    assert res.result == "failed" and res.message == "No entry form found on the page"
    assert res.details["timing"]["shutdown_seconds"] == "stuck (stopped after 2s)"
    assert time.time() - t0 < 45


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


def test_required_error_gets_one_corrected_resubmit(browser, fixture_server, profile):
    # Red Torpedo (Klaviyo): interests + email consent are only demanded after submitting.
    res = browser.enter(f"{fixture_server}/required_retry.html", Ctx(profile=profile, tags=["nl"]),
                        _settings(entry_mode="auto"), "r8", {})
    assert res.result == "entered", (res.message, res.details)
    assert any("site said fields were required" in s for s in res.details["steps"])
    assert sum("clicked" in s for s in res.details["steps"]) == 2


def test_single_option_terms_radios(browser, fixture_server, profile):
    # Jimmy's (Jotform): "I agree to the terms" / "privacy policy" are lone radio buttons.
    res = browser.enter(f"{fixture_server}/single_radio_terms.html", Ctx(profile=profile),
                        _settings(entry_mode="auto"), "r9", {})
    assert res.result == "entered", (res.message, res.details)


def test_terms_link_that_loads_late_is_still_checked(browser, fixture_server, profile):
    # Rayo: form + T&C link appear only after scrolling; the T&Cs ban automated entries.
    res = browser.enter(f"{fixture_server}/late_terms.html", Ctx(profile=profile), _settings(entry_mode="auto"), "r10", {})
    assert res.result == "tc_blocked", (res.result, res.message)


def test_start_the_quiz_button_then_questions(browser, fixture_server, profile):
    ctx = Ctx(profile=profile, answers=["Alpine", "Abu Dhabi"], question="quiz")
    res = browser.enter(f"{fixture_server}/quiz.html", ctx, _settings(entry_mode="auto"), "r11", {})
    assert res.result == "entered", (res.message, res.details)
    assert any("Start the quiz" in s for s in res.details["steps"])


def test_dry_run_presses_safe_quiz_button(browser, fixture_server, profile):
    ctx = Ctx(profile=profile, answers=["Alpine", "Abu Dhabi"], question="quiz")
    res = browser.enter(f"{fixture_server}/quiz.html", ctx, _settings(entry_mode="dry_run"), "r12", {})
    assert res.result == "dry_run_ok", (res.message, res.details)


def test_live_view_streams_frames_and_status(browser, fixture_server, profile, tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setenv("LIVE_DIR", str(tmp_path))
    res = browser.enter(f"{fixture_server}/quiz.html", Ctx(profile=profile, answers=["Alpine", "Abu Dhabi"]),
                        _settings(entry_mode="auto", live_view=True), "lv1", {"title": "Win a helmet"})
    assert res.result == "entered", res.message
    frame = (tmp_path / "frame.jpg").read_bytes()
    assert frame[:2] == b"\xff\xd8" and len(frame) > 1000          # a real JPEG of the page
    st = _json.loads((tmp_path / "status.json").read_text())
    assert st["title"] == "Win a helmet" and st["phase"] == "Finished" and st["result"].startswith("entered")
    assert isinstance(res.details["steps"], list) and type(res.details["steps"]) is list  # picklable


def test_live_view_off_writes_nothing(browser, fixture_server, profile, tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_DIR", str(tmp_path / "off"))
    browser.enter(f"{fixture_server}/comp_form.html", Ctx(profile=profile, answers=["Charquemont"]),
                  _settings(entry_mode="dry_run", live_view=False, respect_automation_clauses=False), "lv2", {})
    assert not (tmp_path / "off").exists()


def test_tile_quiz_in_a_carousel_with_retry(browser, fixture_server, profile):
    # 'Take the quiz' only scrolls; 'Start the quiz' opens a pop-up carousel of answer tiles. The entry form sits on
    # the last slide (clipped out of view until the quiz is done); wrong answers show a 'Retry' layer on top.
    res = browser.enter(f"{fixture_server}/tile_quiz.html", Ctx(profile=profile, answers=["Monte Carlo"]),
                        _settings(entry_mode="auto"), "q1", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    steps = "\n".join(res.details["steps"])
    assert "clicked 'Take the quiz'" in steps and "clicked 'Start the quiz'" in steps
    assert "\u201cMonte Carlo\u201d (the listing's answer)" in steps
    assert "clicked 'Retry'" in steps and "was wrong - trying" in steps
    assert res.details["quiz"]["answered"] == 2


def test_gated_quiz_reveals_and_follows_the_prize_draw_link(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/gated_quiz.html", Ctx(profile=profile, answers=["Whitby", "Thorpe Park"]),
                        _settings(entry_mode="dry_run"), "q2", {})
    assert res.result == "dry_run_ok", (res.message, res.details["steps"])
    steps = "\n".join(res.details["steps"])
    assert "ticked 'I've read the rules" in steps
    assert "clicked 'Reveal question 2'" in steps and "clicked 'Enter the prize draw'" in steps
    assert "gated_form" in (res.final_url or "")
    assert any(f["label"].lower().startswith("email") for f in res.details["fields"])


def test_gated_quiz_score_too_low(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/gated_quiz.html", Ctx(profile=profile), _settings(entry_mode="dry_run"),
                        "q3", {})
    assert res.result == "failed" and "Quiz score too low" in res.message, (res.message, res.details["steps"])


def test_quizzes_can_be_switched_off(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/tile_quiz.html", Ctx(profile=profile, answers=["Monte Carlo"]),
                        _settings(entry_mode="dry_run", play_quizzes=False), "q4", {})
    assert res.result == "failed" and "quiz" not in res.details


def test_match_quiz_option():
    from app.entry.browser import _match_quiz_option as m
    heights = ["2,135 meters", "2,285 meters", "2,305 meters", "2,360 meters"]
    assert m(heights, ["2,285 m"], 70) == "2,285 meters"
    assert m(heights, ["2,200 meters"], 70) is None          # never a near-miss number
    assert m(heights, ["C"], 70) == "2,305 meters"            # a letter answer
    assert m(["Scarborough", "Whitby", "Filey"], ["2015", "Whitby"], 92) == "Whitby"
    assert m(["Max Verstappen", "Lewis Hamilton"], ["Verstappen"], 70) == "Max Verstappen"


def test_paged_survey_with_input_button_next(browser, fixture_server, profile):
    # Whisky Magazine (Dotdigital survey): Next / Submit are <input type="button">, the pages are hidden containers in
    # one <form>, the last page is only tick boxes and says "please complete the following tick boxes".
    res = browser.enter(f"{fixture_server}/paged_survey.html", Ctx(profile=profile), _settings(entry_mode="auto"), "p1", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    steps = res.details["steps"]
    assert sum("clicked 'Next'" in s for s in steps) == 3 and "clicked 'Submit'" in steps[-1], steps
    acts = {f["label"]: (f["action"], f["note"]) for f in res.details["fields"]}
    assert any("hear about" in k and n == "how did you hear" for k, (a, n) in acts.items()), acts
    assert any(k.startswith("Please Specify") and n == "where you heard (other)" for k, (a, n) in acts.items()), acts
    assert any("18 years" in k and a == "check" for k, (a, n) in acts.items()), acts
    assert any("Whisky Magazine mailing list" in k and a == "uncheck" for k, (a, n) in acts.items()), acts


def test_paged_survey_dry_run_ignores_hidden_later_pages(browser, fixture_server, profile):
    # The later pages' required boxes are in the same <form> but hidden: page 1 is still complete.
    res = browser.enter(f"{fixture_server}/paged_survey.html", Ctx(profile=profile), _settings(entry_mode="dry_run"),
                        "p2", {})
    assert res.result == "dry_run_ok", res.message

# --- web-component widgets (shadow DOM) ----------------------------------------------------------------------------

def test_shadow_dom_widget_popup_form(browser, fixture_server, profile):
    # Baby Blooms (Viral Loops <form-widget>): "JOIN TO ENTER" opens a pop-up form, and the button, the form and the
    # thank-you message are all inside a shadow root that document.querySelectorAll can't see.
    res = browser.enter(f"{fixture_server}/shadow_widget.html", Ctx(profile=profile, tags=["nl"]),
                        _settings(entry_mode="auto"), "sw1", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    assert "Thanks for joining" in res.message
    steps = "\n".join(res.details["steps"])
    assert "clicked 'JOIN TO ENTER'" in steps and "clicked 'Join to Enter'" in steps
    acts = {f["value"] for f in res.details["fields"] if f.get("value")}
    assert profile["email"] in acts


def test_shadow_dom_widget_dry_run_never_opens_join_button(browser, fixture_server, profile):
    # "Join to enter" might enter you by itself, so a dry run leaves it alone (as with "Enter now").
    res = browser.enter(f"{fixture_server}/shadow_widget.html", Ctx(profile=profile), _settings(entry_mode="dry_run"),
                        "sw2", {})
    assert res.result != "entered" and not any("JOIN TO ENTER" in s for s in res.details["steps"])


def test_advert_frame_saying_forbidden_is_not_a_bot_check(browser, fixture_server, profile):
    # Rayo: a small frame saying "403 Forbidden / Access denied" made a normal page with its form count as blocked.
    res = browser.enter(f"{fixture_server}/ad_frame_forbidden.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "bc1", {})
    assert res.result == "entered", (res.result, res.message)


def test_real_access_denied_page_is_still_blocked(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/forbidden_frame.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "bc2", {})
    assert res.result == "blocked" and "bot-check" in res.message, (res.result, res.message)


def test_server_refusal_after_submit_is_reported(browser, fixture_server, profile):
    # Klaviyo forms (Arctic Fit, Pooky, Groove...) only say "An error occurred"; show what the server actually said.
    res = browser.enter(f"{fixture_server}/api_error.html", Ctx(profile=profile, tags=["nl"]), _settings(entry_mode="auto"),
                        "se1", {})
    assert res.result == "failed" and "An error occurred" in res.message, res.message
    assert "the site's server replied HTTP 501 from 127.0.0.1" in res.message and "/api/subscribe" in res.message
    assert res.details["server_errors"].startswith("HTTP 501")


def test_error_reply_is_shown_as_words_not_tokens():
    from app.entry.browser import _reply_text
    # DHL's reply: a fresh CSRF token and a pop-up whose HTML says what went wrong.
    raw = ('{"csrf":{"id":"f1-logistics-hub-2022-2026-21-BRA__token","value":"c68e6e5a8c.iSfHQMBZkl8wd5uyTzBxJn2Db7pUh-aTSJ'
           'YWx5lOpJc.1l2sCYRqyi1mPu7WJWgjExnNXfgywKv7LNV"},"errors":[],"messages":[],"modal":{"html":"<div class=\\"m\\">'
           '<h2>Sorry!</h2><p>You have already taken part today.</p><script>x()</script></div>"}}')
    assert _reply_text(raw) == "Sorry! You have already taken part today."
    assert _reply_text('{"captcha_required": true}') == "captcha_required"
    assert _reply_text("<html><title>Attention Required! | Cloudflare</title></html>") == "Attention Required! | Cloudflare"
    assert _reply_text("Bad request") == "Bad request"


def test_wayin_link_submit_and_country_field(browser, fixture_server, profile):
    # CinemaBlend (Future plc / Wayin): Submit is <a class="xCTA xButton">, and "Country" got the quiz answer
    # because it starts with "count".
    res = browser.enter(f"{fixture_server}/wayin_form.html", Ctx(profile=profile, answers=["Festive Road"]),
                        _settings(entry_mode="auto"), "wy1", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    acts = {f["label"]: (f["action"], f["source"]) for f in res.details["fields"]}
    assert acts["Country"] == ("fill", "country"), acts


def test_promo_teasers_after_submit_are_not_a_follow_up_quiz(browser, fixture_server, profile):
    # Yours magazine: after submitting, a "Want to hear about the latest competitions...? Click here" box and cards for
    # other comps were played as a quiz, and compbot clicked through to another competition. County got the answer too.
    res = browser.enter(f"{fixture_server}/promo_after_submit.html", Ctx(profile=profile, answers=["53"]),
                        _settings(entry_mode="auto"), "pa1", {})
    steps = "\n".join(res.details["steps"])
    assert "questions to answer" not in steps and "quiz" not in steps, steps
    assert "other.html" not in (res.final_url or "")
    acts = {f["label"]: (f["action"], f["source"]) for f in res.details["fields"]}
    assert acts["County"] == ("fill", "county"), acts


# --------------------------------------------------------------------------- 4 Nov failure analysis


def test_confirmations_that_were_missed(browser, fixture_server, profile):
    # Muddy Stilettos ("Huzzah, you have now entered!") and Leeds List ("Your form has been successfully received")
    # were read as "nothing changed" and entered again on the next run - up to four more times.
    for page in ("now_entered.html", "form_received.html"):
        res = browser.enter(f"{fixture_server}/{page}", Ctx(profile=profile), _settings(entry_mode="auto"), "fa1", {})
        assert res.result == "entered", (page, res.message)
        assert sum("clicked" in s for s in res.details["steps"]) == 1, res.details["steps"]


def test_server_saying_already_entered_counts_as_entered(browser, fixture_server, profile):
    # Welcome to Fife: the page showed nothing, but the server replied HTTP 409 "already been used to enter".
    res = browser.enter(f"{fixture_server}/already_used.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "fa2", {})
    assert res.result == "entered" and "already" in res.message, res.message


def test_bot_checks_turning_the_entry_down_go_to_you(browser, fixture_server, profile):
    # Visit Northumberland: "Unable to verify reCaptcha" (server); Klaviyo: "An error occurred when submitting".
    res = browser.enter(f"{fixture_server}/recaptcha_refused.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "fa3", {})
    assert res.result == "captcha" and "Unable to verify reCaptcha" in res.message, res.message
    res = browser.enter(f"{fixture_server}/klaviyo_refused.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "fa4", {})
    assert res.result == "blocked" and "Klaviyo" in res.message, res.message
    res = browser.enter(f"{fixture_server}/imperva_refused.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "fa4b", {})  # uDiscover Music: "Oops an error occurred" behind Imperva
    assert res.result == "blocked" and "bot protection" in res.message, res.message


def test_button_held_by_a_bot_check_is_a_captcha(browser, fixture_server, profile, monkeypatch):
    # SheerLuxe / SLMan: a hidden Turnstile keeps the button on "Solving captcha..." for ever.
    from app.entry import browser as engine
    monkeypatch.setattr(engine, "CAPTCHA_BUTTON_WAIT_S", 1)
    res = browser.enter(f"{fixture_server}/turnstile_stuck.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "fa5", {})
    assert res.result == "captcha" and "Solving captcha" in res.message, res.message
    res = browser.enter(f"{fixture_server}/turnstile_stuck.html", Ctx(profile=profile), _settings(entry_mode="dry_run"),
                        "fa6", {})
    assert res.result == "captcha", res.message


def test_waits_while_the_button_says_sending(browser, fixture_server, profile):
    # TUI: "Sending..." for a good while before the thank-you.
    res = browser.enter(f"{fixture_server}/busy_then_done.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "fa7", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    assert any("busy" in s for s in res.details["steps"])


def test_age_gate_is_answered(browser, fixture_server, profile):
    # Brains Brewery: "Are you over 18?" sat on top of the form.
    res = browser.enter(f"{fixture_server}/age_gate.html", Ctx(profile=profile), _settings(entry_mode="auto"), "fa8", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    assert any("answered the age check" in s for s in res.details["steps"])
    child = dict(profile, dob="2015-01-01")  # never for someone under 18
    res = browser.enter(f"{fixture_server}/age_gate.html", Ctx(profile=child), _settings(entry_mode="auto"), "fa9", {})
    assert not any("age check" in s for s in res.details["steps"]) and res.result != "entered"


def test_follows_the_here_link_instead_of_signing_up_to_the_newsletter(browser, fixture_server, profile):
    # Velvet / Sea Angler: "click HERE and complete the online form"; Sea Angler's only form was its newsletter.
    ctx = Ctx(profile=profile, answers=["UNIVERSE"])
    res = browser.enter(f"{fixture_server}/here_link.html", ctx, _settings(entry_mode="auto"), "fl1", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    assert any("followed 'HERE'" in s for s in res.details["steps"]) and "newsletter_done" not in (res.final_url or "")
    res = browser.enter(f"{fixture_server}/here_link.html", Ctx(profile=profile, answers=["UNIVERSE"]),
                        _settings(entry_mode="dry_run"), "fl2", {})
    assert res.result == "dry_run_ok" and (res.final_url or "").endswith("here_target.html"), res.message
    assert "would press \u201cEnter\u201d" in res.message


def test_follows_a_tap_to_enter_link(browser, fixture_server, profile):
    # Vegan Food & Living: "TAP TO ENTER".
    res = browser.enter(f"{fixture_server}/tap_link.html", Ctx(profile=profile, answers=["UNIVERSE"]),
                        _settings(entry_mode="dry_run"), "fl3", {})
    assert res.result == "dry_run_ok", (res.message, res.details["steps"])
    assert "clicked 'TAP TO ENTER'" in res.details["steps"]


def test_google_forms_style_choices_and_div_submit(browser, fixture_server, profile):
    # eVision / Diggerland (Google Forms): a required "Would you like ... news via email?" Yes/No made of
    # <div role=checkbox> was left blank, so the form never went. Answered "No" (not a newsletter comp).
    res = browser.enter(f"{fixture_server}/aria_choices.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "ag1", {})
    assert res.result == "entered", (res.message, res.details)
    acts = {f["label"][:30]: (f["action"], f["note"]) for f in res.details["fields"]}
    assert ("radio", "yes/no") in acts.values(), acts


def test_cookie_gated_form_is_found_on_a_second_try(browser, fixture_server, profile):
    # Some shops only load their competition embed once cookies are accepted.
    res = browser.enter(f"{fixture_server}/cookie_gate.html", Ctx(profile=profile), _settings(entry_mode="dry_run"),
                        "cg1", {})
    assert res.result == "dry_run_ok", (res.message, res.details)
    assert "Reject all" in res.details["cookies"] and "accepted: Accept all" in res.details["cookies"]
    assert "the entry form appeared once cookies were accepted" in res.details["steps"]


def test_sign_in_walls_and_widgets_go_to_you_first_time(browser, fixture_server, profile):
    # Living North / The Lady / Delia ("You need to sign in to enter") and MoneyMagpie's Gleam widget were reported as
    # "No entry form found" and retried.
    res = browser.enter(f"{fixture_server}/login_wall.html", Ctx(profile=profile), _settings(entry_mode="auto"), "lw1", {})
    assert res.result == "login" and "You need to sign in to enter this competition" in res.message, res.message
    res = browser.enter(f"{fixture_server}/gleam_widget.html", Ctx(profile=profile), _settings(entry_mode="auto"), "lw2", {})
    assert res.result == "widget" and "Gleam" in res.message, res.message


def test_div_submit_button(browser, fixture_server, profile):
    # Doctor Who Magazine: SUBMIT is a <div onclick> (and a hidden "Search" one sits in the form too).
    res = browser.enter(f"{fixture_server}/div_submit.html", Ctx(profile=profile), _settings(entry_mode="auto"), "ds1", {})
    assert res.result == "entered", (res.message, res.details["steps"])


def test_formless_page_presses_the_entry_button_not_the_footer_sign_up(browser, fixture_server, profile):
    # Great British Chefs: no <form>; compbot also filled the footer newsletter box and found no button.
    res = browser.enter(f"{fixture_server}/formless.html", Ctx(profile=profile), _settings(entry_mode="auto"), "ds2", {})
    assert res.result == "entered", (res.message, res.details)
    assert not any("Newsletter" in f["label"] or f["label"] == "Search" for f in res.details["fields"])


def test_form_inside_a_scrolling_card(browser, fixture_server, profile):
    # Slimming World (Microsoft Forms): the questions are in a card you scroll, below its fold.
    res = browser.enter(f"{fixture_server}/scroll_panel.html", Ctx(profile=profile), _settings(entry_mode="auto"), "sp1", {})
    assert res.result == "entered", (res.message, res.details)

def test_entry_form_asking_your_age_is_not_mistaken_for_an_age_gate(browser, fixture_server, profile):
    # Its "Enter" button sends the entry: pressing it as an age gate's "yes" would submit, even in a dry run.
    res = browser.enter(f"{fixture_server}/modal_age_question.html", Ctx(profile=profile), _settings(entry_mode="dry_run"),
                        "ag2", {})
    assert res.result == "dry_run_ok", (res.result, res.message, res.details["steps"])
    assert not any("age check" in s for s in res.details["steps"])


def test_off_screen_carousel_slides_stay_hidden(browser):
    # Scrolling a panel down is reaching it; a sideways-scrolling carousel's other slides are still out of view.
    from app.entry.browser import JS_HELPERS
    page = browser.browser.new_page(viewport={"width": 800, "height": 600})
    try:
        page.set_content("""
          <div style="width:300px;overflow:hidden"><div id="track" style="display:flex;overflow-x:auto;width:300px">
            <button id="s1" style="flex:0 0 300px">Slide 1</button><button id="s2" style="flex:0 0 300px">Slide 2</button>
          </div></div>
          <div style="height:120px;overflow:hidden"><div style="height:120px;overflow-y:auto">
            <p style="height:300px">Long text</p><button id="below">Below the fold</button></div></div>""")
        vis = page.evaluate("() => {" + JS_HELPERS + " return ['s1', 's2', 'below'].map(id => __cbVis(document.getElementById(id))); }")
        assert vis == [True, False, True], vis
    finally:
        page.close()
def test_newsletter_already_on_the_page_is_not_step_two(browser, fixture_server, profile):
    # Ecohome: after the entry was sent, the sidebar's newsletter sign-up was filled in and sent as "step 2" (and its
    # "already subscribed" reply was then read as the entry being confirmed).
    res = browser.enter(f"{fixture_server}/nl_sidebar.html", Ctx(profile=profile), _settings(entry_mode="auto"), "nl1", {})
    assert res.result == "unverified", (res.result, res.message, res.details["steps"])
    assert "step 2: skipped a form that was already on the page" in res.details["steps"]
    assert not any("SIGN UP" in s for s in res.details["steps"])
    assert not any(f["label"].startswith("Your") for f in res.details["fields"])


def test_newsletter_pop_up_after_the_entry_is_not_step_two(browser, fixture_server, profile):
    # Dengie: a newsletter pop-up that came up once the entry was sent was signed up to as "step 2".
    res = browser.enter(f"{fixture_server}/nl_popup.html", Ctx(profile=profile), _settings(entry_mode="auto"), "nl2", {})
    assert res.result == "unverified", (res.result, res.message, res.details["steps"])
    assert "step 2: skipped a newsletter sign-up" in res.details["steps"]
    assert not any("Sign up" in s for s in res.details["steps"])


def test_form_asking_for_a_password_needs_an_account(browser, fixture_server, profile):
    # Yeo Valley's Yeokens: step 2 asks you to choose a password.
    res = browser.enter(f"{fixture_server}/account_step.html", Ctx(profile=profile), _settings(entry_mode="auto"), "ac1", {})
    assert res.result == "login" and "password" in res.message and "step 2" in res.message, res.message


def test_security_code_refusal_is_a_captcha(browser, fixture_server, profile):
    # The Times (Formstack): "ERROR: The security code does not match." is its hidden reCAPTCHA saying no.
    res = browser.enter(f"{fixture_server}/security_code.html", Ctx(profile=profile), _settings(entry_mode="auto"), "sc1", {})
    assert res.result == "captcha" and "security code does not match" in res.message, res.message


def test_winners_page_link_goes_to_you(browser, fixture_server, profile):
    # SheerLuxe: the listing linked to /competition-winners; reported as "No entry form found" and retried.
    res = browser.enter(f"{fixture_server}/competition-winners.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "wp1", {})
    assert res.result == "wrongpage" and "winners page" in res.message, res.message


def test_quiz_options_labelled_beside_their_radios(browser, fixture_server, profile):
    # Doctor Who Magazine: the question was taken from the first option ("A) Ashley Stafford") and never answered.
    ctx = Ctx(profile=profile, answers=["Ashley Carter"])
    res = browser.enter(f"{fixture_server}/table_radios.html", ctx, _settings(entry_mode="auto"), "tr1", {})
    assert res.result == "entered", (res.message, res.details["fields"])
    q = next(f for f in res.details["fields"] if f["source"] == "answer")
    assert q["label"].startswith("What character did Natalie Gumede play"), q


def test_date_of_birth_set_through_its_date_picker(browser, fixture_server, profile):
    # Brains: a read-only flatpickr box was skipped, so the site said "Date of birth is required".
    res = browser.enter(f"{fixture_server}/date_picker.html", Ctx(profile=profile), _settings(entry_mode="auto"), "dp1", {})
    assert res.result == "entered", (res.message, res.details["fields"])
    assert any(f["source"] == "dob" and f["ok"] for f in res.details["fields"])


def test_spin_the_wheel_is_the_entry_button(browser, fixture_server, profile):
    # Free Spin Daily: no submit button - you click the wheel.
    res = browser.enter(f"{fixture_server}/spin_wheel.html", Ctx(profile=profile), _settings(entry_mode="dry_run"), "sw1", {})
    assert res.result == "dry_run_ok" and "the prize wheel" in res.message, res.message
    res = browser.enter(f"{fixture_server}/spin_wheel.html", Ctx(profile=profile), _settings(entry_mode="auto"), "sw2", {})
    assert res.result == "entered" and "won five entries" in res.message, (res.message, res.details["steps"])


def test_web_component_fields_and_redrawn_tick_boxes(browser, fixture_server, profile):
    # Roborock: email box and tick boxes inside shadow roots; the terms box redraws itself and has links inside it.
    res = browser.enter(f"{fixture_server}/web_components.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "wc1", {})
    assert res.result == "entered" and "sharing your data" not in res.message, (res.message, res.details)
    acts = {f["label"][:20]: f["action"] for f in res.details["fields"]}
    assert acts["I agree to the Priva"] == "check" and acts["I consent to the tra"] == "uncheck", acts


def test_pop_up_frame_over_the_button_is_moved_aside(browser, fixture_server, profile):
    # Warren James: a full-screen sign-up <iframe> covered "enter now", whose own click handler sends the entry.
    res = browser.enter(f"{fixture_server}/iframe_overlay.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "io1", {})
    assert res.result == "entered", (res.result, res.message, res.details["steps"])


def test_site_consent_comes_before_the_embeds_own_button(browser, fixture_server, profile):
    # Gardener's World: pressing the Riddle embed's "Accept and continue" while the site's consent dialog was open
    # lost the consent, so the entry form never loaded ("No entry form found").
    res = browser.enter(f"{fixture_server}/embed_placeholder.html", Ctx(profile=profile), _settings(entry_mode="dry_run"),
                        "ep1", {})
    assert res.result == "dry_run_ok", (res.result, res.message, res.details)
    assert res.details["cookies"] == ["Accept All & Continue"], res.details["cookies"]


def test_next_question_that_comes_up_by_itself(browser, fixture_server, profile):
    # Great British Life's Typeform: picking the answer brings up the next question; there's no button until the end.
    ctx = Ctx(profile=profile, answers=["Foregate Street"])
    res = browser.enter(f"{fixture_server}/one_at_a_time.html", ctx, _settings(entry_mode="auto"), "oa1", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    assert "step 1: the next question came up by itself" in res.details["steps"], res.details["steps"]
    assert [f["source"] for f in res.details["fields"]] == ["answer", "full_name", "email"], res.details["fields"]


def test_boxes_named_only_by_placeholders_and_first_choices(browser, fixture_server, profile):
    # Johansens: the Title list's options were read as First name's label, and day 1 was guessed for the Day list.
    res = browser.enter(f"{fixture_server}/list_labels.html", Ctx(profile=profile), _settings(entry_mode="auto"), "ll1", {})
    assert res.result == "entered", (res.message, res.details["fields"])
    by_source = {f["source"]: f for f in res.details["fields"]}
    assert {"title", "first_name", "last_name", "dob_day", "dob_month", "dob_year", "email"} <= set(by_source)
    assert not any("Mr" in f["label"] and "Mrs" in f["label"] for f in res.details["fields"]), res.details["fields"]

def test_members_only_pages_are_sign_in_walls():
    from app.entry.browser import LOGIN_WALL_RE

    assert LOGIN_WALL_RE.search("This is for McLaren Racing Club members only. Join today and unlock:")
    assert LOGIN_WALL_RE.search("Members-only competitions") and LOGIN_WALL_RE.search(
        "This competition is only open to registered members.")
    assert not LOGIN_WALL_RE.search("Our members only get the best offers")
    assert not LOGIN_WALL_RE.search("Competition open to UK residents only")

def test_page_for_registered_users_only_is_a_sign_in_wall(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/registered_only.html", Ctx(profile=profile), _settings(entry_mode="dry_run"),
                        "ro1", {})
    assert res.result == "login" and "open to registered users" in res.message, res.message

def test_contact_form_7_labels_and_acceptance_box(browser, fixture_server, profile):
    # Love It!: no <label>s (the words sit in the <p> beside each box's wrapper), and the button waits for the
    # acceptance box. compbot left every box unlabelled, skipped the acceptance box and found the button disabled.
    from datetime import date

    p = dict(profile, dob=f"{date.today().year - 46}-01-01")  # aged 46: the 41-50 group
    res = browser.enter(f"{fixture_server}/cf7_form.html", Ctx(profile=p, answers=["G5"]), _settings(entry_mode="auto"),
                        "cf1", {})
    assert res.result == "entered", (res.message, res.details["fields"])
    labels = {f["label"]: f["source"] for f in res.details["fields"]}
    assert labels.get("Your Name") == "full_name" and labels.get("Your age") == "age"
    assert any(k.startswith("Please tick this checkbox") and v == "rule" for k, v in labels.items())

def test_gleam_giveaway_on_a_blog_is_not_entered_through_its_comment_box(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/gleam_comment.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "gc1", {})
    assert res.result == "widget" and "gleam.io/AnU0E" in res.message, res.message

def test_http2_refusal_is_retried_over_http1(monkeypatch):
    # Waitrose: net::ERR_HTTP2_PROTOCOL_ERROR, and over HTTP/1.1 the page then never loads: the site blocks bots.
    from app.entry import browser as br
    monkeypatch.setattr(br, "_WORKER_MODULE", "tests.fixtures.http2_worker")
    res = br.enter_isolated("https://example.com/win", Ctx(profile={}), _settings(), "h2a", {}, timeout=60)
    assert res.result == "blocked" and "HTTP/2" in res.message, res.message
    assert res.details["steps"][0].startswith("the site's HTTP/2 connection failed")
    monkeypatch.setenv("HTTP1_LOADS", "1")
    res = br.enter_isolated("https://example.com/win", Ctx(profile={}), _settings(), "h2b", {}, timeout=60)
    assert res.result == "dry_run_ok" and res.details["steps"] == [
        "the site's HTTP/2 connection failed, so compbot used HTTP/1.1", "filled"]

def test_empty_form_whose_button_starts_the_competition(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/launch_form.html", Ctx(profile=profile), _settings(entry_mode="dry_run"),
                        "lf1", {})
    assert res.result == "dry_run_ok" and "clicked 'Click here'" in res.details["steps"], (res.message, res.details)
    res = browser.enter(f"{fixture_server}/launch_form.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "lf2", {})
    assert res.result == "entered", (res.message, res.details["steps"])

def test_start_button_in_a_block_beside_the_form_is_found(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/leadfamly_form.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "lff", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    assert res.details["steps"] == ["step 1: clicked 'Start game'"], res.details["steps"]

def test_quiz_further_down_is_played_instead_of_signing_up_to_the_newsletter(browser, fixture_server, profile):
    # Radio Times: a newsletter box on screen, the competition a Riddle quiz further down whose form follows "CHOOSE".
    res = browser.enter(f"{fixture_server}/newsletter_then_quiz.html", Ctx(profile=profile, answers=["McFly"]),
                        _settings(entry_mode="auto"), "nq1", {})
    assert res.result == "entered" and "Thanks for enter" in res.message, (res.message, res.details["steps"])
    steps = res.details["steps"]
    assert "quiz: clicked 'CHOOSE'" in steps and "the entry form appeared after the quiz" in steps, steps

def test_aws_waf_puzzle_page_is_a_bot_check(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/aws_waf.html", Ctx(profile=profile), _settings(entry_mode="auto"), "aw1", {})
    assert res.result == "blocked" and "bot-check" in res.message, (res.result, res.message)

def test_button_with_its_small_print_and_a_sign_in_pop_up(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/small_print_button.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "sp1", {})
    assert res.result == "login" and "APPEAR TO BE SIGNED IN" in res.message, (res.result, res.message)
    assert "clicked 'ENTER FOR FREE'" in res.details["steps"], res.details["steps"]

def test_link_with_a_footnote_mark_is_followed(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/footnote_link.html", Ctx(profile=profile, answers=["UNIVERSE"]),
                        _settings(entry_mode="auto"), "fn1", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    assert "clicked 'enter to win'" in res.details["steps"], res.details["steps"]

def test_next_is_found_below_radio_buttons_drawn_by_the_site(browser, fixture_server, profile):
    # Labels with display: contents round hidden radio buttons draw nothing themselves: their words are what shows.
    res = browser.enter(f"{fixture_server}/contents_radios.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "cr1", {})
    assert res.result == "entered", (res.message, res.details["steps"], res.details.get("fields"))
    assert res.details["steps"] == ["step 1: clicked 'Next'"], res.details["steps"]

def test_next_is_found_when_the_answered_question_folds_away(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/folded_choice.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "fc1", {})
    assert res.result == "entered", (res.message, res.details["steps"], res.details.get("fields"))
    assert res.details["steps"] == ["step 1: clicked 'Next'"], res.details["steps"]

def test_search_box_with_its_choices_listed_after_it(browser, fixture_server, profile):
    # Outgrow (Gilbert): the choices only appear once the box is clicked; any day will do.
    res = browser.enter(f"{fixture_server}/outgrow_dropdown.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "og1", {})
    assert res.result == "entered", (res.message, res.details["steps"], res.details.get("fields"))
    by = {f["label"].rstrip("*"): f for f in res.details["fields"]}
    assert by["Day of your choice"]["action"] == "select" and by["Day of your choice"]["ok"], by

def test_see_through_real_button_over_a_styled_one_is_pressed(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/twin_submit.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "tw1", {})
    assert res.result == "entered", (res.message, res.details["steps"])
    assert res.details["steps"] == ["step 1: clicked 'Send'"], res.details["steps"]

def test_radix_tick_box_is_ticked_by_its_button(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/radix_checkbox.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "rx1", {})
    assert res.result == "entered", (res.message, res.details["steps"], res.details.get("fields"))
    terms = next(f for f in res.details["fields"] if "Terms and Conditions" in f["label"])
    assert terms["action"] == "check" and terms["ok"], terms

def test_bonus_entries_offer_after_sending_means_the_entry_went_in(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/bonus_survey.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "bs1", {})
    assert res.result == "entered" and "bonus entries" in res.message, (res.result, res.message, res.details["steps"])
    assert res.details["steps"] == ["step 1: clicked 'ENTER NOW'"], res.details["steps"]

def test_entering_that_opens_a_sign_in_page_needs_an_account_not_a_phone(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/enter_free_login.html", Ctx(profile=dict(profile, phone="")),
                        _settings(entry_mode="auto"), "efl", {})
    assert res.result == "login", (res.result, res.message, res.details["steps"])
    assert "clicked 'Enter For Free'" in res.details["steps"]

def test_required_after_sending_and_no_phone_in_your_profile_needs_you(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/mailchimp_phone.html", Ctx(profile=dict(profile, phone="")),
                        _settings(entry_mode="auto"), "mcp", {})
    assert res.result == "needs_input" and "profile has no phone" in res.message, (res.result, res.message)

def test_drawn_drop_down_lists_are_chosen_from(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/aria_dropdown.html", Ctx(profile=profile, answers=["6"]),
                        _settings(entry_mode="auto"), "ad1", {})
    assert res.result == "entered", (res.message, res.details["fields"])
    by = {f["label"]: f for f in res.details["fields"]}
    assert by["Title"]["source"] == "title" and by["Title"]["ok"]
    assert by["How many ships are currently in the fleet?"]["source"] == "answer"

def test_jotform_terms_widget_in_its_own_frame_is_ticked(browser, fixture_server, profile, monkeypatch):
    import re

    from app.entry import browser as br
    monkeypatch.setattr(br, "JOTFORM_WIDGET_RE", re.compile(r"^http://127\.0\.0\.1:\d+/form-widgets/"))
    res = browser.enter(f"{fixture_server}/jotform_widget.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "jw1", {})
    assert res.result == "entered", (res.message, res.details["fields"])
    widget = [f for f in res.details["fields"] if "(form widget)" in f["note"]]
    assert [(f["action"], f["ok"]) for f in widget] == [("check", True), ("uncheck", True)], widget

def test_typed_in_lists_are_chosen_from(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/combobox_select.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "cb1", {})
    assert res.result == "entered", (res.message, res.details["fields"])
    by = {f["label"]: f for f in res.details["fields"]}
    assert by["Country of residence"]["source"] == "country" and by["Age"]["source"] == "age"
    assert "Find your address (optional)" not in by or by["Find your address (optional)"]["action"] != "select"

def test_pages_that_say_you_enter_elsewhere():
    from app.entry.browser import SOCIAL_ENTRY_RE

    for said in ["Comment on Facebook Post to Enter", "Download the app now!", "Find a poster, scan the QR code",
                 "leave a comment on this Instagram post", "Enter via our Instagram page"]:
        assert SOCIAL_ENTRY_RE.search(said), said
    for said in ["Follow us on Facebook for more competitions", "Download our app for exclusive offers",
                 "Share this page with friends", "Get the app on Google Play"]:
        assert not SOCIAL_ENTRY_RE.search(said), said

def test_second_page_while_the_first_stays_hidden(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/paged_hidden.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "ph1", {})
    assert res.result == "entered", (res.message, res.details["steps"], res.details["fields"])

def test_tick_box_drawn_as_a_toggle_beside_a_hidden_input(browser, fixture_server, profile):
    res = browser.enter(f"{fixture_server}/toggle_checkbox.html", Ctx(profile=profile), _settings(entry_mode="auto"),
                        "tg1", {})
    assert res.result == "entered", (res.message, res.details["fields"])
    acts = [(f["label"], f["action"], f["ok"]) for f in res.details["fields"] if f["action"] in ("check", "uncheck")]
    assert [(lb.split()[1], a, ok) for lb, a, ok in acts] == [("confirm", "check", True), ("am", "uncheck", True)], acts

def test_page_instructions_and_non_dates_are_not_answers():
    from app.entry.browser import _not_an_answer
    from app.entry.forms import Field

    f = Field("d", "input", "text", label="When does your Motorbike Insurance renew?*")
    q = f.label
    assert _not_an_answer("If you're not sure on the exact date, you can use the first of the month.", f, q)
    assert _not_an_answer("Sometime next year", f, q)
    assert _not_an_answer("01/03/2027", f, q) is None and _not_an_answer("March 2027", f, q) is None
    k = Field("k", "input", "text", label="What is the name of the hotel?")
    assert _not_an_answer("Please enter your answer", k, k.label) and _not_an_answer("The Ritz", k, k.label) is None