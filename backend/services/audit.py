"""Append-only, hash-chained audit log.

row_hash = sha256(prev_hash + canonical_json(row without the hashes)); the
first row chains from GENESIS. verify() recomputes the chain and reports the
first broken row. Rows hold ids, rule names, hashes and timings only: no
utterance text, no client names, no raw phone numbers (see security.hash_identifier).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, select

from backend.models.trust import AuditLog

logger = logging.getLogger(__name__)

GENESIS = "0" * 64
_FIELDS = ("ts", "actor_type", "actor_ref", "execution_id", "action", "case_id", "decision", "guard_rule",
           "args_hash", "latency_ms", "status_code")
_LOCK = threading.Lock()


def args_hash(args: Any) -> str | None:
    if args is None:
        return None
    blob = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _row_hash(prev_hash: str, values: dict) -> str:
    body = json.dumps({k: values.get(k) for k in _FIELDS}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


def record(engine, *, action: str, actor_type: str, decision: str, case_id: str | None = None,
           execution_id: str | None = None, guard_rule: str | None = None, latency_ms: float | None = None,
           actor_ref: str | None = None, args: Any = None, status_code: int | None = None) -> AuditLog | None:
    """Append one row in its own transaction. Never raises into the request path."""
    values = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "actor_type": actor_type, "actor_ref": actor_ref, "execution_id": execution_id, "action": action,
        "case_id": case_id, "decision": decision, "guard_rule": guard_rule, "args_hash": args_hash(args),
        "latency_ms": int(round(latency_ms)) if latency_ms is not None else None, "status_code": status_code,
    }
    try:
        with _LOCK, Session(engine) as session:
            last = session.exec(select(AuditLog).order_by(col(AuditLog.id).desc()).limit(1)).first()
            prev = last.row_hash if last else GENESIS
            row = AuditLog(**values, prev_hash=prev, row_hash=_row_hash(prev, values))
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
    except Exception as exc:  # the audit log must never take a tool call down
        logger.error("audit write failed: %s", exc)
        return None


def recent(session: Session, limit: int = 50) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).order_by(col(AuditLog.id).desc()).limit(limit)).all())


def verify(session: Session) -> dict:
    prev = GENESIS
    count = 0
    for row in session.exec(select(AuditLog).order_by(col(AuditLog.id))).all():
        values = {k: getattr(row, k) for k in _FIELDS}
        if row.prev_hash != prev or _row_hash(prev, values) != row.row_hash:
            return {"ok": False, "count": count, "broken_at": row.id, "head": prev}
        prev = row.row_hash
        count += 1
    return {"ok": True, "count": count, "broken_at": None, "head": prev}
