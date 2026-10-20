"""Builds all stacks from one config file; shared by app.py and the unit tests."""

from __future__ import annotations

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from .cicd_stack import CicdStack
from .config import AppConfig, load
from .mcf_stack import McfStack
from .nag import apply_suppressions
from .platform_stack import PlatformStack


def build_app(
    app: cdk.App,
    *,
    config_path: str,
    only_envs: list[str] | None = None,
    image_tag: str | None = None,
    account: str | None = None,
    nag: bool = True,
    synth_check: bool = False,
) -> dict[str, cdk.Stack]:
    cfg: AppConfig = load(config_path, synth_check=synth_check)
    # Account stays a CloudFormation token so no account ID ends up in templates or nag output.
    env = cdk.Environment(account=account, region=cfg.region)
    stacks: dict[str, cdk.Stack] = {}

    cicd = CicdStack(app, cfg.name("cicd"), app_cfg=cfg, env=env,
                     description="Graviton CI/CD for an open-source PHP storefront (prototype)")
    stacks[cicd.stack_name] = cicd
    for name, env_cfg in cfg.environments.items():
        if only_envs and name not in only_envs:
            continue
        platform = PlatformStack(app, cfg.name(name, "platform"), app_cfg=cfg, env_cfg=env_cfg,
                                 image_tag=image_tag, env=env,
                                 description=f"Storefront runtime on Graviton - {name} (prototype)")
        platform.env_name = name
        stacks[platform.stack_name] = platform
        if env_cfg.mcf.enabled:
            mcf = McfStack(app, cfg.name(name, "mcf"), app_cfg=cfg, env_cfg=env_cfg, env=env,
                           description=f"Storefront to MCF fulfillment bridge - {name} (prototype)")
            mcf.env_name = name
            stacks[mcf.stack_name] = mcf

    for key, value in cfg.tags.items():
        cdk.Tags.of(app).add(key, value)
    for stack in stacks.values():
        env_name = getattr(stack, "env_name", None)
        apply_suppressions(stack, cfg.project, cfg.ssm_prefix, env_name)
    if nag:
        cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))
    return stacks
