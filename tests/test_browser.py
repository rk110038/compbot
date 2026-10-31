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
