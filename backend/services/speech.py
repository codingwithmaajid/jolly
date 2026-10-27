"""Turn record text into something a voice can say cleanly.

TTS engines read "27.10.2026", "C.C. No. 1042 of 2025", "PW1" and
"Rs. 4,80,000" badly, and pronunciation dictionaries only work on some
providers (ElevenLabs, Cartesia sonic-3+; ignored on Sarvam:
https://www.bolna.ai/docs/customizations/languages/tamil.md). So Jolly
normalises tool output itself, before Bolna's LLM ever sees it:

    spoken_date(date(2026, 10, 27))              -> "twenty-seventh October"
    spoken_date(date(2026, 10, 27), lang="ta")   -> "இருபத்தி ஏழு அக்டோபர்"
    spoken_date(date(2026, 10, 27), lang="hi")   -> "सत्ताईस अक्टूबर"
    speak_case_number("C.C. No. 1042 of 2025")   -> "C C number ten forty-two of twenty twenty-five"
    speakable("PW1 absent; adjourned to 12.11.2026")
        -> "P W one absent; adjourned to twelfth November"

Case numbers are read the way advocates read them (pairs: "ten forty-two"),
which find_case() parses straight back. Per Bolna's Tamil guide, IDs stay in
English even on Tamil/Hindi calls; only dates get native number words.
Tamil and Hindi forms here should get a native speaker's review.
"""

from __future__ import annotations

import re
from datetime import date

from backend.services.legal_ids import CASE_TYPES, HI_NUMBERS, TA_TEENS, TA_TENS, TA_TENS_JOIN, TA_UNITS

# ---------------------------------------------------------------------------
# English numbers
# ---------------------------------------------------------------------------

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth",
        "twelve": "twelfth"}


def en_words(n: int) -> str:
    """Cardinal in Indian English: 480000 -> 'four lakh eighty thousand'."""
    if n < 0:
        return "minus " + en_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + en_words(n % 100))
    for size, name in ((10_000_000, "crore"), (100_000, "lakh"), (1000, "thousand")):
        if n >= size:
            return en_words(n // size) + f" {name}" + ("" if n % size == 0 else " " + en_words(n % size))
    return str(n)


def en_ordinal(n: int) -> str:
    words = en_words(n)
    head, sep, last = words.rpartition("-") if "-" in words else ("", "", words)
    if last in _ORD:
        last = _ORD[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return f"{head}{sep}{last}"


def en_year(y: int) -> str:
    """2025 -> 'twenty twenty-five', 2000 -> 'two thousand', 2005 -> 'two thousand five'."""
    if 2000 <= y <= 2009:
        return en_words(y)
    return f"{en_words(y // 100)} {en_words(y % 100)}" if y % 100 else f"{en_words(y // 100)} hundred"


def speak_id_number(digits: str) -> str:
    """How an advocate reads a case/section number: '1042' -> 'ten forty-two', '317' -> 'three seventeen'.

    Leading zeros, long numbers, or a '00' pair are read digit by digit so
    nothing is ambiguous ('004512' -> 'zero zero four five one two').
    """
    digits = digits.strip()
    if not digits.isdigit():
        return digits
    if digits.startswith("0") and len(digits) > 1 or len(digits) > 4:
        return " ".join(_ONES[int(c)] for c in digits)
    n = int(digits)
    if n < 100:
        return en_words(n)
    if len(digits) == 3:
        rest = int(digits[1:])
        return f"{_ONES[int(digits[0])]} " + (f"oh {_ONES[rest]}" if rest < 10 else en_words(rest))
    hi, lo = int(digits[:2]), int(digits[2:])
    if lo == 0:
        return " ".join(_ONES[int(c)] for c in digits)
    return f"{en_words(hi)} " + (f"oh {_ONES[lo]}" if lo < 10 else en_words(lo))


# ---------------------------------------------------------------------------
# Tamil and Hindi numbers (for dates)
# ---------------------------------------------------------------------------

_TA_HUNDREDS = {1: "நூறு", 2: "இருநூறு", 3: "முந்நூறு", 4: "நானூறு", 5: "ஐநூறு", 6: "அறுநூறு", 7: "எழுநூறு",
                8: "எண்ணூறு", 9: "தொள்ளாயிரம்"}
_TA_HUNDREDS_JOIN = {1: "நூற்றி", 2: "இருநூற்றி", 3: "முந்நூற்றி", 4: "நானூற்றி", 5: "ஐநூற்றி", 6: "அறுநூற்றி",
                     7: "எழுநூற்றி", 8: "எண்ணூற்றி", 9: "தொள்ளாயிரத்து"}
_TA_THOUSANDS = {1: "ஆயிரம்", 2: "இரண்டாயிரம்", 3: "மூவாயிரம்", 4: "நாலாயிரம்", 5: "ஐயாயிரம்", 6: "ஆறாயிரம்",
                 7: "ஏழாயிரம்", 8: "எட்டாயிரம்", 9: "ஒன்பதாயிரம்"}


def ta_words(n: int) -> str:
    """Tamil cardinal, 0..9999 (spoken style: 27 -> 'இருபத்தி ஏழு', 2026 -> 'இரண்டாயிரத்து இருபத்தி ஆறு')."""
    if n < 10:
        return TA_UNITS[n]
    if n < 20:
        return TA_TEENS[n - 10]
    if n < 100:
        t, u = divmod(n, 10)
        return TA_TENS[t] if u == 0 else f"{TA_TENS_JOIN[t]} {TA_UNITS[u]}"
    if n < 1000:
        h, r = divmod(n, 100)
        return _TA_HUNDREDS[h] if r == 0 else f"{_TA_HUNDREDS_JOIN[h]} {ta_words(r)}"
    if n < 10000:
        th, r = divmod(n, 1000)
        word = _TA_THOUSANDS[th]
        return word if r == 0 else f"{word[:-2]}த்து {ta_words(r)}"
    return str(n)


def hi_words(n: int) -> str:
    """Hindi cardinal, 0..99999 (2026 -> 'दो हज़ार छब्बीस')."""
    if n < 100:
        return HI_NUMBERS[n]
    parts = []
    if n >= 1000:
        parts.append(f"{hi_words(n // 1000)} हज़ार")
        n %= 1000
    if n >= 100:
        parts.append(f"{HI_NUMBERS[n // 100]} सौ")
        n %= 100
    if n:
        parts.append(HI_NUMBERS[n])
    return " ".join(parts)


_MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
    "ta": ["ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "ஜூன்", "ஜூலை", "ஆகஸ்ட்", "செப்டம்பர்", "அக்டோபர்",
           "நவம்பர்", "டிசம்பர்"],
    "hi": ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर",
           "दिसंबर"],
}
_RELATIVE = {
    "en": {0: "today", 1: "tomorrow", -1: "yesterday"},
    "ta": {0: "இன்று", 1: "நாளை", -1: "நேற்று"},
    "hi": {0: "आज", 1: "कल"},  # 'कल' is also 'yesterday'; say the date for the past
}
_NO_DATE = {"en": "no date fixed", "ta": "தேதி குறிக்கப்படவில்லை", "hi": "तारीख तय नहीं"}


def spoken_date(d: date | None, lang: str = "en", today: date | None = None, with_year: bool | None = None) -> str:
    """A date as it should be spoken. `today` enables 'today'/'tomorrow'. Year only when it differs from today's (or asked)."""
    lang = lang if lang in _MONTHS else "en"
    if d is None:
        return _NO_DATE[lang]
    if today is not None and (d - today).days in _RELATIVE[lang]:
        return _RELATIVE[lang][(d - today).days]
    if with_year is None:
        with_year = today is not None and d.year != today.year
    month = _MONTHS[lang][d.month - 1]
    if lang == "ta":
        out = f"{ta_words(d.day)} {month}" + (f" {ta_words(d.year)}" if with_year else "")
    elif lang == "hi":
        out = f"{hi_words(d.day)} {month}" + (f" {hi_words(d.year)}" if with_year else "")
    else:
        out = f"{en_ordinal(d.day)} {month}" + (f" {en_year(d.year)}" if with_year else "")
    return out


def spoken_dates(d: date | None, today: date | None = None) -> dict[str, str]:
    return {lang: spoken_date(d, lang, today) for lang in ("en", "ta", "hi")}


# ---------------------------------------------------------------------------
# Identifiers and free text
# ---------------------------------------------------------------------------

_CASE_NO = re.compile(r"\b((?:[A-Z][a-z]{0,2}\.\s?){1,4}|[A-Z]{1,4}\s)\s*No\.?\s*(\d{1,6})(?:\s*(?:of|/)\s*(\d{4}))?")
_NUM_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")
_WITNESS = re.compile(r"\b([PDCR])\.?\s?W\.?\s?-?(\d{1,3})\b")
_EXHIBIT = re.compile(r"\bEx\.?\s?([PDCR])\.?\s?(\d{1,3})\b")
_BARE_EXHIBIT = re.compile(r"\b(to|and)\s+([PD])(\d{1,3})\b")
_SECTION = re.compile(r"(?<![\w/])(u/s\.?|Sec(?:tion)?s?\.?)\s?(\d{1,4})([A-Z]?)\b", re.I)
_RUPEES = re.compile(r"\bRs\.?\s?([\d,]+)(?:/-)?")
_ACRONYMS = {"CrPC": "C R P C", "BNSS": "B N S S", "NI Act": "N I Act", "IPC": "I P C", "CPC": "C P C"}


def _case_no_rep(m: re.Match) -> str:
    pieces = re.findall(r"[A-Z][a-z]{0,2}\.\s?|[A-Z]{1,4}\s", m.group(1))
    kept = ""
    for i in range(len(pieces)):  # drop leading words that aren't part of a case type ("Act. C.C. No.")
        letters = re.sub(r"[^a-z]", "", "".join(pieces[i:]).lower())
        if letters in CASE_TYPES:
            kept = "".join(pieces[:i])
            prefix = "".join(pieces[i:]).strip()
            break
    else:
        return m.group(0)
    letters = re.sub(r"[^A-Za-z]", "", prefix)
    spoken_prefix = " ".join(letters.upper()) if len(letters) <= 3 else " ".join(p for p in re.split(r"[.\s]+", prefix) if p)
    out = f"{kept}{spoken_prefix} number {speak_id_number(m.group(2))}"
    if m.group(3):
        out += f" of {en_year(int(m.group(3)))}"
    return out


def speak_case_number(case_number: str) -> str:
    """'C.C. No. 1042 of 2025' -> 'C C number ten forty-two of twenty twenty-five'."""
    return _CASE_NO.sub(_case_no_rep, case_number)


def speakable(text: str, lang: str = "en", today: date | None = None) -> str:
    """Rewrite dates, case numbers, witnesses, exhibits, sections and rupee amounts for TTS."""
    def date_rep(m: re.Match) -> str:
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return m.group(0)
        return spoken_date(d, lang, today)

    t = _NUM_DATE.sub(date_rep, text)
    t = _CASE_NO.sub(_case_no_rep, t)
    t = _EXHIBIT.sub(lambda m: f"exhibit {m.group(1)} {en_words(int(m.group(2)))}", t)
    t = _BARE_EXHIBIT.sub(lambda m: f"{m.group(1)} {m.group(2)} {en_words(int(m.group(3)))}", t)
    t = _WITNESS.sub(lambda m: f"{m.group(1)} W {en_words(int(m.group(2)))}", t)
    t = _SECTION.sub(lambda m: ("under section " if m.group(1).lower().startswith("u")
                                else "Section " if m.group(1)[0] == "S" else "section ")
                     + speak_id_number(m.group(2)) + (f" {m.group(3)}" if m.group(3) else ""), t)
    t = _RUPEES.sub(lambda m: f"rupees {en_words(int(m.group(1).replace(',', '')))}", t)
    for k, v in _ACRONYMS.items():
        t = t.replace(k, v)
    return t


_TITLE_WORDS = ((" v. ", " versus "), (" vs. ", " versus "), (" Vs. ", " versus "), ("Pvt. Ltd.", "Private Limited"),
                ("Pvt.", "Private"), ("Ltd.", "Limited"), ("Adv. ", "Advocate "))


def speak_title(title: str) -> str:
    """'A v. B Pvt. Ltd.' -> 'A versus B Private Limited'."""
    for k, v in _TITLE_WORDS:
        title = title.replace(k, v)
    return title
