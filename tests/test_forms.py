from app.entry.forms import Ctx, Field, find_automation_clause, map_field


def ctx(profile, **kw):
    return Ctx(profile=profile, **kw)


def test_profile_fields(profile):
    c = ctx(profile)
    assert map_field(Field("1", "input", "text", name="fname", label="First name *", required=True), c).value == "Test"
    assert map_field(Field("2", "input", "text", label="Surname", required=True), c).value == "Person"
    assert map_field(Field("3", "input", "email", label="Email"), c).value == "test.person@example.com"
    assert map_field(Field("4", "input", "text", label="Confirm your email"), c).source == "email_confirm"
    assert map_field(Field("5", "input", "text", label="Postcode"), c).value == "RG1 1AA"
    assert map_field(Field("6", "input", "text", placeholder="DD/MM/YYYY", label="Date of birth"), c).value == "05/03/1980"
    assert map_field(Field("7", "input", "date", label="Date of birth"), c).value == "1980-03-05"
    assert map_field(Field("8", "input", "text", label="Full name"), c).value == "Test Person"
    assert map_field(Field("9", "input", "text", autocomplete="address-line1", label="Street"), c).value == "1 Test Street"


def test_question_field_uses_answer_not_profile(profile):
    c = ctx(profile, answers=["John Cooper Clarke"])
    a = map_field(Field("1", "input", "text", label="Name one artist on our Tickets Directory", required=True), c)
    assert a.kind == "fill" and a.value == "John Cooper Clarke"


def test_unknown_required_and_honeypot(profile):
    c = ctx(profile)
    assert map_field(Field("1", "input", "text", label="Why should you win? (tie-breaker)", required=True), c).kind == "unknown"
    assert map_field(Field("2", "input", "text", name="website", label="Website"), c).kind == "skip"


def test_how_did_you_hear_other_then_please_specify(profile):
    c = ctx(profile)
    hear = Field("1", "radio", "radio", label="How did you hear about this competition?", required=True,
                 options=[{"text": t, "value": str(i), "idx": f"h{i}"} for i, t in
                          enumerate(["Social Media", "Google", "Word of Mouth", "Email", "Other"])])
    a = map_field(hear, c)
    assert a.value == "h4" and c.heard_other
    spec = map_field(Field("2", "input", "text", label="Please Specify"), c)
    assert spec.kind == "fill" and spec.value == "MoneySavingExpert forum"
    # Without an "Other" pick, a lone "please specify" box isn't guessed at; a tie-breaker never gets it.
    assert map_field(Field("3", "input", "text", label="Please specify"), ctx(profile)).kind == "skip"
    assert map_field(Field("4", "input", "text", label="Tell us why you'd love to win", required=True), c).kind == "unknown"
    assert map_field(Field("3", "input", "text", label="Company name"), c).kind == "skip"


def test_checkboxes(profile):
    c = ctx(profile)
    terms = Field("1", "input", "checkbox", label="I agree to the terms and conditions", required=True)
    news = Field("2", "input", "checkbox", label="Keep me up to date with news and offers by email")
    third = Field("3", "input", "checkbox", label="Share my details with carefully selected partners")
    optout = Field("4", "input", "checkbox", label="Tick here if you do not wish to receive marketing")
    assert map_field(terms, c).kind == "check"
    assert map_field(news, c).kind == "uncheck"
    assert map_field(third, c).kind == "uncheck"
    assert map_field(optout, c).kind == "check"
    nl = ctx(profile, tags=["nl"])
    assert map_field(news, nl).kind == "check"
    assert map_field(third, nl).kind == "uncheck"


def test_i_agree_to_marketing_is_not_terms(profile):
    # "I agree" in front of a marketing consent doesn't make it the T&Cs box (DHL's F1 quiz form).
    c = ctx(profile)
    mkt = Field("1", "input", "checkbox", label="I agree that my contact details may be used for future "
                                                "communication from DHL via email.")
    assert map_field(mkt, c).kind == "uncheck"
    both = Field("2", "input", "checkbox", label="I have read and accepted the terms and conditions and agree to "
                                                 "future communication by email", required=True)
    assert map_field(both, c).kind == "check"


def test_radio_and_select_matching(profile):
    c = ctx(profile, answers=["39G"])
    radio = Field("r", "radio", "radio", label="How much protein per serving?", required=True,
                  options=[{"value": "a", "text": "20g", "idx": "o1"}, {"value": "b", "text": "39g", "idx": "o2"}])
    assert map_field(radio, c).value == "o2"
    month = Field("m", "select", "select-one", label="Date of birth month", aria="Date of birth month",
                  options=[{"value": "", "text": "Month"}] + [{"value": str(i), "text": n} for i, n in
                                                             enumerate(["January", "February", "March"], 1)])
    assert map_field(month, c).value == "3"
    country = Field("c", "select", "select-one", label="Country", options=[
        {"value": "", "text": "Please select"}, {"value": "IE", "text": "Ireland"}, {"value": "GB", "text": "United Kingdom"}])
    assert map_field(country, c).value == "GB"
    hear = Field("h", "select", "select-one", label="How did you hear about us?", required=True, options=[
        {"value": "", "text": "Please select"}, {"value": "fb", "text": "Facebook"}, {"value": "o", "text": "Other"}])
    assert map_field(hear, c).value == "o"


def test_automation_clause():
    assert find_automation_clause(
        "3.7 Entries made online using methods generated by a script, macro or the use of automated devices will be void.")
    assert find_automation_clause(
        "Competition entries that are automatically generated by computer, completed by third parties or in bulk will not be accepted.")
    assert not find_automation_clause(
        "The Promoter accepts no responsibility for entries lost due to software or hardware failure.")
    assert not find_automation_clause("Bulk entries made from trade, consumer groups or third parties will not be accepted.")
    assert not find_automation_clause(
        "all entries received after the Competition Period shall be automatically disqualified.")
    assert find_automation_clause("Entries submitted via automated or bulk entry services will be deemed invalid.")


# --- regressions from the first overnight run (1 Nov 2026) ---------------------------------------------------------

def test_first_name_label_beats_generic_autocomplete(profile):
    # Pan Macmillan: "First name" box marked autocomplete="name" got the full name and failed validation.
    f = Field("1", "input", "text", name="firstName", label="First name", autocomplete="name", required=True)
    assert map_field(f, ctx(profile)).value == "Test"


def test_split_first_last_labels(profile):
    # Gravity Forms name field: sub-labels are just "First" and "Last".
    c = ctx(profile)
    assert map_field(Field("1", "input", "text", name="input_1.3", label="First"), c).value == "Test"
    assert map_field(Field("2", "input", "text", name="input_1.6", label="Last"), c).value == "Person"


def test_city_label_beats_address_field_name(profile):
    # foodandtravel: "City" box named address[city] was filled with address line 1.
    c = ctx(profile)
    assert map_field(Field("1", "input", "text", name="address_city", label="City"), c).value == "Testville"
    assert map_field(Field("2", "input", "text", name="address_state", label="State/Province/Region"), c).value == "Berkshire"
    country = Field("3", "select", "select-one", name="address_country", label="Country", required=True, options=[
        {"value": "", "text": ""}, {"value": "Ireland", "text": "Ireland"}, {"value": "United Kingdom", "text": "United Kingdom"}])
    assert map_field(country, ctx(profile, answers=["Paris"], question="Where?")).value == "United Kingdom"


def test_tooltip_question_mark_on_personal_field(profile):
    # Cadbury: "EMAIL ADDRESS * ?" (a help icon) was treated as the quiz question and given the answer.
    c = ctx(profile, answers=["Tuesday"], question="What day is day 1 of advent?")
    assert map_field(Field("1", "input", "text", name="email", label="EMAIL ADDRESS * ?"), c).value == "test.person@example.com"
    assert map_field(Field("2", "input", "text", name="phone", label="PHONE NUMBER * ?"), c).value == "07700900123"
    q = Field("3", "input", "text", name="q", label="WHAT DAY OF THE WEEK IS DAY 1 OF ADVENT THIS YEAR (2026) * ?")
    assert map_field(q, c).value == "Tuesday"


def test_other_persons_details_not_filled_with_profile(profile):
    a = map_field(Field("1", "input", "text", label="Running partners name", required=True), ctx(profile))
    assert a.kind == "unknown"
    assert map_field(Field("2", "input", "text", label="Your friend's email"), ctx(profile)).kind == "skip"


def test_maths_check_is_treated_as_captcha(profile):
    a = map_field(Field("1", "input", "text", label="3 + 2 equals *", required=True), ctx(profile))
    assert a.kind == "unknown" and a.note.startswith("captcha")


def test_circumvent_limit_clause_is_not_an_automation_ban():
    # Muddy Stilettos: about dodging one-entry-per-household, not a ban on a single automated entry.
    assert not find_automation_clause(
        "If it becomes apparent that either a participant or household is using any means to circumvent this "
        "condition such as, and without limitation, multiple email accounts, multiple reproduced or "
        "computer-generated entries, using multiple identities, fraudulently filling in forms, they will be disqualified.")
    assert find_automation_clause("4.8 Automated Entry : The use of any automated entry software or any other "
                                  "mechanical or electronic means that permits any person to enter any Competition "
                                  "repeatedly is prohibited.")


def test_yes_option_wording_decides_consent_radio(profile):
    # Northern Life: "Your Data" radio - Yes option reads "Yes - I agree to the use of my data as outlined above".
    f = Field("r", "radio", "radio", label="Your Data", required=True, options=[
        {"value": "yes", "text": "Yes - I agree to the use of my data as outlined above", "idx": "y"},
        {"value": "no", "text": "No - I do not agree", "idx": "n"}])
    assert map_field(f, ctx(profile)).value == "y"
    mkt = Field("m", "radio", "radio", label="Stay in touch?", options=[
        {"value": "1", "text": "Yes, send me offers and news", "idx": "y"}, {"value": "0", "text": "No thanks", "idx": "n"}])
    assert map_field(mkt, ctx(profile)).value == "n"
    assert map_field(mkt, ctx(profile, tags=["nl"])).value == "y"


def test_contact_channel_checkboxes(profile):
    email = Field("1", "input", "checkbox", name="consent", label="Email", context="How would you like to hear from us? Email Online Advertisements")
    ads = Field("2", "input", "checkbox", name="consent", label="Online Advertisements", context="How would you like to hear from us?")
    assert map_field(email, ctx(profile, tags=["nl"])).kind == "check"
    assert map_field(email, ctx(profile)).kind == "skip"
    assert map_field(ads, ctx(profile, tags=["nl"])).kind == "skip"


def test_single_radio_consent(profile):
    f = Field("r", "radio", "radio", label="I agree to the terms and conditions.", options=[
        {"value": "yes", "text": "Terms and conditions: https://example.com/terms", "idx": "t1"}])
    assert map_field(f, ctx(profile)).value == "t1"


def test_county_abbreviations_match_select_options(profile):
    # Muddy Stilettos: profile county "Berks" didn't match "Berkshire", the select was skipped and submit stayed disabled.
    opts = [{"value": "", "text": "-- Select your country or county"}, {"value": "4", "text": "Bucks & Oxon"},
            {"value": "5", "text": "Berkshire"}, {"value": "9", "text": "Glos & Worcs"}]
    sel = Field("1", "select", "select", name="county", label="Select country or county", options=opts)
    assert map_field(sel, ctx(dict(profile, county="Berks"))).value == "5"
    assert map_field(sel, ctx(dict(profile, county="Oxfordshire"))).value == "4"
    assert map_field(sel, ctx(dict(profile, county="Worcestershire"))).value == "9"


def test_country_box_is_not_a_quiz_question(profile):
    # CinemaBlend: "Country" starts with "count" and was filled with the quiz answer.
    a = map_field(Field("1", "input", "text", name="country", label="Country"), ctx(profile, answers=["Festive Road"]))
    assert a.kind == "fill" and a.value == "United Kingdom"


def test_sums_are_a_captcha_even_when_the_comp_has_an_answer():
    # Active Traveller: "9 + 3 equals" went to the AI (which said 11) because the comp's question had an answer.
    from app.entry.forms import Ctx, Field, map_field
    ctx = Ctx(profile={"first_name": "Rob"}, answers=["Croatia"])
    a = map_field(Field(idx="1", tag="input", label="9 + 3 equals *", required=True), ctx)
    assert a.kind == "unknown" and a.note.startswith("captcha"), a
    a = map_field(Field(idx="2", tag="input", label="Keyword", required=True), Ctx(profile={}, answers=["UNIVERSE"]))
    assert (a.kind, a.value) == ("fill", "UNIVERSE"), a

def test_region_lists_use_your_region_or_country():
    # Vegan Food & Living's "Region" is United Kingdom / Europe / USA...; other forms list regions of England.
    from app.entry.forms import Ctx, Field, map_field
    ctx = Ctx(profile={"county": "Berks", "country": "United Kingdom"})
    countries = [{"value": v, "text": v} for v in ["", "United Kingdom", "Europe", "USA/Canada", "Other"]]
    countries[0]["text"] = "Please select"
    a = map_field(Field(idx="r", tag="select", label="Region", options=countries, required=True), ctx)
    assert (a.kind, a.value) == ("select", "United Kingdom"), a
    regions = [{"value": v, "text": v} for v in ["London", "South East", "South West", "Scotland"]]
    a = map_field(Field(idx="r2", tag="select", label="Your region *", options=regions, required=True), ctx)
    assert (a.kind, a.value) == ("select", "South East"), a
    a = map_field(Field(idx="c", tag="select", label="County", options=regions, required=True), ctx)
    assert a.kind == "unknown"  # a "County" list without your county is still a question for you

# --------------------------------------------------------------------------- 5 Nov overnight run


def _f(label, tag="input", type_="text", options=(), required=True, placeholder="", context=""):
    from app.entry.forms import Field
    opts = [{"value": o, "text": o, "idx": f"o{i}"} for i, o in enumerate(options)]
    return Field(idx="x", tag=tag, type=type_, label=label, placeholder=placeholder, required=required, options=opts,
                 context=context)


def test_every_kind_of_sum_is_a_captcha():
    # Outdoors Magic: "9 – 5 equals" (en dash) went to the AI, which filled it in.
    from app.entry.forms import Ctx, map_field
    ctx = Ctx(profile={"first_name": "Rob"})
    for label in ["9 \u2013 5 equals *", "9 - 5 =", "What is 9 \u2212 5?", "3 + 4", "Seven plus two equals",
                  "What is three times two?", "Please solve: 6 \u00f7 2"]:
        assert map_field(_f(label), ctx).note.startswith("captcha"), label
    assert map_field(_f("What is 3 + 4?", tag="radio", options=["6", "7", "8"]), ctx).note.startswith("captcha")
    for label in ["Age (18 - 99)", "Phone number", "Postcode"]:  # ranges and ordinary boxes aren't sums
        assert not map_field(_f(label, required=False), ctx).note.startswith("captcha"), label
    assert map_field(_f("18-24", type_="checkbox"), ctx).note != "captcha: maths anti-spam check"
    assert map_field(_f("Security Code *"), ctx).note.startswith("captcha")  # The Times


def test_marketing_questions_are_answered_no():
    # Cornwall Living: the AI answered "Receive emails from Silver Origins?" with YES PLEASE!
    from app.entry.forms import Ctx, map_field
    ctx = Ctx(profile={})
    f = _f("Receive emails from Silver Origins? *", tag="radio", options=["YES PLEASE!", "NO THANKS"])
    a = map_field(f, ctx)
    assert (a.kind, a.value, a.note) == ("radio", "o1", "yes/no"), a
    # Yeo Valley: "you can unsubscribe at any time" made an opt-in box look like an opt-out box, so it was ticked.
    box = _f("I'd also like to receive all emails from Yeo Valley. You can unsubscribe at any time.", type_="checkbox",
             required=False)
    assert map_field(box, ctx).kind == "uncheck"
    for label in ["Tick this box if you do not wish to receive marketing", "Opt out of marketing emails",
                  "Please tick here if you'd prefer not to be contacted", "If you don't want to hear from us, tick here"]:
        assert map_field(_f(label, type_="checkbox", required=False), ctx).kind == "check", label


def test_age_groups_come_from_your_date_of_birth():
    # 58 & Co: the AI picked 18-24 for "Age *".
    from datetime import date

    from app.entry.forms import Ctx, map_field
    today = date.today()
    ctx = Ctx(profile={"dob": date(today.year - 38, 1, 1).isoformat()})
    groups = ["Under 18", "18\u201424", "25-34", "35 to 44", "45-54", "55+"]
    a = map_field(_f("Age *", tag="radio", options=groups), ctx)
    assert (a.kind, a.value, a.source) == ("radio", "o3", "age"), a
    a = map_field(_f("Age group", tag="select", options=["", *groups]), ctx)
    assert a.value == "o4", a
    old = Ctx(profile={"dob": date(today.year - 70, 1, 1).isoformat()})
    assert map_field(_f("Age *", tag="radio", options=groups), old).value == "o5"
    assert map_field(_f("Age *", tag="radio", options=groups), Ctx(profile={})).kind == "unknown"


def test_birthday_and_split_date_of_birth_boxes():
    # 58 & Co: "Birthday *" (MM/DD) got the month only; Cornwall Living: DD / MM / YYYY boxes went to the AI.
    from app.entry.forms import Ctx, map_field
    ctx = Ctx(profile={"dob": "1980-03-05"})
    assert map_field(_f("Birthday *", placeholder="MM/DD"), ctx).value == "03/05"
    assert map_field(_f("Date of birth", placeholder="DD/MM/YYYY"), ctx).value == "05/03/1980"
    assert map_field(_f("Date of birth (YYYY-MM-DD)"), ctx).value == "1980-03-05"
    ctx_dob = "DATE OF BIRTH * DD MM YYYY"
    assert map_field(_f("DD", context=ctx_dob), ctx).value == "05"
    assert map_field(_f("DATE OF BIRTH *", placeholder="MM", context=ctx_dob), ctx).value == "03"
    assert map_field(_f("YYYY", context=ctx_dob), ctx).value == "1980"
    assert map_field(_f("Day", context="Which day would you like to visit?", required=False), ctx).kind != "fill"

def test_visible_label_beats_a_stray_search_aria_label(profile):
    # Purina puts aria-label="Enter search query" on every box: its First name / Last name boxes were left empty.
    c = ctx(profile)
    aria = "Enter search query"
    assert map_field(Field("1", "input", "text", name="first_name", label="First name", aria=aria, required=True), c).value == "Test"
    assert map_field(Field("2", "input", "text", name="last_name", label="Last name", aria=aria, required=True), c).value == "Person"
    # ...but a real search box, and a hidden-from-people trap, stay empty.
    assert map_field(Field("3", "input", "text", name="q", placeholder="Search", aria="Search"), c).kind == "skip"
    assert map_field(Field("4", "input", "text", name="hp_name", label="Name", aria="Leave this field blank"), c).kind == "skip"


def test_opt_in_with_over_18_boilerplate_stays_unticked(profile):
    # Purina: "Click if you would like to receive offers... You confirm that you are over 18 years old." was ticked as
    # an eligibility box.
    c = ctx(profile)
    opt = ("Click if you would like to receive offers, news, competitions and information about Purina UK&I, its "
           "brands and its products. You can withdraw your consent at any time here. You confirm that you are over 18 "
           "years old. If you are already opted-in you will continue to receive future communications.")
    assert map_field(Field("1", "input", "checkbox", label=opt), c).kind == "uncheck"
    terms = ("I'm 18 or over and have read and understood the Terms and Conditions of the promotion. I understand that "
             "Nestle will not use my personal information for any purpose other than to contact me if I win.")
    assert map_field(Field("2", "input", "checkbox", label=terms, required=True), c).kind == "check"


def test_data_shared_with_a_sponsor_is_third_party_marketing(profile):
    # Roborock: "I consent to the transfer of my personal data to Real Madrid C.F. ... commercial communications".
    c = ctx(profile)
    lab = ("I consent to the transfer of my personal data to Real Madrid C.F. for the sending of information and "
           "commercial communications, including by electronic means and prior profiling, of products, services and "
           "activities of Real Madrid and its sponsors and collaborators.")
    a = map_field(Field("1", "input", "checkbox", label=lab), c)
    assert (a.kind, a.note) == ("uncheck", "third-party marketing"), a
    assert map_field(Field("2", "input", "checkbox", label="I agree to the Privacy Policy and Terms and Conditions."),
                     c).kind == "check"
