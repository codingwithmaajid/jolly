# infra/aws: Jolly Tier 1 on AWS Mumbai (CDK, Python)

One Graviton box in **ap-south-1** that runs the same Docker image Render runs.
The full runbook (account, credits, DNS, Bolna, rollback, costs, DPDP) is in
[`docs/DEPLOY_AWS.md`](../../docs/DEPLOY_AWS.md). This file is the map.

```
Bolna (AWS ap-south-1 IPs) ──HTTPS──> Cloudflare (api.golnk.in) ──> Elastic IP
                                                                      │
  EC2 t4g.small, Amazon Linux 2023 arm64, IMDSv2 only, no SSH (SSM Session Manager)
    ├─ caddy:2 container  :80/:443, Let's Encrypt cert, reverse_proxy api:8000
    ├─ api container      ghcr.io/codingwithmaajid/jolly-backend:<tag>, non-root, logs -> CloudWatch /jolly/api
    ├─ /opt/jolly/data    encrypted gp3 data volume (SQLite), kept as a snapshot if the stack is deleted
    └─ jolly-backup.timer 03:00 IST nightly: sqlite .backup -> s3://<BackupBucket>/sqlite/ (30-day lifecycle)

EventBridge Scheduler cron(0 8 ? * MON-SAT *) Asia/Kolkata ─> Lambda ─> POST /briefs/morning-call
Secrets Manager jolly/app (LLM/Bolna keys, phone)   SSM /jolly/env/* (plain config), /jolly/image-tag
Alarms: EC2 system check -> auto-recover, instance check -> reboot, Lambda errors; SNS email
Budget: $10/month (email at 80% and 100% actual, 100% forecast)
GitHub OIDC role jolly-github-deploy -> ssm:SendCommand on this one instance
```

## Files

| Path | What it is |
|---|---|
| `app.py`, `cdk.json`, `requirements.txt` | CDK app. Settings live in `cdk.json` → `context` (domain, image, alert email, budget, deploy refs). |
| `jolly_infra/stack.py` | The whole stack. |
| `host/bootstrap.sh` | EC2 user-data template: installs Docker, jq and the CloudWatch agent, mounts the data volume, writes the scripts below, runs the first deploy. |
| `host/deploy.sh` | On the box: builds `.env` from SSM + Secrets Manager, pulls the image, restarts, health-checks, **rolls back to the previous image if unhealthy**, starts Caddy. |
| `host/backup.sh`, `host/restore.sh` | Online SQLite backup to S3 and restore from a given key. |
| `host/Caddyfile`, `host/jolly-backup.*`, `host/cloudwatch-agent.json` | Caddy config, systemd timer, memory/disk metrics. |
| `lambda/morning_call/index.py` | The 8 AM bridge. `{"dry_run": true}` only hits `/health`; `{"phone": "+91..."}` overrides the number. |
| `check_stack.py` | Offline assertions on the synthesized template (no AWS account needed). |
| `ecs-express/README.md` | Notes for the post-buildathon Tier 2 (Fargate + RDS pgvector + S3/KMS). |

## Commands

```bash
cd infra/aws
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
npx aws-cdk@2 synth            # offline, no credentials needed
python3 check_stack.py         # template assertions
npx aws-cdk@2 bootstrap aws://<ACCOUNT_ID>/ap-south-1
npx aws-cdk@2 deploy           # review the IAM/security-group diff it prints, then confirm
```

If the account already has a GitHub OIDC provider, set
`"githubOidcProviderArn": "arn:aws:iam::<acct>:oidc-provider/token.actions.githubusercontent.com"`
in `cdk.json` so the stack imports it instead of creating a second one (that would fail).

## Not covered here (by design)

- Building the image: `.github/workflows/build-image.yml` pushes to GHCR on PR merge or `v*` tag.
- Redeploys: `.github/workflows/deploy-aws.yml` (manual, OIDC, SSM).
- The console (static Vite build): Cloudflare Pages or Render static, as today.
- Changes to `host/*.sh` reach a running box only on a new instance. On an existing box, copy them over
  with SSM Session Manager, or replace the instance (the data volume and S3 backups survive).
