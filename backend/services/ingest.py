"""Document ingestion: split into cited chunks, screen, embed, store.

Chunking
- Order sheets with several dated entries ("12.09.2026  Accused absent ...",
  "28.09.2026  Adjourned to ...") become one chunk per entry, numbered
  P1..Pn in date order of appearance, each with `hearing_date` set.
- Everything else (and single-date order sheets, like the seed data) is split
  on blank lines; an optional leading marker such as [P3] becomes the
  citation, unmarked paragraphs are numbered P1..Pn. Single-date order-sheet
  paragraphs inherit the document date as `hearing_date`.

Every chunk records `char_start`/`char_end` into `Document.text` (the body
after any [Pn] marker) and a deterministic header line used both as the
embedding title and for keyword search.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import numpy as np
from sqlalchemy import inspect
from sqlmodel import Session, select

from backend.models.cases import Case, Document
from backend.models.retrieval import Chunk
from backend.services.embeddings import Embedder
from backend.services.guard import looks_like_injection
from backend.services.legal_ids import find_dates

logger = logging.getLogger(__name__)

_MARKER = re.compile(r"^\s*\[(P\d+)\]\s*")
_BLOCK_SEP = re.compile(r"\n\s*\n")
# A dated order-sheet entry starts a line: "12.09.2026", "[P4] 12/09/2026", "Dt. 12-09-2026:"
_ENTRY_START = re.compile(
    r"^[ \t]*(?:\[(P\d+)\][ \t]*)?(?:(?:dt|date)\.?[: \t]*)?(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass
class Span:
    para: str
    text: str          # whitespace-normalised body
    start: int         # offsets into the original document text
    end: int
    hearing_date: date | None = None


def _clean(s: str) -> str:
    return " ".join(s.split())


def split_spans(text: str) -> list[Span]:
    """Blank-line paragraphs with [Pn] markers honoured and character offsets kept."""
    spans: list[Span] = []
    pos = 0
    blocks: list[tuple[int, int]] = []
    for m in _BLOCK_SEP.finditer(text):
        blocks.append((pos, m.start()))
        pos = m.end()
    blocks.append((pos, len(text)))
    n = 0
    for b_start, b_end in blocks:
        raw = text[b_start:b_end]
        if not raw.strip():
            continue
        n += 1
        lead = len(raw) - len(raw.lstrip())
        start = b_start + lead
        m = _MARKER.match(raw[lead:])
        para = m.group(1) if m else f"P{n}"
        if m:
            start += m.end()
        end = b_start + len(raw.rstrip())
        body = _clean(text[start:end])
        if body:
            spans.append(Span(para, body, start, end))
    return spans


def split_paragraphs(text: str) -> list[tuple[str, str]]:
    return [(s.para, s.text) for s in split_spans(text)]


def split_order_sheet(text: str, doc_date: date | None = None) -> list[Span]:
    """One span per dated entry; falls back to paragraphs when the sheet has no dated entry lines."""
    starts = list(_ENTRY_START.finditer(text))
    if not starts:
        return [Span(s.para, s.text, s.start, s.end, doc_date) for s in split_spans(text)]
    spans: list[Span] = []
    pre_end = starts[0].start()
    if text[:pre_end].strip():  # cause title / preamble before the first entry
        lead = len(text[:pre_end]) - len(text[:pre_end].lstrip())
        spans.append(Span("P0", _clean(text[:pre_end]), lead, len(text[:pre_end].rstrip()), doc_date))
    for i, m in enumerate(starts):
        seg_end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        start = m.start(2) if m.group(1) else m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
        end = m.start() + len(text[m.start():seg_end].rstrip())
        dates = find_dates(m.group(2))
        para = m.group(1) or f"P{i + 1}"
        spans.append(Span(para, _clean(text[start:end]), start, end, dates[0][2] if dates else doc_date))
    return spans


def chunk_document(doc: Document) -> list[Span]:
    if doc.kind == "order_sheet":
        return split_order_sheet(doc.text, doc.doc_date)
    return split_spans(doc.text)


def build_header(case: Case | None, doc: Document, span: Span) -> str:
    when = span.hearing_date or doc.doc_date
    parts = [case.case_number if case else doc.case_id, doc.title]
    if when:
        parts.append(when.strftime("%d %b %Y"))
    parts.append(span.para)
    return " · ".join(parts)


def ensure_chunk_schema(session: Session) -> bool:
    """Chunks are derived data: if the table predates the current columns, rebuild it. Returns True if rebuilt."""
    engine = session.get_bind()
    insp = inspect(engine)
    if not insp.has_table(Chunk.__tablename__):
        Chunk.__table__.create(engine)
        return True
    have = {c["name"] for c in insp.get_columns(Chunk.__tablename__)}
    want = {c.name for c in Chunk.__table__.columns}
    if want <= have:
        return False
    logger.info("Chunk table is missing %s; rebuilding the retrieval index.", sorted(want - have))
    session.commit()
    Chunk.__table__.drop(engine)
    Chunk.__table__.create(engine)
    return True


def ingest_document(session: Session, doc: Document, embedder: Embedder) -> list[Chunk]:
    for old in session.exec(select(Chunk).where(Chunk.doc_id == doc.id)).all():
        session.delete(old)
    spans = chunk_document(doc)
    if not spans:
        session.commit()
        return []
    case = session.get(Case, doc.case_id)
    headers = [build_header(case, doc, s) for s in spans]
    vectors = embedder.embed([s.text for s in spans], titles=headers)
    model, dim = embedder.model, int(vectors.shape[1]) if vectors.ndim == 2 else embedder.dim
    chunks = []
    seen: set[str] = set()
    for i, (span, header, vec) in enumerate(zip(spans, headers, vectors)):
        para = span.para if span.para not in seen else f"{span.para}-{i}"
        seen.add(para)
        chunk = Chunk(
            id=f"{doc.id}#{para}", doc_id=doc.id, case_id=doc.case_id, para=para, seq=i,
            doc_kind=doc.kind, hearing_date=span.hearing_date, header=header,
            char_start=span.start, char_end=span.end, text=span.text,
            trusted=doc.trusted, suspicious=looks_like_injection(span.text),
            embed_model=model, embed_dim=dim, embedding=np.asarray(vec, dtype=np.float32).tobytes(),
        )
        session.add(chunk)
        chunks.append(chunk)
    session.commit()
    return chunks


def ensure_index(session: Session, embedder: Embedder) -> int:
    """Embed any document with no chunks, or chunks from a different model or size. Returns docs indexed."""
    ensure_chunk_schema(session)
    done = 0
    for doc in session.exec(select(Document)).all():
        chunk = session.exec(select(Chunk).where(Chunk.doc_id == doc.id)).first()
        if chunk is None or chunk.embed_model != embedder.model or chunk.embed_dim != embedder.dim:
            ingest_document(session, doc, embedder)
            done += 1
    return done
