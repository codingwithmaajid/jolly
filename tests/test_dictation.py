from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.main import create_app
from backend.services.dictation import find_dates

SETTINGS = Settings(database_url="sqlite://")
NOTE = (
    "Accused counsel sought time to file the reply, granted. "
    "Adjourned to 14 November for cross-examination of PW1. "
    "Prepare cross questions on the dishonour memo. Collect the bank return memo original from the client."
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(SETTINGS))


def test_find_dates_resolves_year_forward() -> None:
    ref = date(2026, 12, 20)
    assert find_dates("posted to 5th January", ref) == [date(2027, 1, 5)]
    assert find_dates("listed on Nov 14, 2026", ref) == [date(2026, 11, 14)]


def test_dictation_creates_pending_draft_without_touching_case(client: TestClient) -> None:
    before = client.get("/cases/1042").json()
    timeline_before = len(client.get("/cases/1042/timeline").json())
    r = client.post("/cases/1042/dictations", json={"text": NOTE, "hearing_date": "2026-10-21"})
    assert r.status_code == 201
    d = r.json()
    assert d["status"] == "pending"
    assert d["next_date"] == "2026-11-14"
    assert "cross-examination of PW1" in (d["next_purpose"] or "")
    assert len(d["tasks"]) == 2
    assert d["send_link"] is None  # nothing sendable before approval
    assert d["extractor"] == "rules"
    assert client.get("/cases/1042").json()["next_hearing"] == before["next_hearing"]
    assert len(client.get("/cases/1042/timeline").json()) == timeline_before
    assert [u["id"] for u in client.get("/updates?status=pending").json()] == [d["id"]]


def test_approve_commits_to_record(client: TestClient) -> None:
    d = client.post("/cases/1042/dictations", json={"text": NOTE, "hearing_date": "2026-10-21"}).json()
    tasks_before = len(client.get("/cases/1042/tasks").json())
    assert client.post(f"/updates/{d['id']}/approve").status_code == 401  # no signed token
    r = client.post(f"/updates/{d['id']}/approve", headers={"X-Approval-Token": d["approval_token"]})
    assert r.status_code == 200
    a = r.json()
    assert a["status"] == "approved"
    assert a["send_link"].startswith("https://wa.me/919800000101?text=")
    case = client.get("/cases/1042").json()
    assert case["next_hearing"] == "2026-11-14"
    assert client.get("/cases/1042/timeline").json()[-1]["kind"] == "dictation"
    assert len(client.get("/cases/1042/tasks").json()) == tasks_before + 2
    assert client.post(f"/updates/{d['id']}/approve", headers={"X-Approval-Token": d["approval_token"]}).status_code == 409
    m = client.get("/updates/metrics").json()
    assert m["approved"] == 1 and m["approved_unedited"] == 1


def test_approve_with_edits_counts_as_edited(client: TestClient) -> None:
    d = client.post("/cases/2217/dictations", json={"text": NOTE, "hearing_date": "2026-10-21"}).json()
    r = client.post(d["approve_url"], json={"next_date": "2026-11-20", "tasks": []})
    assert r.json()["next_date"] == "2026-11-20"
    assert client.get("/cases/2217").json()["next_hearing"] == "2026-11-20"
    assert client.get("/updates/metrics").json()["approved_unedited"] == 0


def test_reject_leaves_record_alone(client: TestClient) -> None:
    before = client.get("/cases/0789").json()["next_hearing"]
    d = client.post("/cases/0789/dictations", json={"text": NOTE, "hearing_date": "2026-10-21"}).json()
    assert client.post(d["reject_url"]).json()["status"] == "rejected"
    assert client.get("/cases/0789").json()["next_hearing"] == before


def test_bolna_log_hearing_update_tool(client: TestClient) -> None:
    r = client.post("/bolna/tools/log-hearing-update", json={"case_id": "ten forty two", "dictation": NOTE})
    body = r.json()
    assert body["found"] is True and body["update_id"]
    assert "approve" in body["spoken_confirmation"].lower()
    assert client.get(f"/updates/{body['update_id']}").json()["source"] == "voice"
    miss = client.post("/bolna/tools/log-hearing-update", json={"case_id": "9999", "dictation": NOTE}).json()
    assert miss["found"] is False


def test_unknown_case_404(client: TestClient) -> None:
    assert client.post("/cases/9999/dictations", json={"text": NOTE}).status_code == 404
