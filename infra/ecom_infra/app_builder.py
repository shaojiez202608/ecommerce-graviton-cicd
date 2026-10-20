"""Builds all stacks from one config file; shared by app.py and the unit tests."""

from __future__ import annotations

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from .cicd_stack import CicdStack
from .config import AppConfig, load
from .mcf_stack import McfStack
from .nag import apply_suppressions
from .platform_stack import PlatformStack
from .trust import apply_trust_conditions


def build_app(
    app: cdk.App,
    *,
    config_path: str,
    only_envs: list[str] | None = None,
    image_tag: str | None = None,
    image_tags: dict[str, str] | None = None,
    account: str | None = None,
    nag: bool = True,
    synth_check: bool = False,
) -> dict[str, cdk.Stack]:
    cfg: AppConfig = load(config_path, synth_check=synth_check)
    selected = [e for e in cfg.environments if not only_envs or e in only_envs]
    tags = resolve_image_tags(selected, image_tag, image_tags or {})
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
                                 image_tag=tags.get(name), env=env,
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
        apply_trust_conditions(stack, cfg)
        env_name = getattr(stack, "env_name", None)
        apply_suppressions(stack, cfg, cfg.environments[env_name] if env_name else None)
    if nag:
        cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))
    return stacks


def resolve_image_tags(selected: list[str], image_tag: str | None, image_tags: dict[str, str]) -> dict[str, str]:
    """Per-environment image tags. A single -c imageTag is accepted only when exactly one
    environment is synthesized, so one environment's tag can never be applied to another."""
    unknown = sorted(set(image_tags) - set(selected))
    if unknown:
        raise ValueError(f"imageTags names environments that are not synthesized: {unknown}")
    if image_tag:
        if len(selected) != 1:
            raise ValueError("-c imageTag applies to one environment only; with several environments use "
                             "-c imageTags=dev=<TAG>,test=<TAG> (or -c envs=<ENV>)")
        return {selected[0]: image_tag, **image_tags}
    return dict(image_tags)


def parse_image_tags(raw: str | None) -> dict[str, str]:
    """'dev=abc,prod=def' -> {'dev': 'abc', 'prod': 'def'}"""
    out = {}
    for part in filter(None, (raw or "").split(",")):
        env, sep, tag = part.partition("=")
        if not sep or not env or not tag:
            raise ValueError(f"imageTags entry {part!r} must look like <ENV>=<TAG>")
        out[env.strip()] = tag.strip()
    return out
