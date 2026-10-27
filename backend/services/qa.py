"""Answer a question about one case, with citations, or decline honestly.

Pipeline:
  S0 input guard (outcome / solicitation / injection / question about another matter)
  -> S2 retrieve screened paragraphs, restricted to this case (and re-checked)
  -> abstain when the passages share no content words with the question
  -> Gemini rephrase with citation validation + S3 output check | extractive fallback
  -> PII masked in whatever is spoken.
Untrusted passages are attributed, never stated as fact: the other side's
pleadings as "the opposing party's ... claims", uploads as "an unverified
uploaded document ... says".
"""

import logging
import re
from datetime import date

from pydantic import BaseModel
from sqlmodel import Session, select

from backend.models.cases import Case, Document
from backend.services.embeddings import Embedder, tokens
from backend.services.guard import check_output, injection_rule, mask_pii, refusal_for, screen_question
from backend.services.retrieval import Hit, search, with_neighbours

logger = logging.getLogger(__name__)

CROSS_CASE_REFUSAL = (
    "I can only answer about the matter we're on. Ask me about that case separately, "
    "and only if it's in your diary."
)
NOT_FOUND = "I couldn't find that in the case file. You may want to check the physical file."
# Words that say nothing about *what* is being asked, so they can't show a passage is relevant.
_GENERIC = frozenset(tokens(
    "case matter court judge bench say said tell told last time happen happened next please kindly remind "
    "me us our we you client file record document what why when who how about there any status "
    "hearing heard date order ordered today yesterday"
))


class AnswerCitation(BaseModel):
    doc_id: str
    doc_title: str
    para: str | None
    doc_date: date | None
    quote: str
    trusted: bool


class CaseAnswer(BaseModel):
    case_id: str
    question: str
    answer: str
    answerable: bool
    mode: str  # llm | extractive | refused | not_found
    citations: list[AnswerCitation]
    guard_rule: str | None = None


def _spoken_date(d: date | None) -> str:
    return d.strftime("%-d %B %Y") if d else "an undated document"


def _is_upload(h: Hit) -> bool:
    return "-UP-" in h.doc_id


def _untrusted_label(h: Hit) -> str:
    if _is_upload(h):
        return "unverified upload; not from the court record"
    return "filed by the opposing party; their claim, not a finding"


def _source_label(h: Hit) -> str:
    return f"{h.doc_title}, {h.para}, {_spoken_date(h.doc_date)}"


def _cite(h: Hit) -> AnswerCitation:
    return AnswerCitation(doc_id=h.doc_id, doc_title=h.doc_title, para=h.para, doc_date=h.doc_date,
                          quote=mask_pii(h.text), trusted=h.trusted)


def _other_case_mentioned(session: Session, case: Case, question: str) -> bool:
    """True if the question names a different matter (its id, court number or a party)."""
    q = question.lower()
    for other in session.exec(select(Case).where(Case.id != case.id)).all():
        if re.search(rf"(?<!\d){re.escape(other.id.lower())}(?!\d)", q):
            return True
        for name in (other.client_name, other.opposing_party, other.case_number):
            if name and len(name) >= 6 and name.lower() in q and name.lower() not in (
                case.client_name.lower(), case.opposing_party.lower()):
                return True
    return False


def _restrict_to_case(session: Session, case: Case, hits: list[Hit]) -> list[Hit]:
    """Defence in depth: retrieval is already filtered by case_id; drop anything that isn't."""
    own = set(session.exec(select(Document.id).where(Document.case_id == case.id)).all())
    kept = [h for h in hits if h.doc_id in own]
    if len(kept) != len(hits):
        logger.error("retrieval returned %d passages from other cases for case %s", len(hits) - len(kept), case.id)
    return kept


def _shares_content(question: str, hits: list[Hit]) -> bool:
    content = set(tokens(question)) - _GENERIC
    if not content:
        return True
    seen: set[str] = set()
    for h in hits[:4]:
        seen.update(tokens(f"{h.doc_title} {h.text}"))
    return bool(content & seen)


def _extractive(case: Case, question: str, hits: list[Hit]) -> CaseAnswer:
    """No-LLM fallback: quote the best passages, grouped by document, in record order."""
    best = hits[0].score
    top = [h for h in hits[:4] if h.score >= best * 0.6]
    top.sort(key=lambda h: (h.doc_id, h.para or ""))
    groups: dict[str, list[Hit]] = {}
    for h in top:
        groups.setdefault(h.doc_id, []).append(h)
    parts = []
    for doc_hits in groups.values():
        h0 = doc_hits[0]
        quoted = " ".join(h.text for h in doc_hits)
        if h0.trusted:
            parts.append(f"The {h0.doc_title} of {_spoken_date(h0.doc_date)} records: {quoted}")
        elif _is_upload(h0):
            parts.append(f"An unverified uploaded document, '{h0.doc_title}', says: {quoted}")
        else:
            parts.append(f"The opposing party's {h0.doc_title} claims: {quoted}")
    return CaseAnswer(case_id=case.id, question=question, answer=mask_pii(" ".join(parts)), answerable=True,
                      mode="extractive", citations=[_cite(h) for h in top])


def answer_question(
    session: Session, case: Case, question: str, *, embedder: Embedder, llm=None, k: int = 5,
) -> CaseAnswer:
    question = question.strip()
    rule = screen_question(question)
    if rule is None and _other_case_mentioned(session, case, question):
        rule = "S4.cross_case"
    if rule:
        answer = CROSS_CASE_REFUSAL if rule == "S4.cross_case" else refusal_for(rule)
        return CaseAnswer(case_id=case.id, question=question, answer=answer, answerable=False,
                          mode="refused", citations=[], guard_rule=rule)
    hits = _restrict_to_case(session, case, with_neighbours(session, search(session, embedder, question, case_id=case.id, k=k)))
    if not hits or not _shares_content(question, hits):
        return CaseAnswer(case_id=case.id, question=question, answerable=False, mode="not_found", citations=[],
                          answer=NOT_FOUND)
    if llm is not None:
        passages = [
            {"id": h.chunk_id,
             "source": _source_label(h) + ("" if h.trusted else f" ({_untrusted_label(h)})"),
             "text": h.text}
            for h in hits
        ]
        try:
            out = llm.grounded_answer(question, passages)
            by_id = {h.chunk_id: h for h in hits}
            cited = [by_id[c] for c in out.get("citations", []) if c in by_id]
            answer = str(out.get("answer", "")).strip()
            answerable = bool(out.get("answerable", True))
            record = " ".join(h.text for h in hits)
            violations = check_output(answer, record=record)
            if violations and set(violations) <= {"pii.aadhaar", "pii.pan", "pii.phone"}:
                answer, violations = mask_pii(answer), []
            if answerable and (not cited or not answer or violations or injection_rule(answer)):
                raise ValueError(f"ungrounded or unsafe model output {violations}")
            if not answerable:
                return CaseAnswer(case_id=case.id, question=question, answer=answer or
                                  "The case file doesn't say.", answerable=False, mode="llm", citations=[])
            return CaseAnswer(case_id=case.id, question=question, answer=answer, answerable=True, mode="llm",
                              citations=[_cite(h) for h in cited])
        except Exception as exc:  # network, quota, bad JSON, failed validation
            logger.warning("LLM answer rejected, falling back to extractive: %s", exc)
    return _extractive(case, question, hits)
