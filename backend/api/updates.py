"""Post-hearing dictation and the one-tap approval queue.

- POST /cases/{id}/dictations   dictation text -> pending draft
- GET  /updates?status=pending  approval queue (all cases)
- GET  /cases/{id}/updates      drafts for one case
- GET  /updates/{id}            one draft
- POST /updates/{id}/approve    optional edits -> commits to the case record
- POST /updates/{id}/reject     discard the draft
  Both need the signed, expiring, single-use approval token from the update
  view (X-Approval-Token header or ?token=). Every decision is audited.
- GET  /updates/metrics         share of drafts approved without edits
"""

from datetime import date

import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from backend.core.db import get_session
from backend.core.security import TokenError, verify_approval_token
from backend.models.cases import HearingUpdate
from backend.models.trust import SpentApprovalToken
from backend.services import audit
from backend.services.cases import find_case
from backend.services.dictation import UpdateEdits, UpdateView, approve, create_update, reject, view

router = APIRouter(tags=["Hearing updates"])

# Edited-or-not per approved draft, kept in memory for the demo metric.
_EDITED: dict[int, bool] = {}


class DictationIn(BaseModel):
    text: str = Field(min_length=5, max_length=5000)
    hearing_date: date | None = None


class Metrics(BaseModel):
    pending: int
    approved: int
    rejected: int
    approved_unedited: int
    approved_unedited_share: float | None


def _update_or_404(session: Session, update_id: int) -> HearingUpdate:
    upd = session.get(HearingUpdate, update_id)
    if upd is None:
        raise HTTPException(status_code=404, detail=f"Update {update_id} not found.")
    return upd


@router.post("/cases/{case_id}/dictations", response_model=UpdateView, status_code=201)
def dictate(case_id: str, body: DictationIn, request: Request, session: Session = Depends(get_session)) -> UpdateView:
    case = find_case(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found.")
    upd = create_update(session, case, body.text, hearing_date=body.hearing_date, llm=request.app.state.llm)
    return view(session, upd, request.app.state.settings)


@router.get("/updates/metrics", response_model=Metrics)
def metrics(session: Session = Depends(get_session)) -> Metrics:
    rows = session.exec(select(HearingUpdate)).all()
    counts = {s: sum(1 for r in rows if r.status == s) for s in ("pending", "approved", "rejected")}
    unedited = sum(1 for r in rows if r.status == "approved" and not _EDITED.get(r.id, False))
    share = round(unedited / counts["approved"], 2) if counts["approved"] else None
    return Metrics(**counts, approved_unedited=unedited, approved_unedited_share=share)


@router.get("/updates", response_model=list[UpdateView])
def list_updates(request: Request, status: str | None = Query(default=None, pattern="^(pending|approved|rejected)$"),
                 session: Session = Depends(get_session)) -> list[UpdateView]:
    q = select(HearingUpdate).order_by(col(HearingUpdate.created_at).desc())
    if status:
        q = q.where(HearingUpdate.status == status)
    return [view(session, u, request.app.state.settings) for u in session.exec(q).all()]


@router.get("/cases/{case_id}/updates", response_model=list[UpdateView])
def case_updates(case_id: str, request: Request, session: Session = Depends(get_session)) -> list[UpdateView]:
    case = find_case(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found.")
    q = select(HearingUpdate).where(HearingUpdate.case_id == case.id).order_by(col(HearingUpdate.created_at).desc())
    return [view(session, u, request.app.state.settings) for u in session.exec(q).all()]


@router.get("/updates/{update_id}", response_model=UpdateView)
def get_update(update_id: int, request: Request, session: Session = Depends(get_session)) -> UpdateView:
    return view(session, _update_or_404(session, update_id), request.app.state.settings)


def _authorise(request: Request, session: Session, update_id: int, action: str,
               header_token: str | None, query_token: str | None) -> tuple[HearingUpdate, str]:
    """Verify the approval token, then the update's state. Returns (update, nonce)."""
    t0 = time.perf_counter()
    settings = request.app.state.settings

    def deny(code: int, detail: str, rule: str) -> HTTPException:
        audit.record(request.app.state.engine, action=action, actor_type="advocate", decision="denied",
                     guard_rule=rule, status_code=code, args={"update_id": update_id},
                     latency_ms=(time.perf_counter() - t0) * 1000)
        return HTTPException(status_code=code, detail=detail)

    try:
        claims = verify_approval_token(settings, header_token or query_token, update_id)
    except TokenError as exc:
        raise deny(401, str(exc), "approval.token_invalid") from exc
    upd = session.get(HearingUpdate, update_id)
    if upd is None:
        raise deny(404, f"Update {update_id} not found.", "approval.not_found")
    if upd.status != "pending":
        raise deny(409, f"Update {update_id} is already {upd.status}.", "approval.not_pending")
    if session.get(SpentApprovalToken, claims["n"]) is not None:
        raise deny(401, "Approval token already used.", "approval.token_reused")
    return upd, claims["n"]


def _spend(session: Session, nonce: str, update_id: int, action: str) -> None:
    session.add(SpentApprovalToken(nonce=nonce, update_id=update_id, action=action))
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=401, detail="Approval token already used.") from exc


@router.post("/updates/{update_id}/approve", response_model=UpdateView)
def approve_update(update_id: int, request: Request, edits: UpdateEdits | None = None,
                   token: str | None = Query(default=None), x_approval_token: str | None = Header(default=None),
                   session: Session = Depends(get_session)) -> UpdateView:
    t0 = time.perf_counter()
    upd, nonce = _authorise(request, session, update_id, "approve", x_approval_token, token)
    _spend(session, nonce, upd.id, "approve")  # committed together with the approval
    _EDITED[upd.id] = approve(session, upd, edits)
    audit.record(request.app.state.engine, action="approve", actor_type="advocate", decision="allowed",
                 case_id=upd.case_id, args={"update_id": upd.id, "edited": _EDITED[upd.id]}, status_code=200,
                 latency_ms=(time.perf_counter() - t0) * 1000)
    return view(session, upd, request.app.state.settings)


@router.post("/updates/{update_id}/reject", response_model=UpdateView)
def reject_update(update_id: int, request: Request, token: str | None = Query(default=None),
                  x_approval_token: str | None = Header(default=None),
                  session: Session = Depends(get_session)) -> UpdateView:
    t0 = time.perf_counter()
    upd, nonce = _authorise(request, session, update_id, "reject", x_approval_token, token)
    _spend(session, nonce, upd.id, "reject")
    reject(session, upd)
    audit.record(request.app.state.engine, action="reject", actor_type="advocate", decision="allowed",
                 case_id=upd.case_id, args={"update_id": upd.id}, status_code=200,
                 latency_ms=(time.perf_counter() - t0) * 1000)
    return view(session, upd, request.app.state.settings)
