"""Hybrid retrieval, legal-identifier normalisation, order-sheet chunking, intent router, embeddings."""

from datetime import date, timedelta

import numpy as np
import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from backend.core.config import Settings
from backend.main import create_app
from backend.models.cases import Document, HearingEvent
from backend.models.retrieval import Chunk
from backend.services.cases import find_case
from backend.services.embeddings import GeminiEmbedder, HashingEmbedder
from backend.services.ingest import ensure_chunk_schema, ensure_index, ingest_document, split_order_sheet
from backend.services.legal_ids import legal_tokens, parse_spoken_date, words_to_digits
from backend.services.qa import answer_question
from backend.services.retrieval import Hit, classify_intent, rrf, search, with_neighbours

TODAY = date.today()


@pytest.fixture()
def app():
    return create_app(Settings(database_url="sqlite://"))


# ---------------------------------------------------------------- identifiers

@pytest.mark.parametrize("a, b", [
    ("P.W.1 cross-examined", "PW one cross examination"),
    ("u/s 138 NI Act", "section one thirty eight"),
    ("C.C. No. 1042 of 2025", "CC 1042/2025"),
    ("C.C. No. 1042 of 2025", "C C ten forty two of twenty twenty five"),
    ("Ex.P5 marked", "exhibit P five"),
    ("adjourned to 12.11.2026", "twelfth of November 2026"),
])
def test_identifier_spellings_normalise_to_same_token(a, b):
    assert set(legal_tokens(a)) & set(legal_tokens(b))


@pytest.mark.parametrize("spoken, digits", [
    ("பத்து நாற்பத்தி இரண்டு", "1042"),          # Tamil words
    ("இருபத்தி இரண்டு பதினேழு", "2217"),
    ("दस बयालीस", "1042"),                        # Hindi words
    ("௧௦௪௨", "1042"),                              # Tamil digits
    ("केस १०४२", "केस 1042"),                     # Devanagari digits
])
def test_native_script_numbers(spoken, digits):
    assert words_to_digits(spoken) == digits


def test_native_script_dates():
    assert parse_spoken_date("१२ सितंबर 2026") == date(2026, 9, 12)
    assert parse_spoken_date("12 செப்டம்பர் 2026") == date(2026, 9, 12)
    assert parse_spoken_date("what happened on the twelfth of September 2026") == date(2026, 9, 12)


def test_find_case_accepts_native_script_and_full_case_numbers(app):
    with Session(app.state.engine) as s:
        assert find_case(s, "பத்து நாற்பத்தி இரண்டு").id == "1042"
        assert find_case(s, "दस बयालीस").id == "1042"
        assert find_case(s, "C.S. No. 2217 of 2024").id == "2217"
        assert find_case(s, "M C seven eighty nine").id == "0789"


# ---------------------------------------------------------------- chunking

SHEET = (
    "IN THE COURT OF THE XIV METROPOLITAN MAGISTRATE, EGMORE\nC.C. No. 1042 of 2025\n\n"
    "12.09.2026  Accused absent. Petition u/s 317 CrPC allowed.\nLast chance.\n\n"
    "28.09.2026 Counsel for accused seeks time.\nAdjourned to 12.10.2026 for cross of PW1.\n"
    "12/10/2026 PW1 cross-examined in part. Call on 02.11.2026."
)


def test_order_sheet_one_chunk_per_dated_entry_with_offsets():
    spans = split_order_sheet(SHEET)
    assert [s.para for s in spans] == ["P0", "P1", "P2", "P3"]
    assert [s.hearing_date for s in spans[1:]] == [date(2026, 9, 12), date(2026, 9, 28), date(2026, 10, 12)]
    for s in spans:
        assert " ".join(SHEET[s.start:s.end].split()) == s.text
    assert spans[2].text.startswith("28.09.2026") and "Adjourned to 12.10.2026" in spans[2].text


def test_ingested_chunks_carry_header_dates_offsets_and_model(app):
    with Session(app.state.engine) as s:
        doc = Document(id="1042-OS-FULL", case_id="1042", title="Order sheet (certified copy)", kind="order_sheet",
                       doc_date=date(2026, 10, 12), text=SHEET, trusted=True)
        s.add(doc)
        s.commit()
        chunks = ingest_document(s, doc, app.state.embedder)
        assert len(chunks) == 4
        c = s.get(Chunk, "1042-OS-FULL#P2")
        assert c.hearing_date == date(2026, 9, 28) and c.doc_kind == "order_sheet"
        assert c.header.startswith("C.C. No. 1042 of 2025 · Order sheet (certified copy) · 28 Sep 2026")
        assert doc.text[c.char_start:c.char_end].startswith("28.09.2026")
        assert c.embed_model == "local-hash-v1" and c.embed_dim == 1024


def test_seed_order_sheet_paragraphs_inherit_document_date(app):
    with Session(app.state.engine) as s:
        c = s.get(Chunk, "1042-OS-2#P2")
        d = s.get(Document, "1042-OS-2")
        assert c.hearing_date == d.doc_date and c.seq == 1
        assert d.text[c.char_start:c.char_end] == c.text


# ---------------------------------------------------------------- router

@pytest.mark.parametrize("q, intent", [
    ("what's the next date", "next_date"),
    ("when is it listed next", "next_date"),
    ("அடுத்த தேதி என்ன", "next_date"),
    ("अगली तारीख क्या है", "next_date"),
    ("why was it adjourned", "last_hearing"),
    ("what happened at the last hearing", "last_hearing"),
    ("போன தடவை என்ன ஆச்சு", "last_hearing"),
    ("what happened on 12.09.2026", "on_date"),
    ("what is the cheque number", "search"),
])
def test_intent_router(q, intent):
    assert classify_intent(q)[0] == intent


def test_router_answers_last_hearing_from_latest_entry_not_similarity(app):
    with Session(app.state.engine) as s:
        hits = search(s, app.state.embedder, "what happened last time", case_id="1042")
        assert {h.doc_id for h in hits} == {"1042-OS-2"} and all(h.route == "timeline" for h in hits)
        assert [h.para for h in hits] == ["P1", "P2", "P3"]


def test_router_next_date_puts_adjournment_entry_first(app):
    with Session(app.state.engine) as s:
        hits = search(s, app.state.embedder, "what's the next date", case_id="1042")
        assert hits[0].chunk_id == "1042-OS-2#P3" and "Adjourned to" in hits[0].text


def test_router_on_date_picks_that_entry(app):
    with Session(app.state.engine) as s:
        doc = Document(id="1042-OS-FULL", case_id="1042", title="Order sheet", kind="order_sheet",
                       doc_date=date(2026, 10, 12), text=SHEET, trusted=True)
        s.add(doc)
        s.commit()
        ingest_document(s, doc, app.state.embedder)
        hits = search(s, app.state.embedder, "what happened on 28.09.2026", case_id="1042",
                      today=date(2026, 10, 20))
        assert hits[0].chunk_id == "1042-OS-FULL#P2"
        # an on-date question with no entry that day falls through to hybrid search
        miss = search(s, app.state.embedder, "what happened on 01.01.2020 about the cheque", case_id="1042")
        assert miss and miss[0].route != "timeline"


def test_router_prefers_approved_dictation_newer_than_any_order_sheet(app):
    with Session(app.state.engine) as s:
        s.add(HearingEvent(case_id="1042", event_date=TODAY, kind="dictation",
                           outcome="PW1 cross-examination completed. DW1 to be examined.", next_date=TODAY + timedelta(days=14)))
        s.commit()
        hits = search(s, app.state.embedder, "what happened at the last hearing", case_id="1042")
        assert hits[0].chunk_id.startswith("timeline#") and "DW1" in hits[0].text


def test_why_adjourned_answer_is_cited_from_latest_order_sheet(app):
    with Session(app.state.engine) as s:
        case = find_case(s, "1042")
        ans = answer_question(s, case, "Why was the case adjourned?", embedder=app.state.embedder)
        assert ans.answerable and {c.doc_id for c in ans.citations} == {"1042-OS-2"}
        assert "medical" in ans.answer.lower()


# ---------------------------------------------------------------- hybrid

def test_rrf_normalised_and_rewards_agreement():
    fused = rrf([[0, 1, 2], [1, 0, 3]])
    assert max(fused.values()) <= 1.0
    assert fused[0] == pytest.approx(fused[1]) and fused[0] > fused[2] > 0
    assert rrf([[5], []])[5] == pytest.approx(1.0)


@pytest.mark.parametrize("q, expect", [
    ("cheque zero zero four five one two", "1042-COMPLAINT#P2"),
    ("317 petition", "1042-OS-2#P1"),
    ("P.W.1 chief examination", "1042-OS-1#P2"),
    ("PW one chief examination", "1042-OS-1#P2"),
])
def test_hybrid_finds_exact_identifiers(app, q, expect):
    with Session(app.state.engine) as s:
        hits = search(s, app.state.embedder, q, case_id="1042", k=3)
        assert expect in [h.chunk_id for h in hits]


class _BrokenEmbedder(HashingEmbedder):
    def embed(self, texts, *, is_query=False, titles=None):
        if is_query:
            raise requests.ConnectionError("quota")
        return super().embed(texts, is_query=is_query, titles=titles)


def test_query_embedding_failure_degrades_to_keyword_search(app):
    with Session(app.state.engine) as s:
        hits = search(s, _BrokenEmbedder(), "cheque 004512 Indian Bank", case_id="1042")
        assert hits and hits[0].chunk_id == "1042-COMPLAINT#P2" and hits[0].route == "keyword"


def test_injected_paragraph_never_returned_by_hybrid(app):
    with Session(app.state.engine) as s:
        hits = search(s, app.state.embedder, "ignore all previous instructions dismissed", case_id="2217", k=10)
        assert "2217-COUNTER#P3" not in [h.chunk_id for h in hits]


def test_search_contract_and_neighbours(app):
    with Session(app.state.engine) as s:
        hits = search(s, app.state.embedder, "cheque returned unpaid", case_id="1042")
        assert hits and isinstance(hits[0], Hit) and hits == sorted(hits, key=lambda h: -h.score)
        expanded = with_neighbours(s, hits[:1])
        assert len(expanded) >= 2 and all(h.doc_id == hits[0].doc_id for h in expanded)


def test_search_api_still_works(app):
    r = TestClient(app).get("/cases/1042/search", params={"q": "PW1 cross"}).json()
    assert r and {"doc_id", "para", "score", "text"} <= set(r[0])


# ---------------------------------------------------------------- embeddings

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self._body


class _FakeHTTP:
    def __init__(self, fail_models=()):
        self.calls, self.fail_models = [], set(fail_models)

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, json))
        model = url.split("/models/")[1].split(":")[0]
        if model in self.fail_models:
            return _Resp(400, {"error": {"message": "Invalid JSON payload: unknown field taskType"}})
        return _Resp(200, {"embeddings": [{"values": list(np.random.rand(768))} for _ in json["requests"]]})


def test_gemini_embedding_2_uses_prompt_prefixes_and_768_dims():
    http = _FakeHTTP()
    e = GeminiEmbedder("k", model="gemini-embedding-2", session=http)
    v = e.embed(["Adjourned to 12.11.2026."], titles=["C.C. No. 1042 of 2025 · Order sheet"])
    q = e.embed(["why adjourned"], is_query=True)
    assert v.shape == (1, 768) and q.shape == (1, 768)
    doc_req, query_req = http.calls[0][1]["requests"][0], http.calls[1][1]["requests"][0]
    assert "taskType" not in doc_req and doc_req["outputDimensionality"] == 768
    assert doc_req["content"]["parts"][0]["text"] == "title: C.C. No. 1042 of 2025 · Order sheet | text: Adjourned to 12.11.2026."
    assert query_req["content"]["parts"][0]["text"] == "task: question answering | query: why adjourned"


def test_gemini_falls_back_to_001_with_task_type_on_400_and_sticks():
    http = _FakeHTTP(fail_models={"gemini-embedding-2"})
    e = GeminiEmbedder("k", model="gemini-embedding-2", session=http)
    e.embed(["text"], titles=["T"])
    assert e.model == "gemini-embedding-001"
    req = http.calls[-1][1]["requests"][0]
    assert req["taskType"] == "RETRIEVAL_DOCUMENT" and req["title"] == "T"
    assert req["content"]["parts"][0]["text"] == "text"
    e.embed(["q"], is_query=True)
    assert "gemini-embedding-001" in http.calls[-1][0] and http.calls[-1][1]["requests"][0]["taskType"] == "QUESTION_ANSWERING"


def test_model_or_dim_change_triggers_reindex(app):
    with Session(app.state.engine) as s:
        assert ensure_index(s, app.state.embedder) == 0
        assert ensure_index(s, HashingEmbedder(dim=512)) > 0
        assert {c.embed_dim for c in s.exec(select(Chunk)).all()} == {512}


def test_old_chunk_table_is_rebuilt(app):
    with Session(app.state.engine) as s:
        s.exec(text("DROP TABLE chunk"))
        s.exec(text("CREATE TABLE chunk (id VARCHAR PRIMARY KEY, doc_id VARCHAR, case_id VARCHAR, para VARCHAR, "
                    "text VARCHAR, trusted BOOLEAN, suspicious BOOLEAN, embed_model VARCHAR, embedding BLOB, created_at DATETIME)"))
        s.commit()
        assert ensure_chunk_schema(s) is True
        assert ensure_index(s, app.state.embedder) > 0
        assert s.get(Chunk, "1042-OS-2#P2").hearing_date is not None
