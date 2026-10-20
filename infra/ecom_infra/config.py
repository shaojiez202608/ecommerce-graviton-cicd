"""Single configuration file -> typed settings. All tunables live in config/*.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

VALID_ENVS = ("dev", "test", "uat", "prod")
VALID_DEPLOYMENTS = ("rolling", "blue_green")
SPAPI_REGIONS = ("na", "eu", "fe")
# Sizing fields that must be chosen by whoever deploys the profile (no defaults in code or docs).
SIZING_FIELDS = ("db_instance_class", "cache_node_type", "search_instance_type")
SIZING_HELP = (
    "choose a Graviton instance class from the official pages: "
    "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.DBInstanceClass.html , "
    "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/CacheNodes.SupportedTypes.html , "
    "https://docs.aws.amazon.com/opensearch-service/latest/developerguide/supported-instance-types.html"
)
# Used only with synth_check=True (template/cdk-nag checks of a profile whose certificate is not known yet).
SYNTH_CHECK_CERTIFICATE = "synth-check-placeholder"
SANDBOX_PROFILE = Path(__file__).resolve().parents[2] / "config" / "sandbox.json"


@dataclass(frozen=True)
class Marketplace:
    marketplace_id: str
    region: str  # SP-API endpoint group: na | eu | fe


@dataclass(frozen=True)
class McfConfig:
    enabled: bool
    spapi_mode: str
    marketplaces: dict[str, Marketplace]
    fulfillment_policy: str
    max_attempts: int
    tracking_poll_minutes: int
    inventory_sync_minutes: int
    mock_ship_after_seconds: int
    lambda_memory_mib: int
    intake_batch_size: int
    spapi_http_timeout_seconds: int

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted({m.region for m in self.marketplaces.values()}))

    @property
    def intake_timeout_seconds(self) -> int:
        # Worst case per message: LWA token + createFulfillmentOrder + up to two getFulfillmentOrder
        # confirmations, each bounded by the HTTP timeout; plus DynamoDB/EventBridge overhead.
        per_message = 4 * self.spapi_http_timeout_seconds + 10
        return self.intake_batch_size * per_message + 10

    @property
    def intake_visibility_timeout_seconds(self) -> int:
        # SQS guidance for Lambda event sources: visibility timeout >= 6x the function timeout.
        return 6 * self.intake_timeout_seconds

    @property
    def stuck_received_minutes(self) -> int:
        """RECEIVED for longer than all redeliveries can take -> the message went to the DLQ."""
        return (self.intake_visibility_timeout_seconds * (self.max_attempts + 1)) // 60 + 1


@dataclass(frozen=True)
class AlarmConfig:
    cpu_percent: float
    memory_percent: float
    db_free_storage_gib: float
    alb_target_5xx_per_minute: float


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
    db_allocated_storage_gib: int
    cache_node_type: str
    cache_engine_version: str
    search_instance_type: str
    search_engine_version: str
    search_data_nodes: int
    search_volume_gib: int
    allowed_ingress_cidrs: tuple[str, ...]
    snapshot_before_migrate: bool
    predeploy_snapshots_keep: int
    base_url_scheme: str
    maintenance_on_failure: str
    alarms: AlarmConfig
    mcf: McfConfig
    bake_time_minutes: int = 0
    certificate_arn: str = ""
    site_hostname: str = ""

    @property
    def https(self) -> bool:
        return self.base_url_scheme == "https"


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
    scan_timeout_seconds: int = 900
    scan_unavailable_action: str = "fail"


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

    @property
    def protected_environments(self) -> tuple[str, ...]:
        """Environments that sit behind a manual approval in some pipeline."""
        return tuple(sorted({e for p in self.pipelines for e in p.approvals_before}))

    def name(self, *parts: str) -> str:
        return "-".join((self.project, *parts))

    def ssm(self, env: str, key: str) -> str:
        return f"{self.ssm_prefix}/{env}/{key}"


def _mcf(name: str, raw: dict) -> McfConfig:
    markets = {}
    for key, value in raw["marketplaces"].items():
        if not isinstance(value, dict) or not value.get("id") or value.get("region") not in SPAPI_REGIONS:
            raise ValueError(f"{name}: mcf.marketplaces.{key} needs {{'id': <marketplaceId>, 'region': na|eu|fe}}")
        markets[key] = Marketplace(marketplace_id=value["id"], region=value["region"])
    data = {k: v for k, v in raw.items() if k != "marketplaces"}
    mcf = McfConfig(marketplaces=markets, **data)
    if mcf.spapi_mode not in ("mock", "live"):
        raise ValueError(f"{name}: spapi_mode must be mock or live")
    if mcf.intake_batch_size < 1 or mcf.max_attempts < 1:
        raise ValueError(f"{name}: mcf.intake_batch_size and mcf.max_attempts must be >= 1")
    if mcf.intake_timeout_seconds > 900:
        raise ValueError(f"{name}: intake batch too large for the Lambda timeout limit; lower intake_batch_size")
    return mcf


def _env(name: str, raw: dict, synth_check: bool, sizing_fallback: dict) -> EnvConfig:
    mcf = _mcf(name, raw["mcf"])
    alarms = AlarmConfig(**raw["alarms"])
    data = {k: v for k, v in raw.items() if k not in ("mcf", "alarms") and not k.startswith("_")}
    data["allowed_ingress_cidrs"] = tuple(data.get("allowed_ingress_cidrs", []))
    env = EnvConfig(name=name, mcf=mcf, alarms=alarms, **data)

    blanks = [f for f in SIZING_FIELDS if not getattr(env, f)]
    if blanks and synth_check:
        env = replace(env, **{f: sizing_fallback[f] for f in blanks})
    elif blanks:
        raise ValueError(f"{name}: {', '.join(blanks)} not set; {SIZING_HELP}")

    if env.deployment not in VALID_DEPLOYMENTS:
        raise ValueError(f"{name}: deployment must be one of {VALID_DEPLOYMENTS}")
    if env.deployment == "blue_green" and env.desired_count < 1:
        raise ValueError(f"{name}: blue_green needs desired_count >= 1")
    if env.deployment == "blue_green" and env.bake_time_minutes < 0:
        raise ValueError(f"{name}: bake_time_minutes must be >= 0")
    for cidr in env.allowed_ingress_cidrs:
        if cidr.endswith("/0"):
            raise ValueError(f"{name}: {cidr} is not allowed (no 0.0.0.0/0 or ::/0 ingress)")
    if env.base_url_scheme not in ("http", "https"):
        raise ValueError(f"{name}: base_url_scheme must be http or https")
    if env.https:
        missing = [f for f in ("certificate_arn", "site_hostname") if not getattr(env, f)]
        if missing and synth_check:
            env = replace(env, certificate_arn=env.certificate_arn or SYNTH_CHECK_CERTIFICATE,
                          site_hostname=env.site_hostname or f"{name}.shop.example.com")
        elif missing:
            raise ValueError(
                f"{name}: base_url_scheme=https needs {' and '.join(missing)} (an ACM certificate in the "
                "deployment region and the host name it covers); or use base_url_scheme=http"
            )
    if env.maintenance_on_failure not in ("disable", "keep"):
        raise ValueError(f"{name}: maintenance_on_failure must be disable or keep")
    if env.snapshot_before_migrate and env.predeploy_snapshots_keep < 1:
        raise ValueError(f"{name}: predeploy_snapshots_keep must be >= 1 when snapshot_before_migrate is on")
    return env


def _sizing_fallback() -> dict:
    """Synth-check only: borrow the sandbox profile's sizing so a blank profile can be synthesized."""
    dev = json.loads(SANDBOX_PROFILE.read_text())["environments"]["dev"]
    return {f: dev[f] for f in SIZING_FIELDS}


def load(path: str | Path, *, synth_check: bool = False) -> AppConfig:
    """Load and validate a profile.

    synth_check=True fills blank sizing fields from the sandbox profile and blank HTTPS certificate
    fields with placeholders, so a not-yet-filled profile can be synthesized and checked with cdk-nag.
    Templates produced that way are not deployable as-is (the placeholder certificate does not exist).
    """
    raw = json.loads(Path(path).read_text())
    fallback = _sizing_fallback() if synth_check else {}
    envs = {}
    for name, value in raw["environments"].items():
        if name not in VALID_ENVS:
            raise ValueError(f"unknown environment {name!r}; expected one of {VALID_ENVS}")
        envs[name] = _env(name, value, synth_check, fallback)
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
        scan_timeout_seconds=int(b.get("scan_timeout_seconds", 900)),
        scan_unavailable_action=b.get("scan_unavailable_action", "fail"),
    )
    if build.scan_unavailable_action not in ("fail", "warn"):
        raise ValueError("build.scan_unavailable_action must be fail or warn")
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


def env_field_names() -> set[str]:
    return {f.name for f in fields(EnvConfig)}
