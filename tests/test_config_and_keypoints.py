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
    customer = load(ROOT / "config" / "customer.json", synth_check=True)
    assert set(customer.environments) == {"dev", "test", "uat", "prod"}
    assert load(ROOT / "config" / "sandbox.json").destroy_on_delete


def test_customer_profile_requires_sizing_to_be_filled_in():
    with pytest.raises(ValueError, match="db_instance_class, cache_node_type, search_instance_type not set"):
        load(ROOT / "config" / "customer.json")
    raw = json.loads((ROOT / "config" / "customer.json").read_text())
    for env in raw["environments"].values():
        assert env["db_instance_class"] == env["cache_node_type"] == env["search_instance_type"] == ""


def test_no_instance_type_names_outside_the_sandbox_profile():
    """Instance classes live only in config/sandbox.json (deployable sandbox values)."""
    pattern = re.compile(r"\b(?:db\.|cache\.)?[cmrtix]\d+g[dn]*\b")  # instance family tokens
    for path in ROOT.rglob("*"):
        if (not path.is_file() or path.suffix not in (".py", ".json", ".md", ".sh", ".yml", ".php")
                or any(part in {".venv", "cdk.out", "node_modules", ".git"} for part in path.parts)
                or path.name == "sandbox.json"):
            continue
        assert not pattern.search(path.read_text()), path


def test_sizing_and_thresholds_come_from_the_profile():
    cfg = load(ROOT / "config" / "sandbox.json")
    dev = cfg.environments["dev"]
    assert dev.db_allocated_storage_gib > 0 and dev.search_volume_gib > 0
    assert dev.alarms.cpu_percent > 0 and dev.mcf.lambda_memory_mib > 0


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r["environments"]["dev"].update(allowed_ingress_cidrs=["0.0.0.0/0"]), "not allowed"),
    (lambda r: r["environments"].update(staging=r["environments"]["dev"]), "unknown environment"),
    (lambda r: r["pipelines"][0].update(environments=["prod"]), "undefined environments"),
    (lambda r: r["environments"]["dev"]["mcf"].update(spapi_mode="real"), "spapi_mode"),
    (lambda r: r["environments"]["dev"].update(deployment="canary"), "deployment"),
    (lambda r: r["environments"]["dev"].update(db_instance_class=""), "not set"),
    (lambda r: r["environments"]["dev"].update(base_url_scheme="https"), "needs certificate_arn and site_hostname"),
    (lambda r: r["environments"]["dev"].update(maintenance_on_failure="maybe"), "maintenance_on_failure"),
    (lambda r: r["environments"]["dev"]["mcf"]["marketplaces"].update(DE={"id": "X"}), "region"),
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


MIGRATE = (ROOT / "app" / "magento" / "docker" / "migrate.sh").read_text()
DEPLOY = (ROOT / "scripts" / "deploy" / "ecs_deploy.sh").read_text()


def test_migration_traps_signals_and_serializes_with_a_db_lock():
    assert "trap cleanup EXIT" in MIGRATE and "trap 'exit 143' TERM" in MIGRATE and "trap 'exit 130' INT" in MIGRATE
    assert "hold-lock" in MIGRATE and "exit 75" in MIGRATE
    tool = (ROOT / "app" / "magento" / "docker" / "build-env.php").read_text()
    assert "GET_LOCK(" in tool
    assert MIGRATE.index("migration lock acquired") < MIGRATE.index("bin/magento setup:install")


def test_maintenance_window_closes_only_after_new_tasks_serve():
    upgrade_branch = MIGRATE.split("upgrade required", 1)[1]
    assert "maintenance:enable" in upgrade_branch and "maintenance:disable" not in upgrade_branch
    flush = (ROOT / "app" / "magento" / "docker" / "cache-flush.sh").read_text()
    assert flush.index("cache:flush") < flush.index("maintenance:disable")
    # deploy: migrate -> update service -> wait COMPLETED -> cache-flush (closes the window)
    assert DEPLOY.index("magento-migrate") < DEPLOY.index("aws ecs update-service") \
        < DEPLOY.index('"$ROLLOUT" = "COMPLETED"') < DEPLOY.rindex("magento-cache-flush")


def test_deploy_failure_and_timeout_paths_handle_the_maintenance_flag():
    assert "trap on_exit EXIT" in DEPLOY and 'magento-maintenance $MAINT_POLICY' in DEPLOY
    assert "aws ecs stop-task" in DEPLOY  # timed-out one-off tasks are stopped, not abandoned
    policy = (ROOT / "app" / "magento" / "docker" / "maintenance.sh").read_text()
    assert 'if [ "$policy" = "disable" ]' in policy and "var/.deploy-maintenance" in policy
    smoke = (ROOT / "app" / "smoke" / "Dockerfile").read_text()
    assert "/usr/local/bin/magento-maintenance" in smoke


def test_deploy_verifies_image_before_any_change():
    gate = DEPLOY.index("--verify-only")
    assert gate < DEPLOY.index("register \"$WEB_FAMILY\"") and gate < DEPLOY.index("create-db-snapshot")


def test_predeploy_snapshots_are_pruned():
    assert "delete-db-snapshot" in DEPLOY and "SNAP_KEEP" in DEPLOY


def load_gate():
    import importlib.util

    spec = importlib.util.spec_from_file_location("scan_gate", ROOT / "scripts" / "build" / "scan_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_gate_quarantines_blocked_images(monkeypatch, tmp_path):
    mod = load_gate()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "describe", lambda repo, tag: {
        "imageScanStatus": {"status": "COMPLETE"}, "imageScanFindings": {"findingSeverityCounts": {"HIGH": 1}}})
    calls = []

    class R:
        returncode, stdout, stderr = 0, '{"manifest": 1}', ""

    monkeypatch.setattr(mod, "aws", lambda *a: calls.append(a) or R())
    assert mod.main(["x", "repo", "tag1", "CRITICAL,HIGH", "5", "fail", "--quarantine"]) == 1
    assert [c[0] for c in calls] == ["batch-get-image", "put-image", "batch-delete-image"]
    assert "quarantine-tag1" in calls[1]
    calls.clear()
    assert mod.main(["x", "repo", "tag1", "CRITICAL,HIGH", "5", "fail", "--verify-only"]) == 1
    assert calls == []  # deploy-time verification never changes tags
    monkeypatch.setattr(mod, "describe", lambda repo, tag: {
        "imageScanStatus": {"status": "COMPLETE"}, "imageScanFindings": {"findingSeverityCounts": {"LOW": 3}}})
    assert mod.main(["x", "repo", "tag1", "CRITICAL,HIGH", "5", "fail", "--quarantine"]) == 0 and calls == []


def test_promote_refuses_protected_environments_without_break_glass():
    import subprocess

    out = subprocess.run(["bash", str(ROOT / "scripts" / "promote_image.sh"), "prod", "abc"],
                         env={"ECOM_CONFIG": "config/customer.json", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
                         capture_output=True, text=True, check=False)
    assert out.returncode == 2 and "refusing" in out.stderr
