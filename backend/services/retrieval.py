"""Cited retrieval over a case file: intent router + hybrid (BM25 + vector) search.

search() keeps its original contract (list[Hit], best first) so qa.py and
the /search API work unchanged. Inside:

1. Intent router. Temporal questions ("next date", "last hearing", "why was
   it adjourned", "what happened on 12 September", "previous order") are
   answered from the structured record, not by similarity: the latest
   order-sheet entry by hearing date (or the entry on the date asked), plus
   approved timeline events that have no document yet. Dates are exact;
   embeddings are not.
2. Hybrid search for everything else: a BM25 ranking over word tokens plus
   canonical legal identifiers (pw1, sec138, cc1042_2025, d20260912; see
   legal_ids.py) and a cosine ranking over embeddings, fused with
   Reciprocal Rank Fusion. RRF needs no score calibration between the two
   lists (Supabase hybrid-search guide; Anthropic "Contextual Retrieval":
   embeddings + BM25 beat embeddings alone). If the query embedding fails
   (quota, network) search degrades to BM25 alone instead of erroring.

BM25 runs in Python over the case's chunks, which are already loaded for the
vector scan: a case is tens to hundreds of chunks, so this is sub-millisecond
and works the same on SQLite and Postgres. At larger scale swap in SQLite
FTS5 / Postgres tsvector behind the same function (docs/ARCHITECTURE_V2.md).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from datetime import date

import numpy as np
from sqlmodel import Session, col, select

from backend.models.cases import Case, Document, HearingEvent
from backend.models.retrieval import Chunk
from backend.services.embeddings import Embedder, tokens
from backend.services.legal_ids import bare_numbers, legal_tokens, parse_spoken_date, words_to_digits

logger = logging.getLogger(__name__)

# Small per-case corpora: a lower k than the usual 60 keeps a meaningful gap
# between rank 1 and rank 5 (with k=60 every score is ~equal).
RRF_K = 10
CANDIDATES = 20
BM25_K1, BM25_B = 1.2, 0.75


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    doc_title: str
    doc_date: date | None
    para: str | None
    text: str
    trusted: bool
    score: float
    hearing_date: date | None = None
    char_start: int | None = None
    char_end: int | None = None
    route: str = "hybrid"  # hybrid | keyword | timeline


# ---------------------------------------------------------------------------
# Intent router
# ---------------------------------------------------------------------------

_NEXT = re.compile(r"\b(next (date|hearing|posting|listing)|when is (it|the case|this) (listed|posted|next)|"
                   r"posted to when|adjourned to when|when('s| is) the next|next court date)\b", re.I)
_LAST = re.compile(r"\b(last (hearing|date|time|posting|order|sitting)|previous (hearing|date|order|posting)|"
                   r"why\b.{0,30}\badjourn\w*|reason for (the )?adjournment|"
                   r"what happened (last|in court last|on the last)|latest order|most recent (order|hearing))", re.I)
_ON_DATE = re.compile(r"\b(what happened|what was (done|ordered)|order|proceedings?|hearing)\b.{0,30}\bon\b", re.I)
# Native-script cues for the same questions (Sarvam returns Tamil / Devanagari text)
_NEXT_NATIVE = re.compile(r"(அடுத்த (தேதி|ஹியரிங்|விசாரணை)|अगली (तारीख|तारीख़|सुनवाई|डेट))")
_LAST_NATIVE = re.compile(r"(போன (தடவை|முறை|ஹியரிங்)|கடைசி (ஹியரிங்|விசாரணை)|ஏன் (ஒத்திவைக்க|அட்ஜர்ன்)|"
                          r"पिछली (तारीख|तारीख़|सुनवाई|बार)|क्यों (स्थगित|एडजर्न))")


def classify_intent(query: str, today: date | None = None) -> tuple[str, date | None]:
    """'next_date' | 'last_hearing' | 'on_date' | 'search', with the date asked about for on_date."""
    if _NEXT.search(query) or _NEXT_NATIVE.search(query):
        return "next_date", None
    if _LAST.search(query) or _LAST_NATIVE.search(query):
        return "last_hearing", None
    if _ON_DATE.search(query) or re.search(r"\bon\b", query, re.I):
        d = parse_spoken_date(query, today)
        if d:
            return "on_date", d
    return "search", None


def _hit(c: Chunk, docs: dict[str, Document], score: float, route: str) -> Hit:
    d = docs.get(c.doc_id)
    return Hit(c.id, c.doc_id, d.title if d else c.doc_id, d.doc_date if d else None, c.para, c.text, c.trusted,
               score, c.hearing_date, c.char_start, c.char_end, route)


def _docs_for(session: Session, chunks: list[Chunk]) -> dict[str, Document]:
    ids = {c.doc_id for c in chunks}
    if not ids:
        return {}
    return {d.id: d for d in session.exec(select(Document).where(col(Document.id).in_(ids))).all()}


_ADJOURN = re.compile(r"\b(adjourned to|call on|list(ed)? on|posted to|stands over to|re-?list)\b", re.I)


def route_temporal(session: Session, case_id: str, intent: str, on: date | None = None,
                   today: date | None = None) -> list[Hit]:
    """Hits for a temporal question, from order-sheet entries by hearing date and the timeline."""
    today = today or date.today()
    sheet = session.exec(
        select(Chunk).where(Chunk.case_id == case_id, Chunk.doc_kind == "order_sheet",
                            Chunk.suspicious == False, Chunk.hearing_date != None)  # noqa: E711,E712
    ).all()
    past = [c for c in sheet if c.hearing_date <= today]
    if intent == "on_date":
        chosen = [c for c in sheet if c.hearing_date == on]
    else:
        latest = max((c.hearing_date for c in past), default=None)
        chosen = [c for c in past if c.hearing_date == latest] if latest else []
    chosen.sort(key=lambda c: c.seq)
    if intent == "next_date":  # the entry that fixes the next date first
        chosen.sort(key=lambda c: (0 if _ADJOURN.search(c.text) else 1, c.seq))
    docs = _docs_for(session, chosen)
    hits = [_hit(c, docs, 1.0 - 0.05 * i, "timeline") for i, c in enumerate(chosen)]

    # Approved hearing updates (dictations) land in the timeline before any
    # order sheet is uploaded. If one is newer than the latest sheet entry,
    # it is the record of the last hearing.
    newest_sheet = max((c.hearing_date for c in chosen), default=None)
    events = session.exec(
        select(HearingEvent).where(HearingEvent.case_id == case_id, HearingEvent.kind.in_(("hearing", "order", "dictation")))
        .order_by(col(HearingEvent.event_date).desc())
    ).all()
    for e in events:
        if intent == "on_date" and e.event_date != on:
            continue
        if intent != "on_date" and (e.event_date > today or (newest_sheet and e.event_date <= newest_sheet)):
            continue
        if e.source_doc_id and any(h.doc_id == e.source_doc_id for h in hits):
            continue
        text = e.outcome + (f" Next date: {e.next_date:%d.%m.%Y}." if e.next_date else "")
        hits.insert(0, Hit(f"timeline#{e.id}", e.source_doc_id or f"{case_id}-TIMELINE", "Case diary (approved update)",
                           e.event_date, None, text, True, 1.0, e.event_date, None, None, "timeline"))
        break
    if intent == "next_date":
        case = session.get(Case, case_id)
        if case and case.next_hearing and not hits:
            purpose = case.next_hearing_purpose or case.stage
            hits.append(Hit(f"case#{case_id}", f"{case_id}-TIMELINE", "Case diary", None, None,
                            f"Next hearing on {case.next_hearing:%d.%m.%Y} for {purpose}.", True, 1.0,
                            None, None, None, "timeline"))
    return hits


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------

@lru_cache(maxsize=20000)
def _index_terms_cached(text: str, header: str) -> tuple[str, ...]:
    return tuple(tokens(words_to_digits(text)) + legal_tokens(f"{header} {text}" if header else text))


def index_terms(text: str, header: str = "") -> list[str]:
    """Tokens for keyword search: the chunk's words plus legal identifiers from chunk and header.

    Header *words* (the document title, repeated on every chunk of a document)
    are left out so they don't swamp rare terms like '317'; the header's
    identifiers (case number, hearing date) are kept so 'order of 12 September'
    or 'C.C. 1042/2025' still match.
    """
    return list(_index_terms_cached(text, header))


def query_terms(query: str) -> list[str]:
    q = words_to_digits(query)
    return tokens(q) + legal_tokens(query) + bare_numbers(query)


def bm25_scores(docs_terms: list[list[str]], q_terms: list[str]) -> np.ndarray:
    n = len(docs_terms)
    scores = np.zeros(n, dtype=np.float32)
    if not n or not q_terms:
        return scores
    avgdl = sum(len(d) for d in docs_terms) / n or 1.0
    df: Counter[str] = Counter()
    for d in docs_terms:
        df.update(set(d))
    tfs = [Counter(d) for d in docs_terms]
    for term in set(q_terms):
        if term not in df:
            continue
        idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
        for i, tf in enumerate(tfs):
            f = tf.get(term, 0)
            if f:
                dl = len(docs_terms[i])
                scores[i] += idf * f * (BM25_K1 + 1) / (f + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
    return scores


def rrf(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion, normalised so rank 1 in every list = 1.0."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank)
    top = len([r for r in rankings if r]) / (k + 1) or 1.0
    return {i: s / top for i, s in fused.items()}


def hybrid_search(session: Session, embedder: Embedder, query: str, *, case_id: str | None = None,
                  k: int = 5, min_score: float = 0.05) -> list[Hit]:
    stmt = select(Chunk).where(Chunk.suspicious == False)  # noqa: E712
    if case_id:
        stmt = stmt.where(Chunk.case_id == case_id)
    chunks = session.exec(stmt).all()
    if not chunks:
        return []

    # keyword ranking
    kw = bm25_scores([index_terms(c.text, c.header) for c in chunks], query_terms(query))
    kw_rank = [int(i) for i in np.argsort(-kw) if kw[i] > 0][:CANDIDATES]

    # vector ranking (only chunks embedded by this embedder); degrade gracefully
    vec_rank: list[int] = []
    cos = np.zeros(len(chunks), dtype=np.float32)
    usable = [i for i, c in enumerate(chunks) if c.embed_model == embedder.model and c.embed_dim in (0, embedder.dim)]
    if usable:
        try:
            q = embedder.embed([query], is_query=True)[0]
            matrix = np.vstack([np.frombuffer(chunks[i].embedding, dtype=np.float32) for i in usable])
            if matrix.shape[1] == q.shape[0]:
                cos[usable] = matrix @ q
                vec_rank = [usable[int(j)] for j in np.argsort(-(matrix @ q))
                            if cos[usable[int(j)]] >= min_score][:CANDIDATES]
        except Exception as exc:  # quota, network, model change: keyword search still answers
            logger.warning("Query embedding failed (%s); keyword-only retrieval.", exc)

    fused = rrf([vec_rank, kw_rank])
    route = "hybrid" if vec_rank and kw_rank else ("keyword" if kw_rank else "hybrid")
    order = sorted(fused, key=lambda i: -fused[i])[:k]
    picked = [chunks[i] for i in order]
    docs = _docs_for(session, picked)
    return [_hit(chunks[i], docs, fused[i], route) for i in order if fused[i] >= min_score]


def search(
    session: Session, embedder: Embedder, query: str, *, case_id: str | None = None,
    k: int = 5, min_score: float = 0.05, today: date | None = None,
) -> list[Hit]:
    if case_id:
        intent, on = classify_intent(query, today)
        if intent != "search":
            hits = route_temporal(session, case_id, intent, on, today)
            if hits:
                return hits[: max(k, len(hits))]
    return hybrid_search(session, embedder, query, case_id=case_id, k=k, min_score=min_score)


def with_neighbours(session: Session, hits: list[Hit], top_n: int = 1) -> list[Hit]:
    """Add the chunks either side of the best hits from the same document.

    Order sheets read as a sequence ("accused absent" / "last chance" /
    "adjourned to"), so a single paragraph often lacks the reason.
    """
    seen = {h.chunk_id for h in hits}
    extra: list[Hit] = []
    for h in hits[:top_n]:
        c0 = session.get(Chunk, h.chunk_id)
        if c0 is None:
            continue
        near = session.exec(
            select(Chunk).where(Chunk.doc_id == c0.doc_id, Chunk.seq.in_((c0.seq - 1, c0.seq + 1)))
        ).all()
        for c in sorted(near, key=lambda c: c.seq):
            if c.id in seen or c.suspicious:
                continue
            seen.add(c.id)
            extra.append(Hit(c.id, c.doc_id, h.doc_title, h.doc_date, c.para, c.text, c.trusted, h.score * 0.9,
                             c.hearing_date, c.char_start, c.char_end, h.route))
    return hits[:top_n] + extra + hits[top_n:]
