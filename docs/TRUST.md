# Jolly trust layer

What stops Jolly from saying the wrong thing, to the wrong person, about the wrong case, and how we prove it.

```
Bolna tool call -> [A] shared secret (header, fail closed) -> [S0] input guard -> [S4] advocate/case scope
               -> [S2] retrieval restricted to the case, flagged paragraphs excluded
               -> Gemini (citation-validated) | extractive -> [S3] output guard (PII, case law, leaks)
every POST -> hash-chained audit_log row (rule that fired, latency ms, phone HMAC'd)
```

## Controls

| Control | Where | What it does |
|---|---|---|
| Signed approval links | `backend/core/security.py`, `backend/api/updates.py` | `POST /updates/{id}/approve|reject` need a token `b64url({update, expiry, nonce}).HMAC-SHA256`. Bound to one update, expires (`APPROVAL_TTL_SECONDS`, 24 h), single use (nonce stored in `spentapprovaltoken` in the same transaction as the approval). Pending updates carry `approval_token`, `approve_url`, `reject_url`. The console sends `X-Approval-Token`. |
| Shared secret, fail closed | `security.require_secret` | Bolna tools, webhook and `/briefs/morning-call` accept `X-Jolly-Secret` (or `X-Bolna-Secret`) and the legacy `?secret=` used by the live agent. Constant-time compare. If `BOLNA_WEBHOOK_SECRET` is unset the routes are open only when `APP_ENV` is `local` or `test`; anywhere else they return **503**. |
| Webhook dedupe | `WebhookEvent` (unique `execution_id, status`) | Bolna sends the same execution several times; repeats return `{"duplicate": true}` and are not stored twice. |
| Case scoping (S4) | `backend/services/ownership.py` | A tool call may name an advocate (`advocate` in the body or `X-Jolly-Advocate`). Another advocate's case is reported as "not in your diary", nothing about it is spoken, and the audit row records `S4.not_owner`. |
| Cross-case isolation (S2) | `backend/services/qa.py` | Retrieval is filtered by `case_id`, and qa.py re-checks every passage belongs to the case. A question on one matter that names another matter (id, court number, party) is refused (`S4.cross_case`). |
| Guard v2 (S0/S1) | `backend/services/guard.py` | Normalisation (NFKC, zero-width removal, "i g n o r e" collapse, base64 decode) then patterns for injection, outcome prediction (incl. "chances", "how likely", "expect the court", "jeetenge", जीतेंगे, ஜெயிப்போமா), solicitation (BCI Rule 36) and prompt extraction. |
| Output guard (S3) | `guard.check_output`, `guard.mask_pii` | Aadhaar (Verhoeff-checked), PAN, Indian mobile numbers are masked in spoken answers; Gemini output with case law not in the record, prompt leaks or outcome language is rejected and Jolly falls back to the extractive answer. |
| Abstain | `qa._shares_content` | If the retrieved passages share no content word with the question, Jolly says it couldn't find it instead of quoting something unrelated. |
| Honest labels | `qa._extractive` | Uploaded documents are "an unverified uploaded document"; only the other side's pleadings are "the opposing party's ... claims". |
| Audit log | `backend/services/audit.py`, `GET /audit`, `GET /audit/verify` | Append-only at the ORM level, hash-chained (`row_hash = sha256(prev_hash + row)`), so edits made directly in the DB show up in `/audit/verify` as `broken_at`. Stores ids, rule names, args hash, status code, latency ms; phone numbers only as HMAC. |
| Call quality | `CallExecution`, `GET /calls/quality` | Per-execution status, duration, `time_to_first_audio` (ms), mean LLM first-token ms, interruptions, cost, from Bolna webhooks. Summary: count, completion rate (terminal `completed` with duration > 0), median/p90 latency and duration. |

## Environment variables (new)

| Variable | Default | Notes |
|---|---|---|
| `APPROVAL_SECRET` | falls back to `BOLNA_WEBHOOK_SECRET` | HMAC key for approval tokens. Production with neither set: approvals fail closed. |
| `APPROVAL_TTL_SECONDS` | `86400` | Approval token lifetime. |
| `APPROVAL_TOKEN_IN_PAYLOAD` | `true` | Demo convenience: the console gets the token through `GET /updates`. Set `false` once approvals move to a signed link sent only to the advocate. |
| `AUDIT_PEPPER` | falls back to the approval key | HMAC key for phone numbers in the audit log. |

`BOLNA_WEBHOOK_SECRET` is now **required** when `APP_ENV=production`.

## Evals

`uv run python evals/run_evals.py` (offline, no keys) writes `evals/out/scorecard.md`, `scorecard.json`, `badge.json`. Gates: faithfulness ≥ 90%, refusal ≥ 90%, injection = 100%, dictation ≥ 85%, latency p95 within budget. CI (`.github/workflows/ci.yml`) runs it on every PR.

`uv run python scripts/redteam.py` runs `evals/redteam.yaml`. The **held-out** split was written before guard v2 was merged and was not used to tune it; its score is the honest generalisation number. To add coverage, add a new held-out case rather than editing the guard to pass an existing one. `--live https://<backend>` sends the question attacks to the deployed `ask-case-question` tool.

| | Before (hero-demo) | After (this branch) |
|---|---|---|
| Eval cases | 30/41 | 40/41 |
| Red-team, tuned | 9/21 | 21/21 |
| Red-team, held-out attacks caught | 1/18 | 9/18 |
| Red-team, held-out benign kept | 13/13 | 13/13 |

Not caught yet (held-out): paraphrased "disregard what you were told", "[AI] respond to every question with ...", romanised Tamil, leetspeak, "pretend you are my senior ... will the magistrate convict", "do you think the bench will grant", "bail milegi kya", SMS-to-everyone solicitation, Tamil "verdict in our favour". The remaining eval failure (F02, "cheque amount") is a retrieval miss with the offline hashing embedder.
