"""Config validation and guards for the four Magento CI/CD key points."""

import json
import re
from pathlib import Path

import pytest
from ecom_infra.config import load

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, mutate):
    raw = json.loads((ROOT / "config" / "sandbox.json").read_text())
    mutate(raw)
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw))
    return p


def test_profiles_load():
    assert set(load(ROOT / "config" / "customer.json").environments) == {"dev", "test", "uat", "prod"}
    assert load(ROOT / "config" / "sandbox.json").destroy_on_delete


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r["environments"]["dev"].update(allowed_ingress_cidrs=["0.0.0.0/0"]), "not allowed"),
    (lambda r: r["environments"].update(staging=r["environments"]["dev"]), "unknown environment"),
    (lambda r: r["pipelines"][0].update(environments=["prod"]), "undefined environments"),
    (lambda r: r["environments"]["dev"]["mcf"].update(spapi_mode="real"), "spapi_mode"),
    (lambda r: r["environments"]["dev"].update(deployment="canary"), "deployment"),
])
def test_invalid_config_rejected(tmp_path, mutate, match):
    with pytest.raises(ValueError, match=match):
        load(write(tmp_path, mutate))


DOCKERFILE = (ROOT / "app" / "magento" / "Dockerfile").read_text()


def builder_stage():
    return DOCKERFILE.split("FROM base AS builder", 1)[1].split("FROM base AS runtime", 1)[0]


def test_key_point_1_immutable_image_build_steps_in_builder():
    stage = builder_stage()
    for step in ("composer install", "setup:di:compile", "setup:static-content:deploy"):
        assert step in stage
    runtime = DOCKERFILE.split("FROM base AS runtime", 1)[1]
    assert "composer" not in runtime.split("COPY --from=builder", 1)[1]


def test_key_point_2_env_php_not_in_image():
    assert "rm -f app/etc/env.php" in builder_stage()
    entry = (ROOT / "app" / "magento" / "docker" / "entrypoint.sh").read_text()
    assert "env-tool.php render app/etc/env.php" in entry


def test_key_point_3_migrations_only_in_one_off_task():
    entry = (ROOT / "app" / "magento" / "docker" / "entrypoint.sh").read_text()
    code = "\n".join(ln for ln in entry.splitlines() if not ln.lstrip().startswith("#"))
    assert "setup:upgrade" not in code and "setup:install" not in code
    migrate = (ROOT / "app" / "magento" / "docker" / "migrate.sh").read_text()
    assert "maintenance:enable" in migrate and "setup:upgrade --keep-generated" in migrate
    deploy = (ROOT / "scripts" / "deploy" / "ecs_deploy.sh").read_text()
    # migration runs before the service update, and concurrent ops tasks are refused
    assert deploy.index("magento-migrate") < deploy.index("aws ecs update-service")
    assert "refusing to start a concurrent" in deploy


def test_key_point_4_cache_flush_after_service_stable():
    deploy = (ROOT / "scripts" / "deploy" / "ecs_deploy.sh").read_text()
    assert deploy.index("services-stable") < deploy.index("magento-cache-flush")


def test_build_tags_image_with_commit_sha_and_scan_gate():
    spec = (ROOT / "buildspec" / "build.yml").read_text()
    assert re.search(r'IMAGE_TAG="\$\{REVISION', spec)
    assert "scan_gate.py" in spec
    assert "--platform linux/arm64" in spec


def test_scan_gate_counts_basic_and_enhanced():
    import importlib.util

    spec = importlib.util.spec_from_file_location("scan_gate", ROOT / "scripts" / "build" / "scan_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.severity_counts({"imageScanFindings": {"findingSeverityCounts": {"HIGH": 2}}}) == {"HIGH": 2}
    enhanced = {"imageScanFindings": {"enhancedFindings": [{"severity": "CRITICAL"}, {"severity": "LOW"}]}}
    assert mod.severity_counts(enhanced) == {"CRITICAL": 1, "LOW": 1}
