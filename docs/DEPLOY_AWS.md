# Deploying Jolly to AWS Mumbai (Tier 1, under 4 hours)

**What you end up with:** `https://api.golnk.in` served from one EC2 `t4g.small` in **ap-south-1 (Mumbai)**,
next to the AWS Mumbai IPs that Bolna calls tools from. It runs the same Docker image as Render. SQLite sits on
an encrypted EBS volume and is backed up to S3 every night. The 8 AM call comes from EventBridge Scheduler,
so nothing has to be woken up any more. Render stays deployed the whole time as the fallback; **DNS and the Bolna
tool URLs are the switch**.

Code: [`infra/aws/`](../infra/aws/README.md) (CDK, Python) · image build: `.github/workflows/build-image.yml`
· redeploys: `.github/workflows/deploy-aws.yml` · Tier 2 notes: [`infra/aws/ecs-express/`](../infra/aws/ecs-express/README.md)

> Prices are USD, ap-south-1, on-demand, from the AWS Price List bulk API on 27 Oct 2026, unless a link says
> otherwise. A month is 730 hours. Nothing in this repo creates AWS resources on its own: you run `cdk deploy`.

---

## Before you start

- A laptop with Node 18+ (`npx`), Python 3.12, the AWS CLI v2 and Docker (optional).
- Access to the Cloudflare zone `golnk.in`, the Bolna dashboard, and the GitHub repo settings.
- Your current values for `LLM_API_KEY`, `BOLNA_API_KEY`, `BOLNA_WEBHOOK_SECRET`, `BOLNA_AGENT_ID`,
  `BOLNA_FROM_NUMBER` (copy them from Render → jolly-backend → Environment).

---

## 0:00–0:20 · Account, credits, guardrails

1. **Create or sign in to the AWS account** and switch the console region to **Asia Pacific (Mumbai) ap-south-1**.
   Turn on MFA for root, then create an admin user (IAM Identity Center/SSO) for daily work. Don't use root again.
2. **Credits** (what the research found):
   - New accounts get **$100 at sign-up plus up to $100 more** for completing activities, valid up to 6 months on
     the Free plan. <https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier.html>
     New-plan accounts can pick `t4g.micro`/`t4g.small` as Free-tier-eligible instance types.
     <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-free-tier-usage.html>
   - **AWS Activate Founders**: $1,000 (up to $5,000 for select participants). Needs a self-funded startup with a
     working company website and a **Paid-plan** account. Apply after the buildathon. <https://aws.amazon.com/startups/credits>
   - **AWS Community Builders**: $500/year (application programme).
     <https://builder.aws.com/content/32g2lQ7kc3Py8kKIYGS15Pe8VSS/aws-community-builders-program>
   - **Student Rewards**: up to $30 in credits for badges.
     <https://www.aboutamazon.com/news/aws/aws-student-rewards-free-cloud-ai-training>
   - The AWS Student Builder Group page lists no fixed credit amount. <https://builder.aws.com/community/student-builder-groups>
   - Free-plan caveat: it "doesn't have access to certain AWS services that would rapidly consume" credits. Tier 1
     only uses EC2, EBS, S3, Lambda, Scheduler, Secrets Manager, SSM, CloudWatch and SNS. If anything is blocked,
     switch to the Paid plan and keep the budget alerts on.
3. **Budget alert in the first 10 minutes.** The stack creates a `$10/month` budget (`budgetUsd` in
   `infra/aws/cdk.json`) that emails `alertEmail` at 80% and 100% of actual spend and at 100% of forecast.
   Until the stack is deployed, add one by hand: Billing → Budgets → *Monthly cost budget* → $10.
   Gross Tier 1 run-rate is about **$16/month** (table below), so a $10 budget is a deliberate early warning;
   raise it to 20 if the forecast email gets noisy.

## 0:20–0:50 · Toolchain and first synth

```bash
aws configure sso                          # or: aws configure (admin keys, laptop only, never in GitHub)
aws sts get-caller-identity                # note the Account id
git checkout jolly/aws-infra               # or main, once the PR is merged
cd infra/aws
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
npx aws-cdk@2 synth                        # must finish with no errors
python3 check_stack.py                     # "all assertions passed"
npx aws-cdk@2 bootstrap aws://<ACCOUNT_ID>/ap-south-1
```

Edit `infra/aws/cdk.json` → `context` if needed: `alertEmail`, `budgetUsd`, `corsOrigins` (the console's URL),
`githubDeployRefs` (which branches may deploy; default `ref:refs/heads/main`). If the account already has a GitHub
OIDC provider, put its ARN in `githubOidcProviderArn`.

## 0:50–1:20 · Image on GHCR

4. Merge the PR (or run **Actions → Build image → Run workflow**). `build-image.yml` pushes
   `ghcr.io/codingwithmaajid/jolly-backend` for `linux/amd64` and `linux/arm64`, tagged `sha-<short>`, the branch
   name, and `latest` on the default branch.
5. **Make the package public** (GitHub → your profile → Packages → `jolly-backend` → Package settings →
   Change visibility → Public). The repo is public, so this exposes nothing new, and the box can pull without a token.
   If you'd rather keep it private, add `GHCR_USER` and `GHCR_TOKEN` (a classic PAT with only `read:packages`)
   to the `jolly/app` secret in step 7; `deploy.sh` logs in when they are present.

## 1:20–1:50 · Deploy the infrastructure

```bash
npx aws-cdk@2 deploy        # read the IAM / security-group summary, answer y
```

6. This creates the VPC (public subnets, **no NAT**), the EC2 `t4g.small` with an Elastic IP, a 10 GB encrypted
   gp3 data volume, the S3 backup bucket, the `jolly/app` secret, SSM parameters `/jolly/env/*`, the morning-call
   Lambda and its schedule, the alarms, the SNS topic, the budget and the `jolly-github-deploy` OIDC role.
   **Confirm the SNS subscription email** AWS sends you, or alarms go nowhere.
   Note the outputs: `PublicIp`, `InstanceId`, `DeployRoleArn`, `BackupBucket`.
7. **Fill the secret** (it is created with `CHANGE_ME` placeholders, which `deploy.sh` ignores):

   ```bash
   aws secretsmanager put-secret-value --region ap-south-1 --secret-id jolly/app --secret-string file://jolly-app.json
   rm jolly-app.json
   ```

   with `jolly-app.json` = `{"LLM_API_KEY":"…","BOLNA_API_KEY":"…","BOLNA_WEBHOOK_SECRET":"…","BOLNA_AGENT_ID":"…","BOLNA_FROM_NUMBER":"+91…","ADVOCATE_PHONE":"+91…"}`.
   Keep `BOLNA_WEBHOOK_SECRET` **identical to Render's** so the existing Bolna agent keeps authenticating.
   Plain settings (CORS, seed flag, `TZ`) live in SSM under `/jolly/env/`; add any other app env var there the same way.
8. **First deploy on the box.** In GitHub → Settings → Secrets and variables → Actions → **Variables**, set
   `AWS_ROLE_ARN` = `DeployRoleArn`, then run **Actions → Deploy to AWS** with tag `latest`.
   (The workflow runs from the default branch, which must match `githubDeployRefs`.)
   Without GitHub: `aws ssm start-session --region ap-south-1 --target <InstanceId>` → `sudo /opt/jolly/deploy.sh latest`.
   The first boot log is `/var/log/jolly-bootstrap.log`.

## 1:50–2:20 · DNS: api.golnk.in via Cloudflare

9. Cloudflare → `golnk.in` → DNS → add `A  api  <PublicIp>` with **Proxy status: DNS only (grey cloud)**.
   Caddy on the box gets a Let's Encrypt certificate over HTTP-01 within a minute.
10. `curl https://api.golnk.in/health` → `{"status":"ok",…}`. Also check `https://api.golnk.in/health/llm`.
11. Switch the record to **Proxied (orange)** and set SSL/TLS mode to **Full (strict)**.
    If a certificate renewal ever fails while proxied, flip to DNS-only for a few minutes or install a Cloudflare
    Origin certificate in Caddy.

## 2:20–2:50 · The 8 AM call

12. Lambda console → the `MorningCallFunction` output → Test with `{"dry_run": true}` (only calls `/health`),
    then `{"phone": "+91XXXXXXXXXX"}` with your own number: the phone should ring.
13. The schedule `jolly-morning-call` fires **08:00 Asia/Kolkata, Mon–Sat** (`cron(0 8 ? * MON-SAT *)` with
    `ScheduleExpressionTimezone`). <https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html>
    Retries are off on purpose, so a slow success never rings twice. Failures email you via the Lambda-errors alarm.
    For a live demo, add a one-time schedule 5 minutes ahead in the Scheduler console with the same target.
14. **Avoid double calls:** delete the GitHub variable `MORNING_CALL_ENABLED` (the `morning-brief.yml` cron then
    only warms Render up), or disable that workflow.

## 2:50–3:20 · Point Bolna at Mumbai

15. Re-point the agent's four custom functions and its webhook from code (same secret, new host):

    ```bash
    BOLNA_API_KEY=… BOLNA_WEBHOOK_SECRET=… JOLLY_BACKEND_URL=https://api.golnk.in \
      python scripts/bolna_agent.py --agent-id <BOLNA_AGENT_ID>
    ```

    Or by hand in the Bolna dashboard: `/bolna/tools/get-case-brief`, `/bolna/tools/get-daily-brief`,
    `/bolna/tools/ask-case-question`, `/bolna/tools/log-hearing-update` and the execution webhook `/bolna/webhook`,
    all on `https://api.golnk.in`.
16. Optional hardening: with the proxy on, the origin sees Cloudflare IPs, so restrict `/bolna/*` to Bolna's
    tool-call IPs (13.203.39.153, 13.126.9.249, 13.202.133.53) with a Cloudflare WAF rule, not in Caddy.
17. Place one inbound "ask" call and one corridor update. Check the timeline entry and the approval link.

## 3:20–3:50 · Reliability checks

18. Alarms are already in place: `StatusCheckFailed_System` → **EC2 auto-recover**, `StatusCheckFailed_Instance` →
    reboot, both also email. Add a free external uptime ping on `https://api.golnk.in/health` if you want
    app-level alerting too.
19. Backups: `aws ssm start-session …` → `sudo systemctl start jolly-backup.service` →
    `aws s3 ls s3://<BackupBucket>/sqlite/`. The timer runs nightly at 03:00 IST; objects expire after 30 days.
    Restore: `sudo /opt/jolly/restore.sh sqlite/jolly-<stamp>.db.gz`.
20. Logs: CloudWatch → Log groups → `/jolly/api` (14-day retention). Memory/disk metrics: namespace `Jolly`.
21. Point the console at the new API (`VITE_API_URL=https://api.golnk.in`) and add its origin to
    `/jolly/env/CORS_ORIGINS`, then redeploy.

## 3:50–4:00 · Freeze

22. Record the demo against Mumbai. Leave Render running.

---

## Day-2 operations

| Task | How |
|---|---|
| Ship a new version | Merge a PR → `build-image.yml` pushes `sha-<short>` → **Deploy to AWS** with that tag. `deploy.sh` health-checks for up to 5 min and **rolls back to the previous image automatically** if `/health` never answers. Expect about a minute of downtime per deploy (one container, startup builds the index). |
| Roll back a version | **Deploy to AWS** with the previous `sha-…` tag (also in SSM `/jolly/last-good-image`). |
| Shell on the box | `aws ssm start-session --region ap-south-1 --target <InstanceId>` (no SSH port is open). |
| Rotate a secret | `put-secret-value` on `jolly/app`, then **Deploy to AWS** (the `.env` is rebuilt on every deploy). |
| Change infra | Edit `infra/aws`, `cdk diff`, `cdk deploy`. Changes to `host/*.sh` only reach new instances; on the live box copy them over in a session. |

## Rollback to Render (minutes)

1. Run `scripts/bolna_agent.py` again with `JOLLY_BACKEND_URL=https://jolly-backend-poqc.onrender.com`
   (or edit the five URLs in Bolna back).
2. Re-enable the GitHub `MORNING_CALL_ENABLED=true` variable, and disable the AWS schedule:
   `aws scheduler update-schedule …` or Scheduler console → `jolly-morning-call` → Disable.
3. Optionally add `api.golnk.in` as a custom domain on Render and CNAME it to the onrender.com host (DNS-only), so the same URL keeps working.
4. Nothing on Render changed, so it is exactly as it was. To stop AWS charges entirely: `npx aws-cdk@2 destroy`.
   The S3 bucket and the secret are retained and the data volume is kept as a snapshot; delete them by hand when sure.

---

## Cost (Tier 1, USD/month, Mumbai)

| Line | Price basis | Per month |
|---|---|---|
| EC2 `t4g.small` (2 GiB, Graviton) | $0.0112/h | $8.18 |
| Public IPv4 (Elastic IP) | $0.005/h | $3.65 |
| EBS gp3 root 20 GB + data 10 GB | $0.0912/GB-month | $2.74 |
| S3 backups (30-day lifecycle) | $0.025/GB-month | ~$0.03 |
| Secrets Manager (1 secret) | $0.40/secret | $0.40 |
| CloudWatch Logs + 3 agent metrics (mem, 2 disks) | $0.67/GB ingested; $0.30/metric | ~$0.34 + $0.90 |
| EventBridge Scheduler + Lambda | free tiers (14M invocations/month free) | $0 |
| SSM standard parameters, SNS email, the budget, 4 alarms | not priced in the research | ≈ $0 expected (verify) |
| **Total** | | **≈ $16** |

Sources: AWS Price List bulk API for ap-south-1 (`https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<Service>/current/ap-south-1/index.json`),
<https://aws.amazon.com/eventbridge/pricing/>. Customer-managed KMS keys ($1/key/month) are not used in Tier 1:
the EBS volumes, S3 bucket and secret use AWS-managed encryption. Not included: LLM usage and Bolna
telephony/STT/TTS, which will dominate at scale. On the $100 sign-up credit, Tier 1 runs for roughly the 6-month
Free-plan maximum.

| Alternative | Monthly | Note |
|---|---|---|
| Render Starter (no sleep) | $7 | No India region, so no residency story. Price from a third-party summary of render.com/pricing (<https://livemy.app/blog/render-pricing>); free tier sleeps after 15 min (<https://render.com/docs/free>). |
| Lightsail VM $7 (1 GB) / $12 (2 GB) | $7–12 | Same box recipe, flat price, outside IAM/VPC tooling. <https://aws.amazon.com/lightsail/pricing/> |
| Tier 2: ECS Express + RDS pgvector | ≈ $74 demo, ≈ $161 at 100 advocates | See `infra/aws/ecs-express/README.md`. |

---

## Data residency and DPDP (India)

- **Timeline.** The DPDP Rules, 2025 were notified on 13/14 Nov 2025 with phased commencement. Rules 3 and 5–16
  (notice, security safeguards, breach notification, retention, cross-border transfer) apply after 18 months,
  around **13/14 May 2027**. <https://pib.gov.in/PressReleasePage.aspx?PRID=2190014>,
  <https://www.dsci.in/files/content/knowledge-centre/2025/Insight-Brief-on-DPDP-Rules.pdf>,
  <https://www.legal500.com/intelligence/india/privacy/cross-border-data-transfers-under-indias-dpdp-act-rules-restrictions-and-compliance-roadmap-for-2027>
  The minister has said the window may be shortened, so plan for earlier.
  <https://indianexpress.com/article/business/govt-shorten-data-protection-compliance-timeline-it-minister-vaishnaw-10370734/>
- **Cross-border transfers** are allowed by default unless the government restricts a country; Significant Data
  Fiduciaries can be required to keep specified data in India. Jolly is not an SDF, but law firms will ask.
  <https://www.cnbctv18.com/technology/government-notifies-dpdp-rules-sets-18-month-roadmap-for-data-protection-regime-ws-e-19760652.htm>
- **Roles.** Advocates are the Data Fiduciaries for their clients' data; Jolly is their processor. Show a consent
  notice to advocates. Advocate–client confidentiality is a separate professional duty (not researched here).
- **What Tier 1 lets you claim:** compute, the SQLite database, backups and logs are all in ap-south-1; disks and
  backups are encrypted at rest; TLS end to end (Cloudflare Full strict → Caddy); no long-lived AWS keys (GitHub OIDC);
  least-privilege instance role; no SSH; 30-day backup retention. Bolna's tool-call IPs are AWS Mumbai addresses
  (checked against <https://ip-ranges.amazonaws.com/ip-ranges.json>), so tool calls stay in-region.
- **Still outside India / not verified:** the Gemini API's processing location; where Bolna stores recordings
  and transcripts and runs STT/TTS/LLM (ask Bolna); Cloudflare's edge terminates TLS wherever the caller is.
  For an India-only LLM path later, Bedrock's `in.` Claude Haiku 4.5 profile routes only to Mumbai and Hyderabad.
  <https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html>
- **Before May 2027:** an approvals audit log, a DB purge job matching the retention policy, and a
  breach-notification runbook.
