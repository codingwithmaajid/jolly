"""Normalise Indian court identifiers so keyword search can match them.

The same thing turns up in many spellings: typed in an order sheet
("P.W.1", "u/s 138", "C.C.No.1042/2025", "12.09.2026") and spoken on a
call ("PW one", "section one thirty eight", "C C ten forty two",
"twelfth September"). Sarvam ASR on a Tamil or Hindi call can also return
native-script digits ("௧௦௪௨", "१०४२") and number words ("பத்து நாற்பத்தி
இரண்டு", "दस बयालीस").

`legal_tokens()` maps all of these to the same canonical tokens (`pw1`,
`sec138`, `cc1042`, `cc1042_2025`, `d20260912`) and is used at both index
and query time. Nothing here is a model; it is deterministic and cheap.
"""

from __future__ import annotations

import re
from datetime import date

# ---------------------------------------------------------------------------
# Number words (English, Tamil, Hindi) -> digits
# ---------------------------------------------------------------------------

_EN_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_EN_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

TA_UNITS = ["பூஜ்ஜியம்", "ஒன்று", "இரண்டு", "மூன்று", "நான்கு", "ஐந்து", "ஆறு", "ஏழு", "எட்டு", "ஒன்பது"]
TA_TEENS = ["பத்து", "பதினொன்று", "பன்னிரண்டு", "பதிமூன்று", "பதினான்கு", "பதினைந்து", "பதினாறு", "பதினேழு",
            "பதினெட்டு", "பத்தொன்பது"]
# standalone tens, and the joining form used before a unit ("இருபத்தி ஏழு" = 27)
TA_TENS = {2: "இருபது", 3: "முப்பது", 4: "நாற்பது", 5: "ஐம்பது", 6: "அறுபது", 7: "எழுபது", 8: "எண்பது", 9: "தொண்ணூறு"}
TA_TENS_JOIN = {2: "இருபத்தி", 3: "முப்பத்தி", 4: "நாற்பத்தி", 5: "ஐம்பத்தி", 6: "அறுபத்தி", 7: "எழுபத்தி",
                8: "எண்பத்தி", 9: "தொண்ணூற்றி"}
_TA_COLLOQUIAL = {"ஜீரோ": 0, "ஒண்ணு": 1, "ரெண்டு": 2, "மூணு": 3, "நாலு": 4, "அஞ்சு": 5, "இருபத்து": None,
                  "முப்பத்து": None, "நாற்பத்து": None, "ஐம்பத்து": None}

HI_NUMBERS = [
    "शून्य", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ",
    "दस", "ग्यारह", "बारह", "तेरह", "चौदह", "पंद्रह", "सोलह", "सत्रह", "अठारह", "उन्नीस",
    "बीस", "इक्कीस", "बाईस", "तेईस", "चौबीस", "पच्चीस", "छब्बीस", "सत्ताईस", "अट्ठाईस", "उनतीस",
    "तीस", "इकतीस", "बत्तीस", "तैंतीस", "चौंतीस", "पैंतीस", "छत्तीस", "सैंतीस", "अड़तीस", "उनतालीस",
    "चालीस", "इकतालीस", "बयालीस", "तैंतालीस", "चवालीस", "पैंतालीस", "छियालीस", "सैंतालीस", "अड़तालीस", "उनचास",
    "पचास", "इक्यावन", "बावन", "तिरपन", "चौवन", "पचपन", "छप्पन", "सत्तावन", "अट्ठावन", "उनसठ",
    "साठ", "इकसठ", "बासठ", "तिरसठ", "चौंसठ", "पैंसठ", "छियासठ", "सड़सठ", "अड़सठ", "उनहत्तर",
    "सत्तर", "इकहत्तर", "बहत्तर", "तिहत्तर", "चौहत्तर", "पचहत्तर", "छिहत्तर", "सतहत्तर", "अठहत्तर", "उनासी",
    "अस्सी", "इक्यासी", "बयासी", "तिरासी", "चौरासी", "पचासी", "छियासी", "सत्तासी", "अट्ठासी", "नवासी",
    "नब्बे", "इक्यानवे", "बानवे", "तिरानवे", "चौरानवे", "पचानवे", "छियानवे", "सत्तानवे", "अट्ठानवे", "निन्यानवे",
]
_HI_ALIASES = {"पांच": 5, "छः": 6, "छे": 6, "ज़ीरो": 0, "जीरो": 0}

# word -> (value, kind) where kind is "unit" (0-9), "num" (10-99 complete), "tens" (20..90, may take a unit)
_WORDS: dict[str, tuple[int, str]] = {}
for _w, _v in _EN_UNITS.items():
    _WORDS[_w] = (_v, "unit")
for _w, _v in _EN_TEENS.items():
    _WORDS[_w] = (_v, "num")
for _w, _v in _EN_TENS.items():
    _WORDS[_w] = (_v, "tens")
for _i, _w in enumerate(TA_UNITS):
    _WORDS[_w] = (_i, "unit")
for _i, _w in enumerate(TA_TEENS):
    _WORDS[_w] = (10 + _i, "num")
for _k, _w in TA_TENS.items():
    _WORDS[_w] = (_k * 10, "num")
for _k, _w in TA_TENS_JOIN.items():
    _WORDS[_w] = (_k * 10, "tens")
    _WORDS[_w[:-1] + "ு"] = (_k * 10, "tens")  # இருபத்து, முப்பத்து ...
for _w, _v in _TA_COLLOQUIAL.items():
    if _v is not None:
        _WORDS[_w] = (_v, "unit")
for _i, _w in enumerate(HI_NUMBERS):
    _WORDS[_w] = (_i, "unit" if _i < 10 else "num")
for _w, _v in _HI_ALIASES.items():
    _WORDS[_w] = (_v, "unit")

_NATIVE_DIGITS = str.maketrans("௦௧௨௩௪௫௬௭௮௯०१२३४५६७८९", "01234567890123456789")


def native_digits_to_ascii(text: str) -> str:
    return text.translate(_NATIVE_DIGITS)


def words_to_digits(text: str) -> str:
    """Replace runs of spoken number words with digit strings, keep everything else.

    'C C ten forty two of twenty twenty five' -> 'C C 1042 of 2025'
    'PW one' -> 'PW 1';  'பத்து நாற்பத்தி இரண்டு' -> '1042';  'दस बयालीस' -> '1042'
    Each word (or tens+unit pair) contributes its digits, which is how Indian
    advocates read case numbers aloud.
    """
    text = native_digits_to_ascii(text)
    parts = re.split(r"(\s+|-)", text)
    out: list[str] = []
    run: list[str] = []
    pending_sep = ""

    def flush():
        nonlocal pending_sep
        if run:
            out.append("".join(run))
            run.clear()
        if pending_sep:
            out.append(pending_sep)
            pending_sep = ""

    i = 0
    words = [p for p in parts]
    while i < len(words):
        tok = words[i]
        if tok == "" or tok.isspace() or tok == "-":
            if run:
                pending_sep = pending_sep or tok
            else:
                out.append(tok)
            i += 1
            continue
        key = tok.lower().strip(".,;:?!")
        trail = tok[len(tok.rstrip(".,;:?!")):]
        if key in _WORDS:
            val, kind = _WORDS[key]
            if kind == "tens":
                # look ahead past one separator for a unit
                j = i + 1
                while j < len(words) and (words[j] == "" or words[j].isspace() or words[j] == "-"):
                    j += 1
                nxt = words[j].lower().strip(".,;:?!") if j < len(words) else ""
                if nxt in _WORDS and _WORDS[nxt][1] == "unit" and _WORDS[nxt][0] != 0:
                    val += _WORDS[nxt][0]
                    trail = words[j][len(words[j].rstrip(".,;:?!")):]
                    i = j
            pending_sep = ""
            run.append(str(val))
            if trail:
                flush()
                out.append(trail)
            i += 1
            continue
        flush()
        out.append(tok)
        i += 1
    flush()
    return "".join(out)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    # Tamil
    "ஜனவரி": 1, "பிப்ரவரி": 2, "மார்ச்": 3, "ஏப்ரல்": 4, "மே": 5, "ஜூன்": 6, "ஜூலை": 7, "ஆகஸ்ட்": 8,
    "செப்டம்பர்": 9, "அக்டோபர்": 10, "நவம்பர்": 11, "டிசம்பர்": 12,
    # Hindi
    "जनवरी": 1, "फ़रवरी": 2, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6, "जुलाई": 7,
    "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "अक्तूबर": 10, "नवंबर": 11, "नवम्बर": 11,
    "दिसंबर": 12, "दिसम्बर": 12,
}
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30,
}
_MONTH_RE = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b")
_DAY_MONTH = re.compile(rf"(?<!\w)(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+({_MONTH_RE})(?:[\s,]+(\d{{4}}))?(?!\w)", re.I)
_MONTH_DAY = re.compile(rf"(?<!\w)({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:[\s,]+(\d{{4}}))?(?!\w)", re.I)


def _spoken_ordinals_to_digits(text: str) -> str:
    def rep(m: re.Match) -> str:
        tens = {"twenty": 20, "thirty": 30}.get((m.group(1) or "").lower().rstrip("- "), 0)
        return str(tens + _ORDINAL_WORDS[m.group(2).lower()])
    return re.sub(r"\b(twenty[- ]|thirty[- ])?(" + "|".join(_ORDINAL_WORDS) + r")\b", rep, text, flags=re.I)


def _mk(y: int | None, m: int, d: int, today: date) -> date | None:
    if y is None:
        y = today.year
    elif y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, today: date | None = None) -> list[tuple[int, int, date]]:
    """All dates in `text` as (start, end, date). Indian order: dd.mm.yyyy."""
    today = today or date.today()
    found: list[tuple[int, int, date]] = []
    for m in _NUMERIC_DATE.finditer(text):
        d = _mk(int(m.group(3)), int(m.group(2)), int(m.group(1)), today)
        if d:
            found.append((m.start(), m.end(), d))
    for m in _DAY_MONTH.finditer(text):
        d = _mk(int(m.group(3)) if m.group(3) else None, MONTHS[m.group(2).lower()], int(m.group(1)), today)
        if d:
            found.append((m.start(), m.end(), d))
    for m in _MONTH_DAY.finditer(text):
        d = _mk(int(m.group(3)) if m.group(3) else None, MONTHS[m.group(1).lower()], int(m.group(2)), today)
        if d:
            found.append((m.start(), m.end(), d))
    found.sort()
    dedup: list[tuple[int, int, date]] = []
    for f in found:
        if dedup and f[0] < dedup[-1][1]:
            continue
        dedup.append(f)
    return dedup


def parse_spoken_date(text: str, today: date | None = None) -> date | None:
    """First date in a typed or spoken question: '12.09.2026', 'twelfth of September', '१२ सितंबर'."""
    t = words_to_digits(_spoken_ordinals_to_digits(native_digits_to_ascii(text)))
    hits = find_dates(t, today)
    return hits[0][2] if hits else None


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

# Case-type prefixes seen on Indian cause lists (district + High Court).
CASE_TYPES = (
    "crlop", "crlrc", "crla", "crlmp", "crp", "wmp", "wp", "wa", "cs", "os", "cc", "mc", "ia", "op", "cma",
    "sa", "as", "ep", "hcp", "rcop", "stc", "sc", "mop", "aos", "osa", "tos", "a",
)
_CASE_TYPE_RE = "|".join(sorted(CASE_TYPES, key=len, reverse=True))
# "C.C. No. 1042 of 2025", "CC 1042/2025", "Crl.O.P.No.55 of 2026", "A. No. 3310 of 2024", "c c 1042"
_CASE_NO = re.compile(
    r"(?<![a-z0-9])((?:[a-z]{1,4}\.?\s?){1,3}?)\s*(?:no\.?|number)?\s*(\d{1,6})(?:\s*(?:of|/)\s*(\d{4}))?(?![0-9])",
    re.I,
)
_WITNESS = re.compile(r"(?<![a-z])(p|d|c|r)\.?\s?w\.?\s?-?\s?(\d{1,3})(?!\d)", re.I)
_EXHIBIT = re.compile(r"(?<![a-z])ex(?:hibit)?\.?\s?(p|d|c|r)\.?\s?-?\s?(\d{1,3})(?!\d)", re.I)
_SECTION = re.compile(r"(?<![a-z])(?:sec(?:tion)?s?\.?|u/s\.?|s\.)\s?(\d{1,4}[a-z]?)(?![0-9])", re.I)
_ORDER_RULE = re.compile(r"(?<![a-z])o(?:rder)?\.?\s?(\d{1,2})\s*,?\s*r(?:ule)?\.?\s?(\d{1,3})(?!\d)", re.I)


def legal_tokens(text: str, today: date | None = None) -> list[str]:
    """Canonical identifier tokens found in `text` (typed or spoken)."""
    t = words_to_digits(_spoken_ordinals_to_digits(native_digits_to_ascii(text)))
    out: list[str] = []
    for m in _WITNESS.finditer(t):
        out.append(f"{m.group(1).lower()}w{int(m.group(2))}")
    for m in _EXHIBIT.finditer(t):
        out.append(f"ex{m.group(1).lower()}{int(m.group(2))}")
    for m in _SECTION.finditer(t):
        out.append(f"sec{m.group(1).lower()}")
    for m in _ORDER_RULE.finditer(t):
        out.append(f"o{int(m.group(1))}r{int(m.group(2))}")
    for m in _CASE_NO.finditer(t):
        prefix = re.sub(r"[^a-z]", "", m.group(1).lower())
        if prefix not in CASE_TYPES:
            # allow a trailing filler word: "case cc 1042" -> take the last letters that form a type
            prefix = next((p for p in sorted(CASE_TYPES, key=len, reverse=True) if prefix.endswith(p) and len(p) > 1), "")
            if not prefix:
                continue
        num = int(m.group(2))
        out.append(f"{prefix}{num}")
        if m.group(3):
            out.append(f"{prefix}{num}_{m.group(3)}")
    for _, _, d in find_dates(t, today):
        out.append(f"d{d:%Y%m%d}")
    return out


def bare_numbers(text: str) -> list[str]:
    """Standalone numbers (after word->digit conversion), e.g. '1042', '317', '004512'."""
    t = words_to_digits(native_digits_to_ascii(text))
    return [str(int(n)) for n in re.findall(r"(?<![\d.])\d{2,7}(?![\d.])", t)]
