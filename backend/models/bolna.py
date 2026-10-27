"""Schemas for inbound traffic from Bolna (webhooks and tool calls)."""

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict


class BolnaExecutionEvent(BaseModel):
    """Execution payload Bolna POSTs to the agent's webhook_url.

    Only the fields Jolly reads are typed; everything else is kept.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    agent_id: str | None = None
    status: str | None = None
    transcript: str | None = None
    summary: str | None = None
    extracted_data: dict[str, Any] | None = None
    context_details: dict[str, Any] | None = None


class CallerContext(BaseModel):
    """Optional caller context a tool call may carry (from Bolna context variables).

    advocate: the advocate this call is for; when given, cases of other advocates
    are treated as not found. caller_phone is only ever stored HMAC'd.
    """

    advocate: str | None = None
    caller_phone: str | None = None
    execution_id: str | None = None


class CaseLookupRequest(CallerContext):
    """Arguments the Bolna agent passes to the `get_case_brief` custom function."""

    case_id: str


class CaseLookupResponse(BaseModel):
    case_id: str
    found: bool
    spoken_summary: str
    citations: list[dict[str, Any]] = []


class DailyBriefRequest(CallerContext):
    """Arguments for the `get_daily_brief` custom function (all optional)."""

    date: dt.date | None = None
    advocate: str | None = None


class CaseQuestionRequest(CallerContext):
    """Arguments for the `ask_case_question` custom function."""

    case_id: str
    question: str


class CaseQuestionResponse(BaseModel):
    case_id: str
    found: bool
    spoken_answer: str
    answerable: bool = False
    mode: str = "not_found"
    citations: list[dict[str, Any]] = []
    guard_rule: str | None = None


class HearingUpdateRequest(CallerContext):
    """Arguments for the `log_hearing_update` custom function."""

    case_id: str
    dictation: str


class HearingUpdateResponse(BaseModel):
    case_id: str
    found: bool
    update_id: int | None = None
    spoken_confirmation: str
