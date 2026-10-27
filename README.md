# Jolly - Voice-First Legal Agent

Jolly (formerly AIRA) is a voice-first legal agent for Indian law firms and courts, built for the Bolna Symphony Buildathon 2026.

Tagline: Human decides. AI prepares, coordinates, executes, and tracks.

## Problem

Courts and judicial staff handle large volumes of case documents, filings, hearing records, previous orders, schedules, postponements, missing documents, and administrative follow-up. Much of this work is repetitive and time-sensitive, but the judicial decision must remain with the authorized human judge.

## Solution

Jolly is designed to help authorized judicial users prepare for hearings and manage case workflow. The system should retrieve authorized case material, classify documents, build timelines, identify missing items, prepare source-grounded briefs, and coordinate approved administrative follow-up.

Jolly must not replace judges. It must not determine guilt, liability, sentence, verdict, case outcome, or whether a party should win or lose.

## Current Status

Implemented:

- FastAPI backend.
- React/Vite frontend scaffold.
- Bolna outbound call integration (`POST /call`, `GET /call/{id}`).
- Bolna webhook receiver (`POST /bolna/webhook`) and custom-function tool scaffold (`POST /bolna/tools/get-case-brief`).
- Backend service metadata and health endpoints.
- Docker-based deployment setup.
- Environment-variable based Bolna secret handling.

Planned:

- Judicial case preparation workflow.
- LangGraph agent runtime.
- Case document RAG.
- Case, document, hearing, timeline, workflow, and approval tools.
- Human approval workflows.
- PostgreSQL/Supabase persistence.
- Audit logging and observability.

Not implemented yet:

- Judicial agent.
- RAG pipeline.
- Database schema.
- Document ingestion.
- Case timeline builder.
- Pre-hearing brief generator.
- Human approval UI.
- Authentication and RBAC.

## Flagship Workflow

The hackathon MVP centers on one workflow:

```text
Judge: "Prepare Case 1042 for tomorrow."

Jolly:
1. Identifies the case.
2. Retrieves authorized documents.
3. Searches the case knowledge base.
4. Builds the procedural timeline.
5. Finds hearing history.
6. Detects postponements.
7. Identifies missing documents.
8. Generates a source-grounded pre-hearing brief.
9. Presents the brief for review.
10. Waits for the human judicial decision.
11. Executes only approved administrative follow-up.
```

## Architecture Overview

```text
Judge / Authorized Staff
        |
        v
Voice Interface - Bolna
        |
        v
FastAPI Application
        |
        v
LangGraph Judicial Workflow Agent
        |
        +--> Case Tools
        +--> Document Tools
        +--> Hearing Tools
        +--> Timeline Tools
        +--> Workflow Tools
        +--> Human Approval Tools
        |
        v
RAG / Knowledge Layer
        |
        v
PostgreSQL / Supabase + Object Storage + Vector Index
```

Voice is an interface to the agent. It is not the intelligence layer.

## Human In The Loop

Jolly uses bounded autonomy:

- Level 0: AI provides information only.
- Level 1: AI recommends workflow actions.
- Level 2: AI prepares actions for approval.
- Level 3: AI executes pre-authorized administrative actions.
- Level 4: Prohibited for judicial decisions.

Every legally consequential output must remain subject to human review and authorization.

## RAG And Source Grounding

The planned RAG system will retrieve information from authorized case documents and provide citations. Case summaries and pre-hearing briefs should avoid unsupported factual claims. Uploaded documents are treated as untrusted data, not as instructions to the agent.

## Planned Agent Tools

Planned tool groups:

- Case tools: get case, search cases, get/update case status.
- Document tools: search, retrieve, classify, extract metadata, find missing documents.
- Hearing tools: hearing history, next hearing, postponements, create/update hearings.
- Timeline tools: build case timeline, retrieve events.
- Workflow tools: tasks, notifications, deadlines.
- Knowledge tools: semantic search, keyword search, source retrieval, citation lookup.
- Generation tools: summaries, pre-hearing briefs, draft administrative documents.
- Approval tools: request approval, record approval, reject action.

## Security Philosophy

Jolly will handle sensitive legal information, so the planned architecture requires authentication, authorization, role-based access control, case-level isolation, audit logging, secret management, encryption, document provenance, and prompt-injection protections.

Secrets such as `BOLNA_API_KEY`, LLM keys, database URLs, and Supabase credentials must never be committed to Git or exposed to frontend JavaScript.

### Trust layer (built)

Signed single-use approval links, fail-closed shared secrets, per-advocate case scoping, a multilingual guard (English, Hinglish, Hindi, Tamil) with an output check for PII and invented citations, a hash-chained audit log (`GET /audit`, `GET /audit/verify`) and call-quality stats from Bolna webhooks (`GET /calls/quality`). Details, eval numbers and the honest held-out red-team score: [docs/TRUST.md](docs/TRUST.md).

Evals run offline on every PR with no API keys: `uv run python evals/run_evals.py` (scorecard in `evals/out/scorecard.md`); red-team: `uv run python scripts/redteam.py [--live URL]`.

## Roadmap

P0 MVP critical:

- Define case preparation workflow.
- Define data contracts and tool contracts.
- Add sample case documents for controlled demo data.
- Build source-grounded pre-hearing brief workflow.
- Add human review and approval checkpoints.

P1 important:

- Persistent case and hearing state.
- Observability and audit logging.
- Role-based access.
- Better retrieval evaluation.
- Voice-to-agent integration.

P2 future:

- Multilingual support.
- Postponement analytics.
- Advanced scheduling.
- Broader workflow automation.
- Institutional compliance review.

## Development Setup

Current local setup is intentionally small:

```text
uv sync
uv run uvicorn server:app --reload
```

Environment variables for the Bolna voice layer (see `.env.example`):

```text
BOLNA_API_KEY         # from platform.bolna.ai
BOLNA_AGENT_ID        # UUID of the Jolly agent in Bolna
BOLNA_FROM_NUMBER     # optional, E.164 number bought in Bolna
BOLNA_WEBHOOK_SECRET  # optional, appended as ?secret=... to webhook/tool URLs
```

In the Bolna dashboard, set the agent's webhook URL to `https://<backend>/bolna/webhook?secret=<BOLNA_WEBHOOK_SECRET>` and add a custom function `get_case_brief(case_id)` pointing at `https://<backend>/bolna/tools/get-case-brief?secret=<BOLNA_WEBHOOK_SECRET>`.

## Deployment

The current app is Dockerized and intended for Render deployment. Future deployment will need environment secrets, database services, object storage, vector storage, and observability configuration.

## Limitations

This repository is not a production court system. A real judicial deployment would require legal, regulatory, privacy, security, accessibility, procurement, operational, and institutional review.

The current implementation is a voice-call integration prototype, not a complete judicial assistant.

## Responsible AI Statement

Jolly must support judicial workflow without replacing judicial authority. The system should expose uncertainty, cite sources, avoid unsupported claims, require human approval for consequential actions, preserve auditability, and keep case data isolated.

## Planning Documents

- [PRD.md](./PRD.md) - product requirements.
- [DOCS.md](./DOCS.md) - technical architecture.
- [TECHSTACK.md](./TECHSTACK.md) - technology choices and tradeoffs.
- [STATUS.md](./STATUS.md) - current status and roadmap.
