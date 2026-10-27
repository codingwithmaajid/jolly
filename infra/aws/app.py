#!/usr/bin/env python3
"""CDK entry point. Region is pinned to ap-south-1 (Mumbai) for data residency."""
import os

import aws_cdk as cdk

from jolly_infra.stack import JollyStack

app = cdk.App()
JollyStack(
    app,
    "Jolly",
    env=cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"), region="ap-south-1"),
    description="Jolly Tier 1: single Graviton box in Mumbai (EC2 + Caddy + SQLite on EBS)",
)
cdk.Tags.of(app).add("project", "jolly")
app.synth()
