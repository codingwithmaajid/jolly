"""Audit log API and the route class that times every Bolna call into it.

- GET /audit?limit=50   most recent rows, newest first
- GET /audit/verify     recomputes the hash chain: {ok, count, broken_at, head}

AuditedRoute wraps each POST handler on a router: it starts a timer, lets the
handler fill `request.state.audit` (case_id, decision, guard_rule, actor_ref,
execution_id), and appends one hash-chained row when the handler returns or
raises, with latency in ms.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlmodel import Session

from backend.core.db import get_session
from backend.services import audit

router = APIRouter(tags=["Audit"])


def _decision_for(status: int) -> str:
    if status in (401, 403, 503):
        return "denied"
    if status >= 400:
        return "error"
    return "allowed"


class AuditedRoute(APIRoute):
    actor_type = "bolna_tool"

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()
        if "POST" not in (self.methods or set()):
            return original
        action = self.path.rstrip("/").rsplit("/", 1)[-1].replace("-", "_")
        actor_type = {"webhook": "bolna_webhook", "morning_call": "scheduler", "call": "console"}.get(action, self.actor_type)

        async def handler(request: Request) -> Response:
            request.state.audit = {}
            t0 = time.perf_counter()
            status = 500
            try:
                response = await original(request)
                status = response.status_code
                return response
            except HTTPException as exc:
                status = exc.status_code
                raise
            finally:
                info = getattr(request.state, "audit", {}) or {}
                audit.record(
                    request.app.state.engine, action=action, actor_type=actor_type,
                    decision=info.get("decision") or _decision_for(status),
                    case_id=info.get("case_id"), execution_id=info.get("execution_id"),
                    guard_rule=info.get("guard_rule"), actor_ref=info.get("actor_ref"),
                    args=info.get("args"), latency_ms=(time.perf_counter() - t0) * 1000, status_code=status,
                )

        return handler


class AuditRow(BaseModel):
    id: int
    ts: datetime
    actor_type: str
    actor_ref: str | None
    execution_id: str | None
    action: str
    case_id: str | None
    decision: str
    guard_rule: str | None
    latency_ms: int | None
    status_code: int | None
    prev_hash: str
    row_hash: str


class AuditVerify(BaseModel):
    ok: bool
    count: int
    broken_at: int | None
    head: str


@router.get("/audit", response_model=list[AuditRow])
def audit_recent(limit: int = Query(default=50, ge=1, le=500), session: Session = Depends(get_session)) -> list[AuditRow]:
    return [AuditRow.model_validate(r, from_attributes=True) for r in audit.recent(session, limit)]


@router.get("/audit/verify", response_model=AuditVerify)
def audit_verify(session: Session = Depends(get_session)) -> AuditVerify:
    return AuditVerify(**audit.verify(session))
