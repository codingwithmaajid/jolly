"""Call placement and status routes, backed by Bolna.

Route handlers stay thin: validation + orchestration only. Integration
details live in `backend.services.bolna_client`.
"""

import re
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlmodel import Session

from backend.api.audit import AuditedRoute
from backend.core.db import get_session
from backend.core.security import hash_identifier, require_secret
from backend.services.cases import build_daily_brief, morning_call_user_data

from backend.models.calls import CallRequest, CallResponse, CallStatusResponse
from backend.services.bolna_client import TERMINAL_STATUSES, BolnaClient

router = APIRouter(tags=["Calls"], route_class=AuditedRoute)

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def _get_client(request: Request) -> BolnaClient:
    return BolnaClient(request.app.state.settings)


def _check_allowed(request: Request, phone: str) -> None:
    """CALL_ALLOWED_NUMBERS (comma-separated E.164) stops a public page from
    making Jolly ring arbitrary numbers on our Bolna credit. Unset = open."""
    allowed = request.app.state.settings.call_allowed_numbers
    if allowed and phone not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="This number is not on Jolly's demo allowlist.")


@router.post("/call", response_model=CallResponse, status_code=status.HTTP_200_OK)
async def trigger_call(payload: CallRequest, request: Request) -> CallResponse:
    phone = re.sub(r"[\s-]", "", payload.phone_number or "")

    if not E164.match(phone):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A valid phone number in E.164 format (e.g. +919876543210) is required.",
        )

    request.state.audit["actor_ref"] = hash_identifier(request.app.state.settings, phone)
    _check_allowed(request, phone)
    client = _get_client(request)
    data = client.place_call(phone, agent_id=payload.agent_id, user_data=payload.user_data)

    return CallResponse(
        status=str(data.get("status", "queued")),
        message=f"Jolly is calling {phone}",
        call_id=data.get("execution_id"),
    )


@router.get(
    "/call/{call_id}",
    response_model=CallStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def get_call_status(call_id: str, request: Request) -> CallStatusResponse:
    client = _get_client(request)
    data = client.get_execution(call_id)
    call_status = str(data.get("status", "unknown"))
    telephony = data.get("telephony_data") or {}

    return CallStatusResponse(
        call_id=str(data.get("id", call_id)),
        status=call_status,
        is_terminal=call_status in TERMINAL_STATUSES,
        duration_seconds=data.get("conversation_duration"),
        transcript=data.get("transcript"),
        summary=data.get("summary"),
        recording_url=telephony.get("recording_url"),
        error_message=data.get("error_message"),
    )


class MorningCallResponse(CallResponse):
    matters: int
    spoken_summary: str


@router.post("/briefs/morning-call", response_model=MorningCallResponse)
def morning_call(
    request: Request,
    phone_number: str = Query(..., description="Advocate's phone, E.164"),
    advocate: str | None = None,
    language: str = Query(default="en", pattern="^(en|ta|hi)$"),
    x_bolna_secret: str | None = Header(default=None),
    x_jolly_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> MorningCallResponse:
    """The 8 AM call. Builds today's brief first (which also wakes a sleeping
    free-tier backend), then has Bolna ring the advocate. Needs the shared
    secret so only our scheduler can trigger it."""
    require_secret(request.app.state.settings, x_bolna_secret, x_jolly_secret, secret)
    phone = re.sub(r"[\s-]", "", phone_number)
    request.state.audit["actor_ref"] = hash_identifier(request.app.state.settings, phone)
    if not E164.match(phone):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Phone must be E.164.")
    brief = build_daily_brief(session, day=date.today(), advocate=advocate)
    data = _get_client(request).place_call(
        phone,
        user_data=morning_call_user_data(brief, advocate, language),
    )
    return MorningCallResponse(
        status=str(data.get("status", "queued")),
        message=f"Morning brief call to {phone}",
        call_id=data.get("execution_id"),
        matters=len(brief.matters),
        spoken_summary=brief.spoken_summary,
    )


@router.get("/calls/quality")
def calls_quality(session: Session = Depends(get_session)) -> dict:
    """Call quality from stored Bolna webhooks: count, completion rate, median/p90 latency and duration."""
    from backend.api.bolna import call_quality

    return call_quality(session)
