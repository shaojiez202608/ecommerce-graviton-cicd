#!/usr/bin/env python3
"""CDK entry point.

  npx -y aws-cdk@2 deploy --all -c config=config/sandbox.json
  npx -y aws-cdk@2 deploy proto-ecom-dev-platform -c config=config/sandbox.json -c imageTag=<sha>

Context:
  config    path to the single parameter file (default config/sandbox.json)
  envs      comma-separated subset of environments to synthesize (default: all in the file)
  imageTag  image tag currently promoted to the environment(s); omit on first deploy
  synthCheck  "true": fill blank sizing/certificate fields with placeholders so an unfilled profile
            (config/customer.json) can be synthesized and checked with cdk-nag. Not for deployment.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aws_cdk as cdk  # noqa: E402
from ecom_infra.app_builder import build_app  # noqa: E402

app = cdk.App()
config_path = app.node.try_get_context("config") or "config/sandbox.json"
envs = app.node.try_get_context("envs")
image_tag = app.node.try_get_context("imageTag")
synth_check = str(app.node.try_get_context("synthCheck") or "").lower() in ("1", "true", "yes")
build_app(
    app,
    config_path=config_path,
    only_envs=envs.split(",") if envs else None,
    image_tag=image_tag,
    account=None,
    synth_check=synth_check,
)
app.synth()
