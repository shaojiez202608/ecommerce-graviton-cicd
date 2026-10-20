"""Single configuration file -> typed settings. All tunables live in config/*.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

VALID_ENVS = ("dev", "test", "uat", "prod")
VALID_DEPLOYMENTS = ("rolling", "blue_green")


@dataclass(frozen=True)
class McfConfig:
    enabled: bool
    spapi_mode: str
    spapi_region: str
    marketplaces: dict[str, str]
    fulfillment_policy: str
    max_attempts: int
    tracking_poll_minutes: int
    inventory_sync_minutes: int
    mock_ship_after_seconds: int


@dataclass(frozen=True)
class EnvConfig:
    name: str
    vpc_cidr: str
    max_azs: int
    nat_gateways: int
    deployment: str
    desired_count: int
    task_cpu: int
    task_memory_mib: int
    db_instance_class: str
    db_engine_version: str
    db_multi_az: bool
    cache_node_type: str
    cache_engine_version: str
    search_instance_type: str
    search_engine_version: str
    search_data_nodes: int
    allowed_ingress_cidrs: tuple[str, ...]
    snapshot_before_migrate: bool
    base_url_scheme: str
    mcf: McfConfig
    certificate_arn: str = ""


@dataclass(frozen=True)
class PipelineConfig:
    name: str
    source_key: str
    environments: tuple[str, ...]
    approvals_before: dict[str, str]


@dataclass(frozen=True)
class BuildConfig:
    image_variant: str
    magento_ref: str
    php_image: str
    codebuild_image: str
    block_on_severities: tuple[str, ...]
    alarm_email: str


@dataclass(frozen=True)
class AppConfig:
    project: str
    region: str
    removal_policy: str
    ssm_prefix: str
    tags: dict[str, str]
    build: BuildConfig
    pipelines: tuple[PipelineConfig, ...]
    environments: dict[str, EnvConfig] = field(default_factory=dict)

    @property
    def destroy_on_delete(self) -> bool:
        return self.removal_policy == "destroy"

    def name(self, *parts: str) -> str:
        return "-".join((self.project, *parts))

    def ssm(self, env: str, key: str) -> str:
        return f"{self.ssm_prefix}/{env}/{key}"


def _env(name: str, raw: dict) -> EnvConfig:
    mcf = McfConfig(**raw["mcf"])
    data = {k: v for k, v in raw.items() if k != "mcf"}
    data["allowed_ingress_cidrs"] = tuple(data.get("allowed_ingress_cidrs", []))
    env = EnvConfig(name=name, mcf=mcf, **data)
    if env.deployment not in VALID_DEPLOYMENTS:
        raise ValueError(f"{name}: deployment must be one of {VALID_DEPLOYMENTS}")
    for cidr in env.allowed_ingress_cidrs:
        if cidr.endswith("/0"):
            raise ValueError(f"{name}: {cidr} is not allowed (no 0.0.0.0/0 or ::/0 ingress)")
    if env.mcf.spapi_mode not in ("mock", "live"):
        raise ValueError(f"{name}: spapi_mode must be mock or live")
    if env.deployment == "blue_green" and env.desired_count < 1:
        raise ValueError(f"{name}: blue_green needs desired_count >= 1")
    return env


def load(path: str | Path) -> AppConfig:
    raw = json.loads(Path(path).read_text())
    envs = {}
    for name, value in raw["environments"].items():
        if name not in VALID_ENVS:
            raise ValueError(f"unknown environment {name!r}; expected one of {VALID_ENVS}")
        envs[name] = _env(name, value)
    pipelines = []
    for p in raw["pipelines"]:
        pc = PipelineConfig(
            name=p["name"],
            source_key=p["source_key"],
            environments=tuple(p["environments"]),
            approvals_before=dict(p.get("approvals_before", {})),
        )
        missing = [e for e in pc.environments if e not in envs]
        if missing:
            raise ValueError(f"pipeline {pc.name} references undefined environments {missing}")
        pipelines.append(pc)
    b = raw["build"]
    if b["image_variant"] not in ("magento", "smoke"):
        raise ValueError("build.image_variant must be magento or smoke")
    build = BuildConfig(
        image_variant=b["image_variant"],
        magento_ref=b["magento_ref"],
        php_image=b["php_image"],
        codebuild_image=b["codebuild_image"],
        block_on_severities=tuple(b.get("block_on_severities", ["CRITICAL", "HIGH"])),
        alarm_email=b.get("alarm_email", ""),
    )
    if raw["removal_policy"] not in ("destroy", "retain"):
        raise ValueError("removal_policy must be destroy or retain")
    return AppConfig(
        project=raw["project"],
        region=raw["region"],
        removal_policy=raw["removal_policy"],
        ssm_prefix=raw["ssm_prefix"],
        tags=dict(raw["tags"]),
        build=build,
        pipelines=tuple(pipelines),
        environments=envs,
    )
