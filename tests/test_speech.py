"""Speakable tool output: dates and case numbers in spoken form (English, Tamil, Hindi)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from backend.core.config import Settings
from backend.main import create_app
from backend.services.cases import MORNING_CALL_KEYS, build_daily_brief, find_case, morning_call_user_data
from backend.services.speech import (
    en_ordinal, en_words, hi_words, speak_case_number, speak_id_number, speak_title, speakable, spoken_date, ta_words,
)

T = date(2026, 10, 27)


@pytest.mark.parametrize("d, en, ta, hi", [
    (date(2026, 10, 27), "today", "இன்று", "आज"),
    (date(2026, 10, 28), "tomorrow", "நாளை", "कल"),
    (date(2026, 11, 1), "first November", "ஒன்று நவம்பர்", "एक नवंबर"),
    (date(2026, 11, 12), "twelfth November", "பன்னிரண்டு நவம்பர்", "बारह नवंबर"),
    (date(2026, 11, 23), "twenty-third November", "இருபத்தி மூன்று நவம்பர்", "तेईस नवंबर"),
    (date(2026, 11, 30), "thirtieth November", "முப்பது நவம்பர்", "तीस नवंबर"),
])
def test_spoken_date_three_languages(d, en, ta, hi):
    assert spoken_date(d, "en", T) == en
    assert spoken_date(d, "ta", T) == ta
    assert spoken_date(d, "hi", T) == hi


def test_spoken_date_year_only_when_different():
    assert spoken_date(date(2027, 1, 22), "en", T) == "twenty-second January twenty twenty-seven"
    assert spoken_date(date(2027, 1, 22), "ta", T) == "இருபத்தி இரண்டு ஜனவரி இரண்டாயிரத்து இருபத்தி ஏழு"
    assert spoken_date(date(2027, 1, 22), "hi", T) == "बाईस जनवरी दो हज़ार सत्ताईस"
    assert spoken_date(None) == "no date fixed"


def test_number_words():
    assert en_words(480000) == "four lakh eighty thousand"
    assert en_words(3800000) == "thirty-eight lakh"
    assert en_ordinal(21) == "twenty-first" and en_ordinal(12) == "twelfth" and en_ordinal(30) == "thirtieth"
    assert ta_words(2026) == "இரண்டாயிரத்து இருபத்தி ஆறு" and ta_words(42) == "நாற்பத்தி இரண்டு"
    assert hi_words(2026) == "दो हज़ार छब्बीस" and hi_words(42) == "बयालीस"


@pytest.mark.parametrize("n, spoken", [
    ("1042", "ten forty-two"), ("2217", "twenty-two seventeen"), ("789", "seven eighty-nine"),
    ("138", "one thirty-eight"), ("1005", "ten oh five"), ("004512", "zero zero four five one two"),
])
def test_id_numbers_read_like_an_advocate(n, spoken):
    assert speak_id_number(n) == spoken


def test_case_numbers_and_round_trip_through_find_case():
    app = create_app(Settings(database_url="sqlite://"))
    cases = {"C.C. No. 1042 of 2025": "C C number ten forty-two of twenty twenty-five",
             "C.S. No. 2217 of 2024": "C S number twenty-two seventeen of twenty twenty-four",
             "M.C. No. 789 of 2025": "M C number seven eighty-nine of twenty twenty-five"}
    with Session(app.state.engine) as s:
        for written, spoken in cases.items():
            assert speak_case_number(written) == spoken
            assert find_case(s, spoken).case_number == written


def test_speakable_rewrites_record_text():
    out = speakable("PW1 absent; Ex.P1 to P5 marked. Petition u/s 317 CrPC. Adjourned to 12.11.2026. "
                    "Cheque for Rs. 4,80,000. Act. C.C. No. 1042 of 2025.", "en", T)
    assert "P W one" in out and "exhibit P one to P five" in out
    assert "under section three seventeen C R P C" in out
    assert "adjourned to twelfth November".lower() in out.lower()
    assert "rupees four lakh eighty thousand" in out
    assert "Act. C C number ten forty-two of twenty twenty-five" in out
    assert "12.11.2026" not in out and "PW1" not in out


def test_speak_title():
    assert speak_title("Lakshmi Narayanan v. Coastal Realty Developers Pvt. Ltd.") == \
        "Lakshmi Narayanan versus Coastal Realty Developers Private Limited"


def test_briefs_are_speakable():
    c = TestClient(create_app(Settings(database_url="sqlite://")))
    case = c.post("/bolna/tools/get-case-brief", json={"case_id": "1042"}).json()["spoken_summary"]
    assert "C C number ten forty-two of twenty twenty-five" in case and "PW1" not in case
    assert "Next hearing today" in case and "versus" in case
    daily = c.get("/briefs/daily").json()
    assert daily["spoken_date"]["en"] and daily["spoken_date"]["ta"] and daily["spoken_date"]["hi"]
    assert "P W one" in daily["spoken_summary"]


def test_morning_call_user_data_keys_match_agent_prompt_variables():
    import scripts.bolna_agent as agent

    app = create_app(Settings(database_url="sqlite://"))
    with Session(app.state.engine) as s:
        data = morning_call_user_data(build_daily_brief(s), "Adv. Priya Raman")
    assert set(data) == set(MORNING_CALL_KEYS)
    assert all(isinstance(v, str) and v for v in data.values())
    assert data["daily_brief"].startswith("Good morning. You have 2 matters today.")
    for var in agent.PROMPT_VARIABLES:
        assert var in data, var
    for prompt in agent.PROMPTS.values():
        used = set(__import__("re").findall(r"\{(\w+)\}", prompt))
        assert used <= set(data), used - set(data)
