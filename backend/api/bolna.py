"""Inbound endpoints Bolna calls during and after a conversation.

- POST /bolna/webhook               execution updates (set as the agent's webhook_url)
- POST /bolna/tools/get-case-brief  custom function the voice agent calls mid-call

- POST /bolna/tools/get-daily-brief custom function for the morning brief call
- POST /bolna/tools/ask-case-question  cited answer to a free-form question about one case
- POST /bolna/tools/log-hearing-update  post-hearing dictation -> pending draft for approval

Case answers come from the case store and are source-grounded. Bolna is
the voice layer; the legal reasoning stays in Jolly behind these endpoints.

Trust layer: every POST here is timed into the hash-chained audit log
(AuditedRoute); the shared secret is accepted in the X-Jolly-Secret header or
the legacy ?secret= query and fails closed outside local/test; webhooks are
deduplicated on (execution id, status); a tool call that names an advocate
only sees that advocate's cases.
"""

import logging
import statistics
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from backend.api.audit import AuditedRoute
from backend.core.db import get_session
from backend.core.security import hash_identifier, require_secret
from backend.models.bolna import (
    BolnaExecutionEvent,
    CallerContext,
    CaseLookupRequest,
    CaseLookupResponse,
    CaseQuestionRequest,
    CaseQuestionResponse,
    DailyBriefRequest,
    HearingUpdateRequest,
    HearingUpdateResponse,
)
from backend.models.cases import Case
from backend.models.trust import CallExecution, WebhookEvent
from backend.services.cases import DailyBrief, build_case_brief, build_daily_brief, find_case
from backend.services.dictation import create_update, spoken_confirmation
from backend.services.ownership import NOT_IN_DIARY, owns
from backend.services.qa import answer_question

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bolna", tags=["Bolna"], route_class=AuditedRoute)

# In-memory buffer of recent execution events, newest last (the durable
# record is the WebhookEvent / CallExecution tables).
RECENT_EVENTS: list[dict] = []
MAX_EVENTS = 50


def _check_secret(request: Request, header_secret: str | None, query_secret: str | None) -> None:
    require_secret(request.app.state.settings, header_secret, query_secret)


def _context(request: Request, body: CallerContext | None, case_id: str | None = None) -> str | None:
    """Fill the audit context for this call and return the advocate scope, if any."""
    advocate = (body.advocate if body else None) or request.headers.get("x-jolly-advocate")
    info = request.state.audit
    info["case_id"] = case_id
    if body is not None:
        info["execution_id"] = body.execution_id
        info["actor_ref"] = hash_identifier(request.app.state.settings, body.caller_phone)
        info["args"] = body.model_dump(exclude={"caller_phone"})
    return advocate


def _scoped_case(request: Request, session: Session, query: str, advocate: str | None) -> Case | None:
    case = find_case(session, query)
    if case is not None and not owns(case, advocate):
        request.state.audit.update(decision="refused", guard_rule="S4.not_owner", case_id=case.id)
        return None
    if case is None:
        request.state.audit.setdefault("decision", "not_found")
    else:
        request.state.audit["case_id"] = case.id
    return case


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _store_execution(session: Session, event: BolnaExecutionEvent) -> None:
    extra = event.model_extra or {}
    row = session.get(CallExecution, event.id) or CallExecution(execution_id=event.id)
    row.agent_id = event.agent_id or row.agent_id
    row.status = event.status or row.status
    row.termination_reason = extra.get("termination_reason") or row.termination_reason
    dur = _num(extra.get("conversation_duration"))
    row.conversation_duration = dur if dur is not None else row.conversation_duration
    lat = extra.get("latency_data") or {}
    if isinstance(lat, dict):
        ttfa = _num(lat.get("time_to_first_audio"))
        row.time_to_first_audio_ms = ttfa if ttfa is not None else row.time_to_first_audio_ms
        turns = [(_num(t.get("time_to_first_token"))) for t in ((lat.get("llm") or {}).get("turns") or [])
                 if isinstance(t, dict)]
        turns = [t for t in turns if t is not None]
        if turns:
            row.llm_first_token_ms = statistics.fmean(turns)
        stats = lat.get("interruption_stats") or {}
        if isinstance(stats, dict) and stats.get("total_interruptions") is not None:
            row.total_interruptions = int(stats["total_interruptions"])
    cost = _num(extra.get("total_cost"))
    row.total_cost = cost if cost is not None else row.total_cost
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def execution_webhook(
    event: BolnaExecutionEvent,
    request: Request,
    x_jolly_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    _check_secret(request, x_jolly_secret, secret)
    logger.info("bolna execution event id=%s status=%s", event.id, event.status)
    extra = event.model_extra or {}
    phone = (event.context_details or {}).get("recipient_phone_number") or extra.get("user_number") \
        or (extra.get("telephony_data") or {}).get("to_number")
    request.state.audit.update(execution_id=event.id, args={"id": event.id, "status": event.status},
                               actor_ref=hash_identifier(request.app.state.settings, phone))

    duplicate = False
    if event.id:
        try:
            session.add(WebhookEvent(execution_id=event.id, status=event.status or ""))
            session.commit()
        except IntegrityError:
            session.rollback()
            duplicate = True
    if duplicate:
        request.state.audit["decision"] = "duplicate"
        return {"received": event.id, "duplicate": True}

    if event.id:
        _store_execution(session, event)
    RECENT_EVENTS.append(event.model_dump())
    del RECENT_EVENTS[:-MAX_EVENTS]
    return {"received": event.id or "unknown", "duplicate": False}


@router.get("/executions/recent")
async def recent_executions() -> list[dict]:
    return list(reversed(RECENT_EVENTS))


@router.post("/tools/get-case-brief", response_model=CaseLookupResponse)
def get_case_brief(
    body: CaseLookupRequest,
    request: Request,
    x_jolly_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> CaseLookupResponse:
    _check_secret(request, x_jolly_secret, secret)
    advocate = _context(request, body)
    query = body.case_id.strip()
    case = _scoped_case(request, session, query, advocate)
    if case is None:
        return CaseLookupResponse(
            case_id=query,
            found=False,
            spoken_summary=(
                NOT_IN_DIARY if request.state.audit.get("guard_rule") == "S4.not_owner" else
                f"I couldn't find a case matching {query}. "
                "Could you give me the case number or the client's name?"
            ),
        )
    brief = build_case_brief(session, case)
    return CaseLookupResponse(
        case_id=case.id,
        found=True,
        spoken_summary=brief.spoken_summary,
        citations=[line.citation.model_dump(mode="json") for line in brief.lines if line.citation],
    )


@router.post("/tools/get-daily-brief", response_model=DailyBrief)
def get_daily_brief(
    request: Request,
    body: DailyBriefRequest | None = None,
    x_jolly_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> DailyBrief:
    _check_secret(request, x_jolly_secret, secret)
    body = body or DailyBriefRequest()
    advocate = _context(request, body)
    return build_daily_brief(session, day=body.date, advocate=advocate)


@router.post("/tools/ask-case-question", response_model=CaseQuestionResponse)
def ask_case_question(
    body: CaseQuestionRequest,
    request: Request,
    x_jolly_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> CaseQuestionResponse:
    _check_secret(request, x_jolly_secret, secret)
    advocate = _context(request, body)
    case = _scoped_case(request, session, body.case_id, advocate)
    if case is None:
        not_owner = request.state.audit.get("guard_rule") == "S4.not_owner"
        return CaseQuestionResponse(
            case_id=body.case_id, found=False, guard_rule="S4.not_owner" if not_owner else None,
            spoken_answer=NOT_IN_DIARY if not_owner else
            f"I couldn't find a case matching {body.case_id}. Which case do you mean?",
        )
    ans = answer_question(session, case, body.question, embedder=request.app.state.embedder, llm=request.app.state.llm)
    if ans.guard_rule:
        request.state.audit.update(decision="refused", guard_rule=ans.guard_rule)
    elif ans.mode == "not_found":
        request.state.audit["decision"] = "not_found"
    spoken = ans.answer
    if ans.citations:
        c = ans.citations[0]
        when = c.doc_date.strftime("%-d %B") if c.doc_date else ""
        spoken += f" That's from the {c.doc_title.lower()}{' of ' + when if when else ''}."
    return CaseQuestionResponse(
        case_id=case.id, found=True, spoken_answer=spoken, answerable=ans.answerable, mode=ans.mode,
        citations=[c.model_dump(mode="json") for c in ans.citations], guard_rule=ans.guard_rule,
    )


@router.post("/tools/log-hearing-update", response_model=HearingUpdateResponse)
def log_hearing_update(
    body: HearingUpdateRequest,
    request: Request,
    x_jolly_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> HearingUpdateResponse:
    """Corridor dictation on a call -> pending draft. Never commits or sends anything."""
    _check_secret(request, x_jolly_secret, secret)
    advocate = _context(request, body)
    query = body.case_id.strip()
    case = _scoped_case(request, session, query, advocate)
    if case is None:
        not_owner = request.state.audit.get("guard_rule") == "S4.not_owner"
        return HearingUpdateResponse(
            case_id=query, found=False,
            spoken_confirmation=NOT_IN_DIARY if not_owner else
            f"I couldn't find a case matching {query}. Which case was this hearing for?",
        )
    if len(body.dictation.strip()) < 5:
        return HearingUpdateResponse(
            case_id=case.id, found=True,
            spoken_confirmation="I didn't catch the update. What happened at the hearing, and what's the next date?",
        )
    upd = create_update(session, case, body.dictation, llm=request.app.state.llm, source="voice",
                        execution_id=body.execution_id)
    return HearingUpdateResponse(case_id=case.id, found=True, update_id=upd.id,
                                 spoken_confirmation=spoken_confirmation(upd))


def call_quality(session: Session) -> dict:
    """Summary of stored executions: count, completion rate, latency and duration percentiles."""
    from backend.services.bolna_client import TERMINAL_STATUSES

    rows = session.exec(select(CallExecution)).all()
    terminal = [r for r in rows if (r.status or "") in TERMINAL_STATUSES]
    completed = [r for r in terminal if r.status == "completed" and (r.conversation_duration or 0) > 0]

    def pct(values: list[float]) -> dict:
        v = sorted(values)
        if not v:
            return {"median": None, "p90": None, "n": 0}
        p90 = v[min(len(v) - 1, max(0, int(-(-0.9 * len(v) // 1)) - 1))]  # nearest-rank
        return {"median": statistics.median(v), "p90": p90, "n": len(v)}

    return {
        "count": len(rows),
        "terminal": len(terminal),
        "completed": len(completed),
        "completion_rate": round(len(completed) / len(terminal), 4) if terminal else None,
        "latency_ms": pct([r.time_to_first_audio_ms for r in rows if r.time_to_first_audio_ms is not None]),
        "llm_first_token_ms": pct([r.llm_first_token_ms for r in rows if r.llm_first_token_ms is not None]),
        "duration_s": pct([r.conversation_duration for r in completed if r.conversation_duration is not None]),
        "note": "latency_ms is Bolna's time_to_first_audio (end of caller speech to first agent audio). "
                "Completion = terminal status 'completed' with conversation_duration > 0.",
    }
