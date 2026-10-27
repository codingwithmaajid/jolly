"""Shared-secret checks and signed approval tokens.

Secrets
- Bolna (tool calls, webhooks) and the scheduler send BOLNA_WEBHOOK_SECRET in a
  header (X-Jolly-Secret / X-Bolna-Secret) or, for the live agent that still uses
  query URLs, as ?secret=. Compared in constant time.
- Unset secret: open only when APP_ENV is local or test. Anywhere else the
  route fails CLOSED with 503, so a missing env var never exposes the API.

Approval tokens
- token = b64url(json{u: update_id, e: expiry, n: nonce}) "." b64url(HMAC-SHA256)
- Key: APPROVAL_SECRET, else BOLNA_WEBHOOK_SECRET, else (local/test only) a
  random per-process key. Bound to one update, expiring (APPROVAL_TTL_SECONDS),
  and single use: the nonce is stored when the token is spent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException, status

from backend.core.config import Settings

OPEN_ENVS = frozenset({"local", "test"})
_DEV_KEY = secrets.token_bytes(32)


class TokenError(Exception):
    """Approval token missing, malformed, forged, expired or unusable."""


def is_open_env(settings: Settings) -> bool:
    return (settings.app_env or "").strip().lower() in OPEN_ENVS


def require_secret(settings: Settings, *given: str | None, expected: str | None = None) -> None:
    """Raise 503 (misconfigured, non-local) or 401 (wrong/missing secret)."""
    expected = expected if expected is not None else settings.bolna_webhook_secret
    if not expected:
        if is_open_env(settings):
            return
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Server misconfigured: shared secret not set.")
    candidate = next((g for g in given if g), "")
    if not candidate or not hmac.compare_digest(candidate.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing secret.")


# ---------------------------------------------------------------------------
# Approval tokens
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def approval_key(settings: Settings) -> bytes:
    key = settings.approval_secret or settings.bolna_webhook_secret
    if key:
        return hashlib.sha256(b"jolly-approval:" + key.encode()).digest()
    if is_open_env(settings):
        return _DEV_KEY
    raise TokenError("APPROVAL_SECRET (or BOLNA_WEBHOOK_SECRET) is not set.")


def make_approval_token(settings: Settings, update_id: int, *, ttl: int | None = None, now: float | None = None) -> str:
    key = approval_key(settings)
    now = time.time() if now is None else now
    exp = int(now + (ttl if ttl is not None else settings.approval_ttl_seconds))
    payload = _b64(json.dumps({"u": int(update_id), "e": exp, "n": secrets.token_urlsafe(12)},
                              separators=(",", ":")).encode())
    sig = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_approval_token(settings: Settings, token: str | None, update_id: int, *, now: float | None = None) -> dict:
    """Return the decoded claims or raise TokenError. Does not check single use."""
    if not token or token.count(".") != 1:
        raise TokenError("Approval token missing or malformed.")
    payload, sig = token.split(".")
    expected = _b64(hmac.new(approval_key(settings), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig.encode(), expected.encode()):
        raise TokenError("Approval token signature is invalid.")
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("Approval token payload is invalid.") from exc
    if claims.get("u") != int(update_id):
        raise TokenError("Approval token is for a different update.")
    if (time.time() if now is None else now) > float(claims.get("e", 0)):
        raise TokenError("Approval token has expired.")
    if not claims.get("n"):
        raise TokenError("Approval token has no nonce.")
    return claims


def hash_identifier(settings: Settings, value: str | None) -> str | None:
    """HMAC a phone number (or other identifier) for the audit log. Never store it raw."""
    if not value:
        return None
    pepper = settings.audit_pepper or settings.approval_secret or settings.bolna_webhook_secret
    key = pepper.encode() if pepper else _DEV_KEY
    digits = "".join(ch for ch in value if ch.isdigit() or ch == "+")
    return "h:" + hmac.new(key, digits.encode(), hashlib.sha256).hexdigest()[:24]
