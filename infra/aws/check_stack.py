#!/usr/bin/env python3
"""Offline sanity checks on the synthesized template (no AWS account needed).

    pip install -r requirements.txt && python3 check_stack.py
Named check_*.py (not test_*) so the app's `pytest` run doesn't collect it.
"""
import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from jolly_infra.stack import JollyStack

app = cdk.App(context={"alertEmail": "alerts@example.com"})
t = Template.from_stack(JollyStack(app, "Jolly", env=cdk.Environment(region="ap-south-1")))

t.resource_count_is("AWS::EC2::NatGateway", 0)
t.has_resource_properties("AWS::EC2::Instance", {"InstanceType": "t4g.small"})
t.has_resource_properties("AWS::EC2::Volume", {"Encrypted": True, "VolumeType": "gp3"})
t.has_resource_properties("AWS::EC2::LaunchTemplate", {
    "LaunchTemplateData": {"MetadataOptions": {"HttpTokens": "required"}}})
t.has_resource_properties("AWS::S3::Bucket", {
    "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "RestrictPublicBuckets": True},
    "LifecycleConfiguration": Match.object_like({})})
t.has_resource_properties("AWS::Scheduler::Schedule", {
    "ScheduleExpression": "cron(0 8 ? * MON-SAT *)",
    "ScheduleExpressionTimezone": "Asia/Kolkata",
    "Target": Match.object_like({"RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 600}})})
t.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 0})
t.has_resource_properties("AWS::Budgets::Budget", {
    "Budget": Match.object_like({"BudgetLimit": {"Amount": 10, "Unit": "USD"}})})
t.has_resource_properties("AWS::CloudWatch::Alarm", {"MetricName": "StatusCheckFailed_System"})
t.has_resource_properties("AWS::SecretsManager::Secret", {"Name": "jolly/app"})
t.has_resource_properties("AWS::IAM::Role", {"RoleName": "jolly-github-deploy"})
for port in (80, 443):
    t.has_resource_properties("AWS::EC2::SecurityGroup", {
        "SecurityGroupIngress": Match.array_with([Match.object_like({"FromPort": port})])})
print("check_stack: all assertions passed")
