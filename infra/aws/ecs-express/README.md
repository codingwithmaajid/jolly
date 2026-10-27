# Tier 2 (after the buildathon): ECS Express Mode + RDS PostgreSQL/pgvector + S3/KMS

Notes only, no code yet. Tier 1 (`infra/aws`, one EC2 box with SQLite) is what ships for the
buildathon. Move here once Jolly has more than a handful of advocates or needs zero-downtime deploys.

## Why ECS Express Mode

- App Runner stopped accepting new customers on 30 Apr 2026, and AWS points new users to ECS Express Mode.
  <https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html>
- Express Mode has no extra charge; you pay for Fargate, the ALB, logs and data transfer. It sets up the
  ALB, HTTPS URL and autoscaling for you.
  <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-overview.html>
- Lambda is a poor fit today: the app builds its embedding index at startup, SQLite can't live there, and a
  VPC Lambda that needs both RDS and the internet needs a NAT Gateway (about $41/month).

## Target shape (ap-south-1, all customer data in India)

```
Cloudflare (api.golnk.in) -> AWS WAF -> ALB (HTTPS)
  -> ECS Express service: Fargate ARM 0.5 vCPU / 1 GB, 1-3 tasks, public subnets + public IP, SG: ALB only
  -> RDS PostgreSQL 17 + pgvector, db.t4g.micro (-> small), encrypted, private subnets, SG: tasks only
  -> S3 documents: SSE-KMS customer-managed key, Bucket Keys on, versioned, Block Public Access, presigned URLs
EventBridge Scheduler (one schedule per advocate, 08:00 their time zone) -> Lambda -> ALB
Secrets Manager (Bolna, LLM, DB password) · CloudWatch Logs/alarms · ADOT/OpenTelemetry -> X-Ray
GitHub OIDC -> push image -> update the Express service
```

- **No NAT Gateway**: tasks sit in public subnets with a public IP and a security group that only admits the ALB,
  so they can still reach Bolna and the LLM. RDS stays private.
- **Graviton/ARM everywhere** (Fargate ARM vCPU is about 44% cheaper than x86 in Mumbai). The GHCR image is
  already built for `linux/arm64`; ECS can pull from GHCR with a registry-credentials secret, or mirror to ECR.
- **WAF** on the ALB: allowlist Bolna's tool-call IPs (13.203.39.153, 13.126.9.249, 13.202.133.53) on
  `/bolna/*`, rate-limit `/briefs/morning-call`.

## Code changes needed first (in the app, not infra)

1. Add `psycopg[binary]` and `pgvector` to `pyproject.toml`; Alembic migration for the current SQLModel tables.
2. `CREATE EXTENSION vector;` on RDS; move vectors to a `vector` column (matches ADR-4's schema).
3. Move the boot-time `ensure_index` into a one-off ingest job/task so new tasks start fast.
4. Add OpenTelemetry (`FastAPIInstrumentor`, `RequestsInstrumentor`) so each Bolna tool call is one trace.
5. Optional `LLM_PROVIDER=bedrock`: Claude Haiku 4.5 through the India geo profile
   (`in.anthropic.claude-haiku-4-5-20251001-v1:0`, routes only to Mumbai and Hyderabad) and Titan Text
   Embeddings V2 in-region. Re-embedding is required because dimensions change.
   <https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html>,
   <https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-supported.html>

## Infra steps (about 1 to 2 days)

1. New CDK stack beside `JollyStack` (reuse the VPC: add isolated subnets for RDS).
2. `rds.DatabaseInstance` Postgres 17, `t4g.micro`, encrypted, single-AZ until there are paying firms.
   pgvector 0.8.x is available on current RDS minors:
   <https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-versions.html>
3. KMS CMK + documents bucket; grant the task role.
4. ECS Express Mode service from the image (console or CLI first; codify once the API settles).
5. Migrate: `restore.sh` the latest SQLite backup locally, run a one-off copy script into Postgres, re-embed.
6. Flip Cloudflare `api.golnk.in` to the ALB; keep the EC2 box stopped (not terminated) for a week as rollback.
7. Apply for AWS Activate Founders (needs a working company website and a Paid-plan account).
   <https://aws.amazon.com/startups/credits>

## Cost (USD/month, Mumbai, on-demand, from the research note)

| Line | Tier 2 demo | Tier 2 at 100 advocates |
|---|---|---|
| Fargate ARM 0.5 vCPU / 1 GB | $10.60 (1 task) | $21.21 (2 tasks) |
| ALB + LCU | $17.45 + ~$5.84 | $17.45 + ~$11.68 |
| Public IPv4 | $10.95 (3) | $14.60 (4) |
| RDS | t4g.micro $15.33 + 20 GB $2.62 | t4g.small Multi-AZ ≈ $61.32 (estimate) + $13.10 |
| S3, KMS, Secrets, logs | ≈ $3 | ≈ $14 |
| WAF | $8 | ≈ $8.12 |
| **Infra subtotal** | **≈ $74** | **≈ $161** |

LLM (Bedrock Nova Lite ≈ $13 to Haiku-class ≈ $215 at 100 advocates; verify Haiku on
<https://aws.amazon.com/bedrock/pricing/>) and Bolna telephony/STT/TTS are extra.
