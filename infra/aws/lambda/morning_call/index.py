"""EventBridge Scheduler -> this Lambda -> POST /briefs/morning-call.

Scheduler's targets call AWS APIs, not arbitrary HTTPS, so this is the HTTP bridge.
Event (all optional):
  {"phone": "+91...", "advocate": "name", "dry_run": true}
dry_run only hits /health, so you can test the wiring without ringing anyone.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

_sm = boto3.client("secretsmanager")


def _secret() -> dict:
    raw = _sm.get_secret_value(SecretId=os.environ["SECRET_ID"])["SecretString"]
    return json.loads(raw)


def handler(event, _context):
    event = event or {}
    api = os.environ["API_URL"].rstrip("/")
    if event.get("dry_run"):
        with urllib.request.urlopen(f"{api}/health", timeout=20) as r:
            return {"status": r.status, "body": r.read().decode()[:200]}

    s = _secret()
    phone = event.get("phone") or s.get("ADVOCATE_PHONE")
    if not phone or phone == "CHANGE_ME":
        raise ValueError("No phone: set ADVOCATE_PHONE in the jolly/app secret or pass {'phone': ...}")
    query = {"phone_number": phone}
    if event.get("advocate"):
        query["advocate"] = event["advocate"]
    req = urllib.request.Request(
        f"{api}/briefs/morning-call?{urllib.parse.urlencode(query)}",
        method="POST",
        headers={"x-bolna-secret": s.get("BOLNA_WEBHOOK_SECRET", ""), "user-agent": "jolly-scheduler/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=50) as r:
            body = r.read().decode()[:500]
            print(json.dumps({"status": r.status, "body": body}))
            return {"status": r.status, "body": body}
    except urllib.error.HTTPError as e:  # surface as a Lambda error -> CloudWatch alarm
        raise RuntimeError(f"morning-call failed: HTTP {e.code} {e.read().decode()[:300]}") from e
