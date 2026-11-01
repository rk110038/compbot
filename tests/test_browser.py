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
    assert "\u201cMonte Carlo\u201d (the MSE post)" in steps
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