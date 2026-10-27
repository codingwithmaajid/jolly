"""Jolly Tier 1 on AWS: one Graviton box in Mumbai (ap-south-1).

EC2 t4g.small (AL2023 arm64) runs the GHCR image behind Caddy, SQLite lives on an
encrypted gp3 data volume, backups go to S3 nightly, EventBridge Scheduler fires a
Lambda for the 08:00 IST call, secrets sit in Secrets Manager, plain config in SSM,
plus a budget alarm, instance status alarms, and an OIDC role for GitHub Actions.

Runbook, costs and sources: docs/DEPLOY_AWS.md.
Nothing here is deployed by CI; you run `cdk deploy` yourself.
"""

from __future__ import annotations

import base64
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    SecretValue,
    Size,
    Stack,
    Tags,
    TimeZone,
    aws_budgets as budgets,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_scheduler as scheduler,
    aws_scheduler_targets as targets,
    aws_secretsmanager as sm,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_ssm as ssm,
)
from constructs import Construct

HERE = Path(__file__).resolve().parent.parent  # infra/aws
HOST = HERE / "host"

# Keys the app reads from Secrets Manager. Fill real values after deploy
# (console or `aws secretsmanager put-secret-value`); CHANGE_ME values are skipped.
SECRET_KEYS = (
    "LLM_API_KEY",
    "BOLNA_API_KEY",
    "BOLNA_WEBHOOK_SECRET",
    "BOLNA_AGENT_ID",
    "BOLNA_FROM_NUMBER",
    "ADVOCATE_PHONE",
)


def _b64(name: str) -> str:
    return base64.b64encode((HOST / name).read_bytes()).decode()


class JollyStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw) -> None:
        super().__init__(scope, cid, **kw)

        ctx = self.node.try_get_context
        domain: str = ctx("domain") or "api.golnk.in"
        image: str = ctx("image") or "ghcr.io/codingwithmaajid/jolly-backend"
        github_repo: str = ctx("githubRepo") or "codingwithmaajid/jolly"
        deploy_refs: list[str] = ctx("githubDeployRefs") or ["ref:refs/heads/main"]
        oidc_provider_arn: str | None = ctx("githubOidcProviderArn") or None
        alert_email: str | None = ctx("alertEmail") or None
        budget_usd: float = float(ctx("budgetUsd") or 10)
        instance_type: str = ctx("instanceType") or "t4g.small"
        cors_origins: str = ctx("corsOrigins") or "https://golnk.in"
        param_prefix = "/jolly"

        # ---------- Network: one VPC, public subnets only, no NAT ($0) ----------
        vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)
            ],
        )
        sg = ec2.SecurityGroup(self, "BoxSg", vpc=vpc, allow_all_outbound=True,
                               description="Jolly box: HTTP/HTTPS only; admin via SSM Session Manager (no SSH)")
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS (tighten to Cloudflare ranges once proxied)")
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "ACME HTTP-01 for Caddy + redirect")

        # ---------- Secrets (Secrets Manager) and plain config (SSM) ----------
        app_secret = sm.Secret(
            self, "AppSecret",
            secret_name="jolly/app",
            description="Jolly runtime secrets (JSON). Replace every CHANGE_ME after deploy.",
            secret_object_value={k: SecretValue.unsafe_plain_text("CHANGE_ME") for k in SECRET_KEYS},
            removal_policy=RemovalPolicy.RETAIN,
        )
        plain_env = {
            "APP_ENV": "production",
            "APP_NAME": "Jolly",
            "LOG_LEVEL": "INFO",
            "SEED_DEMO_DATA": "true",
            "BOLNA_BASE_URL": "https://api.bolna.ai",
            "CORS_ORIGINS": cors_origins,
            # POSIX form works even without tzdata in the slim image; makes date.today() IST.
            "TZ": "IST-5:30",
        }
        for key, value in plain_env.items():
            ssm.StringParameter(self, f"Env{key.title().replace('_', '')}",
                                parameter_name=f"{param_prefix}/env/{key}", string_value=value)
        ssm.StringParameter(self, "ImageTag", parameter_name=f"{param_prefix}/image-tag", string_value="latest",
                            description="Tag deploy.sh pulls when none is given; the deploy workflow updates it")

        # ---------- Backups: S3, private, versioned, lifecycle ----------
        backups = s3.Bucket(
            self, "Backups",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(id="expire-sqlite-backups", prefix="sqlite/",
                                 expiration=Duration.days(30),
                                 noncurrent_version_expiration=Duration.days(7)),
                s3.LifecycleRule(id="abort-mpu", abort_incomplete_multipart_upload_after=Duration.days(1)),
            ],
        )

        log_group = logs.LogGroup(self, "ApiLogs", log_group_name="/jolly/api",
                                  retention=logs.RetentionDays.TWO_WEEKS,
                                  removal_policy=RemovalPolicy.DESTROY)

        # ---------- Instance role (least privilege) ----------
        role = iam.Role(
            self, "BoxRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"),
            ],
        )
        app_secret.grant_read(role)
        backups.grant_read_write(role)
        log_group.grant_write(role)
        role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
            resources=[self.format_arn(service="ssm", resource="parameter", resource_name=f"{param_prefix.lstrip('/')}/*")],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:PutParameter"],
            resources=[self.format_arn(service="ssm", resource="parameter",
                                       resource_name=f"{param_prefix.lstrip('/')}/last-good-image")],
        ))

        # ---------- The box ----------
        box = ec2.Instance(
            self, "Box",
            vpc=vpc,
            # Pin the box to the first public subnet so the data volume can share its AZ
            # without a Box <-> Volume dependency cycle.
            vpc_subnets=ec2.SubnetSelection(subnets=[vpc.public_subnets[0]]),
            security_group=sg,
            role=role,
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(cpu_type=ec2.AmazonLinuxCpuType.ARM_64),
            require_imdsv2=True,
            block_devices=[ec2.BlockDevice(
                device_name="/dev/xvda",
                volume=ec2.BlockDeviceVolume.ebs(20, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True),
            )],
        )
        Tags.of(box).add("app", "jolly")  # the deploy workflow targets this tag

        data_volume = ec2.Volume(
            self, "DataVolume",
            availability_zone=vpc.public_subnets[0].availability_zone,
            size=Size.gibibytes(10),
            volume_type=ec2.EbsDeviceVolumeType.GP3,
            encrypted=True,
            removal_policy=RemovalPolicy.SNAPSHOT,
        )
        ec2.CfnVolumeAttachment(self, "DataVolumeAttach", instance_id=box.instance_id,
                                volume_id=data_volume.volume_id, device="/dev/sdf")

        script = (HOST / "bootstrap.sh").read_text()
        replacements = {
            "__REGION__": self.region,
            "__SECRET_ID__": "jolly/app",
            "__PARAM_PREFIX__": param_prefix,
            "__BACKUP_BUCKET__": backups.bucket_name,
            "__LOG_GROUP__": log_group.log_group_name,
            "__DOMAIN__": domain,
            "__IMAGE__": image,
            "__DATA_VOLUME_ID__": data_volume.volume_id,
            "__B64_DEPLOY__": _b64("deploy.sh"),
            "__B64_BACKUP__": _b64("backup.sh"),
            "__B64_RESTORE__": _b64("restore.sh"),
            "__B64_CADDY__": _b64("Caddyfile"),
            "__B64_SVC__": _b64("jolly-backup.service"),
            "__B64_TIMER__": _b64("jolly-backup.timer"),
            "__B64_CWA__": _b64("cloudwatch-agent.json"),
        }
        for placeholder, value in replacements.items():
            script = script.replace(placeholder, value)
        box.add_user_data(script)

        eip = ec2.CfnEIP(self, "Eip", domain="vpc", tags=[{"key": "app", "value": "jolly"}])
        ec2.CfnEIPAssociation(self, "EipAssoc", allocation_id=eip.attr_allocation_id, instance_id=box.instance_id)

        # ---------- Alerts ----------
        topic = sns.Topic(self, "Alerts", display_name="Jolly alerts")
        if alert_email:
            topic.add_subscription(subs.EmailSubscription(alert_email))
        notify = cw_actions.SnsAction(topic)

        def status_alarm(cid: str, metric_name: str, action: cw.IAlarmAction | None, what: str) -> cw.Alarm:
            alarm = cw.Alarm(
                self, cid,
                alarm_description=what,
                metric=cw.Metric(namespace="AWS/EC2", metric_name=metric_name,
                                 dimensions_map={"InstanceId": box.instance_id},
                                 statistic="Maximum", period=Duration.minutes(1)),
                threshold=1, evaluation_periods=2,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.MISSING,
            )
            alarm.add_alarm_action(notify)
            if action is not None:
                alarm.add_alarm_action(action)
            return alarm

        status_alarm("SystemStatusAlarm", "StatusCheckFailed_System",
                     cw_actions.Ec2Action(cw_actions.Ec2InstanceAction.RECOVER),
                     "AWS host problem: auto-recover the Jolly box")
        status_alarm("InstanceStatusAlarm", "StatusCheckFailed_Instance",
                     cw_actions.Ec2Action(cw_actions.Ec2InstanceAction.REBOOT),
                     "Jolly box OS unreachable: reboot it")

        # Monthly cost budget, emailed at 80% actual and 100% forecast.
        if alert_email:
            subscriber = budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=alert_email)
            budgets.CfnBudget(
                self, "Budget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_name="jolly-monthly",
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(amount=budget_usd, unit="USD"),
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            notification_type=ntype, comparison_operator="GREATER_THAN",
                            threshold=pct, threshold_type="PERCENTAGE"),
                        subscribers=[subscriber],
                    )
                    for ntype, pct in (("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100))
                ],
            )

        # ---------- 08:00 IST morning call: Scheduler -> Lambda -> API ----------
        fn = lambda_.Function(
            self, "MorningCall",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset(str(HERE / "lambda" / "morning_call")),
            timeout=Duration.seconds(60),
            memory_size=128,
            retry_attempts=0,  # never auto-retry: a retry after a slow success would ring twice
            environment={"API_URL": f"https://{domain}", "SECRET_ID": "jolly/app"},
            log_group=logs.LogGroup(self, "MorningCallLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=RemovalPolicy.DESTROY),
            description="Jolly: POST /briefs/morning-call",
        )
        app_secret.grant_read(fn)
        scheduler.Schedule(
            self, "MorningSchedule",
            schedule_name="jolly-morning-call",
            description="Jolly 08:00 IST brief call, Mon-Sat",
            schedule=scheduler.ScheduleExpression.cron(
                minute="0", hour="8", week_day="MON-SAT", time_zone=TimeZone.ASIA_KOLKATA),
            target=targets.LambdaInvoke(fn, input=scheduler.ScheduleTargetInput.from_object({}),
                                        retry_attempts=0, max_event_age=Duration.minutes(10)),
        )
        err_alarm = cw.Alarm(
            self, "MorningCallErrors",
            alarm_description="The 8 AM call Lambda failed",
            metric=fn.metric_errors(period=Duration.minutes(5), statistic="Sum"),
            threshold=1, evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        err_alarm.add_alarm_action(notify)

        # ---------- GitHub Actions OIDC deploy role ----------
        if oidc_provider_arn:
            provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(self, "GitHubOidc", oidc_provider_arn)
        else:
            provider = iam.OpenIdConnectProvider(self, "GitHubOidc",
                                                 url="https://token.actions.githubusercontent.com",
                                                 client_ids=["sts.amazonaws.com"])
        deploy_role = iam.Role(
            self, "GitHubDeployRole",
            role_name="jolly-github-deploy",
            description="Assumed by GitHub Actions (OIDC) to redeploy the Jolly box via SSM",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(provider.open_id_connect_provider_arn, conditions={
                "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                "StringLike": {"token.actions.githubusercontent.com:sub":
                               [f"repo:{github_repo}:{r}" for r in deploy_refs]},
            }),
        )
        instance_arn = self.format_arn(service="ec2", resource="instance", resource_name=box.instance_id)
        deploy_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:SendCommand"],
            resources=[instance_arn,
                       self.format_arn(service="ssm", account="", resource="document",
                                       resource_name="AWS-RunShellScript")],
        ))
        deploy_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ec2:DescribeInstances"],
            resources=["*"],
        ))
        deploy_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:PutParameter", "ssm:GetParameter"],
            resources=[self.format_arn(service="ssm", resource="parameter",
                                       resource_name=f"{param_prefix.lstrip('/')}/image-tag")],
        ))

        # ---------- Outputs ----------
        CfnOutput(self, "PublicIp", value=eip.ref, description=f"Point an A record for {domain} here")
        CfnOutput(self, "InstanceId", value=box.instance_id)
        CfnOutput(self, "DeployRoleArn", value=deploy_role.role_arn, description="GitHub repo variable AWS_ROLE_ARN")
        CfnOutput(self, "SecretName", value=app_secret.secret_name)
        CfnOutput(self, "BackupBucket", value=backups.bucket_name)
        CfnOutput(self, "MorningCallFunction", value=fn.function_name)
        CfnOutput(self, "AlertsTopic", value=topic.topic_arn)
        CfnOutput(self, "ShellCommand", value=f"aws ssm start-session --region {self.region} --target {box.instance_id}")
