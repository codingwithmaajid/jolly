"""Case lookup and source-grounded briefs.

Briefs here are deterministic: they are assembled only from rows in the
case store, and every sentence carries the document and paragraph it came
from. An LLM layer can rephrase them later, but it must never add facts
that are not in these citations. Jolly never predicts outcomes.
"""

import re
from datetime import date

from pydantic import BaseModel
from sqlmodel import Session, col, or_, select

from backend.models.cases import Case, Document, HearingEvent, Task
from backend.services.legal_ids import legal_tokens, words_to_digits
from backend.services.speech import speak_case_number, speak_title, speakable, spoken_date, spoken_dates

_NUMBER_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_TENS = {"ten": 1, "twenty": 2, "thirty": 3, "forty": 4, "fifty": 5, "sixty": 6, "seventy": 7, "eighty": 8, "ninety": 9}
_TEENS = {"eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
          "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19"}


class Citation(BaseModel):
    doc_id: str
    doc_title: str
    para: str | None = None
    doc_date: date | None = None


class BriefLine(BaseModel):
    text: str
    citation: Citation | None = None


class CaseBrief(BaseModel):
    case_id: str
    title: str
    case_number: str
    court: str
    court_hall: str | None
    cause_list_item: int | None
    next_hearing: date | None
    next_hearing_purpose: str | None
    stage: str
    lines: list[BriefLine]
    open_tasks: list[str]
    spoken_summary: str


class DailyBrief(BaseModel):
    date: date
    advocate: str | None
    matters: list[CaseBrief]
    spoken_summary: str
    # The day said aloud in each call language: {"en": "twenty-seventh October", "ta": ..., "hi": ...}
    spoken_date: dict[str, str] = {}


def spoken_digits_to_number(text: str) -> str:
    """'ten forty two' -> '1042', 'zero seven eight nine' -> '0789', 'C.C. 1042' -> '1042'."""
    tokens = re.findall(r"[a-z]+|\d+", text.lower())
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.isdigit():
            out.append(t)
        elif t in _TEENS:
            out.append(_TEENS[t])
        elif t in _TENS:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt in _NUMBER_WORDS and _NUMBER_WORDS[nxt] != "0":
                out.append(f"{_TENS[t]}{_NUMBER_WORDS[nxt]}")
                i += 1
            else:
                out.append(f"{_TENS[t]}0")
        elif t in _NUMBER_WORDS and t not in ("o",):
            out.append(_NUMBER_WORDS[t])
        i += 1
    return "".join(out)


def find_case(session: Session, query: str) -> Case | None:
    """Match by matter id, spoken number, court number, or party name."""
    q = query.strip()
    if not q:
        return None
    if case := session.get(Case, q):
        return case
    # "C.C. No. 1042 of 2025", "சி சி பத்து நாற்பத்தி இரண்டு", "१०४२": try the case number itself first
    for tok in legal_tokens(q):
        m = re.fullmatch(r"[a-z]+(\d+)", tok)
        if m and (case := session.get(Case, m.group(1)) or session.get(Case, m.group(1).zfill(4))):
            return case
    digits = spoken_digits_to_number(words_to_digits(q))
    if digits:
        if case := session.get(Case, digits):
            return case
        if case := session.get(Case, digits.zfill(4)):
            return case
        stripped = digits.lstrip("0") or "0"
        hit = session.exec(select(Case).where(col(Case.case_number).contains(f" {stripped} "))).first()
        if hit:
            return hit
    like = f"%{q}%"
    return session.exec(
        select(Case).where(or_(col(Case.client_name).ilike(like), col(Case.opposing_party).ilike(like), col(Case.title).ilike(like)))
    ).first()


def _cite(session: Session, doc_id: str | None, para: str | None) -> Citation | None:
    if not doc_id:
        return None
    doc = session.get(Document, doc_id)
    if doc is None:
        return None
    return Citation(doc_id=doc.id, doc_title=doc.title, para=para, doc_date=doc.doc_date)


def _spoken_date(d: date | None, today: date) -> str:
    """'today' / 'tomorrow' / 'twelfth November' (speech.spoken_date, English)."""
    return spoken_date(d, "en", today)


def _lc_first(s: str) -> str:
    return s[:1].lower() + s[1:]


def build_case_brief(session: Session, case: Case, today: date | None = None) -> CaseBrief:
    today = today or date.today()
    events = session.exec(
        select(HearingEvent).where(HearingEvent.case_id == case.id).order_by(col(HearingEvent.event_date).desc())
    ).all()
    tasks = session.exec(select(Task).where(Task.case_id == case.id, Task.status == "open")).all()

    lines: list[BriefLine] = []
    last_hearing = next((e for e in events if e.kind in ("hearing", "order", "dictation")), None)
    if last_hearing:
        lines.append(BriefLine(
            text=f"Last date ({last_hearing.event_date.strftime('%d %b %Y')}): {last_hearing.outcome}",
            citation=_cite(session, last_hearing.source_doc_id, last_hearing.source_para),
        ))
    newer_filings = [e for e in events if e.kind == "filing" and (not last_hearing or e.event_date > last_hearing.event_date)]
    for e in newer_filings:
        lines.append(BriefLine(
            text=f"Since then ({e.event_date.strftime('%d %b %Y')}): {e.outcome}",
            citation=_cite(session, e.source_doc_id, e.source_para),
        ))

    hearing_word = _spoken_date(case.next_hearing, today)
    where = f" in {case.court_hall}" if case.court_hall else ""
    item = f", item {case.cause_list_item}" if case.cause_list_item else ""
    purpose = _lc_first(case.next_hearing_purpose or case.stage)
    say = lambda text: speakable(text, "en", today)  # noqa: E731  dates, case nos, PW1, Rs. for TTS
    spoken = [f"{speak_title(case.title)}, {speak_case_number(case.case_number)}. "
              f"Next hearing {hearing_word}{where}{item}, for {say(purpose)}."]
    if last_hearing:
        spoken.append(f"Last time, on {_spoken_date(last_hearing.event_date, today)}: {say(last_hearing.outcome)}")
    for e in newer_filings:
        spoken.append(f"Since then: {say(e.outcome)}")
    if tasks:
        spoken.append("Open tasks: " + "; ".join(say(t.title) for t in tasks[:2]) + ".")

    return CaseBrief(
        case_id=case.id,
        title=case.title,
        case_number=case.case_number,
        court=case.court,
        court_hall=case.court_hall,
        cause_list_item=case.cause_list_item,
        next_hearing=case.next_hearing,
        next_hearing_purpose=case.next_hearing_purpose,
        stage=case.stage,
        lines=lines,
        open_tasks=[t.title for t in tasks],
        spoken_summary=" ".join(spoken),
    )


def build_daily_brief(session: Session, day: date | None = None, advocate: str | None = None) -> DailyBrief:
    day = day or date.today()
    stmt = select(Case).where(Case.next_hearing == day)
    if advocate:
        stmt = stmt.where(col(Case.advocate).ilike(f"%{advocate}%"))
    cases = sorted(session.exec(stmt).all(), key=lambda c: (c.court, c.cause_list_item or 999))
    matters = [build_case_brief(session, c, today=day) for c in cases]

    if not matters:
        spoken = "You have no matters listed today."
    else:
        n = len(matters)
        intro = f"Good morning. You have {n} matter{'s' if n != 1 else ''} today."
        parts = [intro]
        for i, m in enumerate(matters, 1):
            where = f"{m.court_hall}, " if m.court_hall else ""
            item = f"item {m.cause_list_item}" if m.cause_list_item else "unlisted item"
            parts.append(f"{i}. {speak_title(m.title)}, {where}{item}: {speakable(m.next_hearing_purpose or m.stage, 'en', day)}.")
        parts.append("Ask me about any case for details.")
        spoken = " ".join(parts)
    return DailyBrief(date=day, advocate=advocate, matters=matters, spoken_summary=spoken,
                      spoken_date=spoken_dates(day))


MORNING_CALL_KEYS = ("call_type", "advocate_name", "daily_brief", "brief_date", "brief_date_spoken",
                     "brief_date_ta", "brief_date_hi", "matters_today", "first_matter", "timezone")


def morning_call_user_data(brief: DailyBrief, advocate_name: str | None = None, language: str = "en") -> dict[str, str]:
    """The `user_data` the 8 AM call sends on POST /call, so the agent opens with the brief and no tool call.

    Keys match the {variables} in scripts/bolna_agent.py's prompt and welcome
    message (docs/VOICE_AGENT.md). All values are short strings: Bolna
    substitutes them verbatim, and an unresolved {var} is spoken literally.
    """
    name = advocate_name or brief.advocate or "Counsel"
    first = brief.matters[0] if brief.matters else None
    return {
        "call_type": "morning_brief",
        "advocate_name": name,
        "daily_brief": brief.spoken_summary,
        "brief_date": brief.date.isoformat(),
        "brief_date_spoken": brief.spoken_date.get(language) or spoken_date(brief.date, language),
        "brief_date_ta": brief.spoken_date.get("ta") or spoken_date(brief.date, "ta"),
        "brief_date_hi": brief.spoken_date.get("hi") or spoken_date(brief.date, "hi"),
        "matters_today": str(len(brief.matters)),
        "first_matter": (f"{first.title}, {first.court_hall or first.court}, item {first.cause_list_item}"
                         if first else "none"),
        "timezone": "Asia/Kolkata",
    }
