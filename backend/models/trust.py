"""Trust-layer tables: audit log, spent approval tokens, webhook dedupe, call quality.

AuditLog is append-only at the ORM level (any UPDATE or DELETE of a row through
a Session raises) and hash-chained, so a write that bypasses the ORM is still
detectable by GET /audit/verify. On Postgres, also `REVOKE UPDATE, DELETE` on
the table from the app role.
"""

from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint, event
from sqlalchemy.orm import Session as _OrmSession
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: str = Field(description="UTC ISO-8601 timestamp (string so the hash is stable across databases).")
    actor_type: str  # bolna_tool | bolna_webhook | advocate | scheduler | console | system
    actor_ref: str | None = Field(default=None, description="HMAC of a phone number, or an advocate name. Never a raw phone.")
    execution_id: str | None = Field(default=None, index=True)
    action: str = Field(index=True)  # ask_case_question | get_case_brief | webhook | approve | reject | ...
    case_id: str | None = Field(default=None, index=True)
    decision: str  # allowed | refused | denied | not_found | duplicate | error
    guard_rule: str | None = None  # e.g. S0.outcome, S0.injection, S4.not_owner, S4.cross_case, S3.pii.phone
    args_hash: str | None = None  # sha256 of canonical args; no free text in the log
    latency_ms: int | None = None
    status_code: int | None = None
    prev_hash: str
    row_hash: str = Field(index=True)


class SpentApprovalToken(SQLModel, table=True):
    nonce: str = Field(primary_key=True)
    update_id: int = Field(index=True)
    action: str
    spent_at: datetime = Field(default_factory=_now)


class WebhookEvent(SQLModel, table=True):
    """One row per (execution_id, status) Bolna delivery. Repeats are dropped."""

    __table_args__ = (UniqueConstraint("execution_id", "status", name="uq_webhook_exec_status"),)
    id: int | None = Field(default=None, primary_key=True)
    execution_id: str = Field(index=True)
    status: str
    received_at: datetime = Field(default_factory=_now)


class CallExecution(SQLModel, table=True):
    """Latest known state and quality numbers for one Bolna execution."""

    execution_id: str = Field(primary_key=True)
    agent_id: str | None = None
    status: str | None = None
    termination_reason: str | None = None
    conversation_duration: float | None = None  # seconds
    time_to_first_audio_ms: float | None = None  # Bolna latency_data.time_to_first_audio (ms)
    llm_first_token_ms: float | None = None  # mean of latency_data.llm.turns[].time_to_first_token
    total_interruptions: int | None = None
    total_cost: float | None = None  # Bolna cents
    updated_at: datetime = Field(default_factory=_now)


@event.listens_for(_OrmSession, "before_flush")
def _audit_is_append_only(session, flush_context, instances) -> None:  # noqa: ARG001
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, AuditLog) and (obj in session.deleted or session.is_modified(obj)):
            raise PermissionError("audit_log is append-only")
