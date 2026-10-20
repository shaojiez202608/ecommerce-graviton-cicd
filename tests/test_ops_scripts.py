"""Destructive-change guard, residual-check helpers and bootstrap-asset identification."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


DIFF = """Stack proto-ecom-cicd
There were no differences

Stack proto-ecom-prod-platform
Resources
[~] AWS::RDS::DBInstance Db DbF5A6E3C3 replace
 └─ [~] DBInstanceIdentifier (requires replacement)
     ├─ [-] proto-ecom-prod-mysql
     └─ [+] proto-ecom-prod-mysql2
[~] AWS::ECS::TaskDefinition WebTask WebTask123 replace
[~] AWS::OpenSearchService::Domain Search Search123
 └─ [~] DomainName (requires replacement)

Stack proto-ecom-dev-platform
Resources
[-] AWS::S3::Bucket AlbLogs AlbLogs1234 destroy
[~] AWS::CloudWatch::Alarm EcsCpuHigh EcsCpuHigh12
 └─ [~] Threshold
"""


def test_guard_finds_replacements_and_deletions():
    mod = load("check_destructive")
    found = {(f.stack, f.rtype, f.action) for f in mod.parse(DIFF)}
    assert ("proto-ecom-prod-platform", "AWS::RDS::DBInstance", "replace") in found
    assert ("proto-ecom-prod-platform", "AWS::OpenSearchService::Domain", "replace") in found
    assert ("proto-ecom-dev-platform", "AWS::S3::Bucket", "destroy") in found
    assert not any(t == "AWS::CloudWatch::Alarm" for _, t, _ in found)


def test_guard_refuses_by_default_and_needs_two_flags_for_protected_data():
    mod = load("check_destructive")
    findings = mod.parse(DIFF)
    assert mod.decide(findings, {"uat", "prod"}, {})[0] == 3
    rc, msgs = mod.decide(findings, {"uat", "prod"}, {"ALLOW_DESTRUCTIVE": "1"})
    assert rc == 3 and any("protected" in m for m in msgs)  # prod RDS/OpenSearch still refused
    assert mod.decide(findings, {"uat", "prod"}, {"ALLOW_DESTRUCTIVE": "1", "ALLOW_DATA_REPLACEMENT": "1"})[0] == 0
    dev_only = [f for f in findings if "dev" in f.stack]
    assert mod.decide(dev_only, {"uat", "prod"}, {"ALLOW_DESTRUCTIVE": "1"})[0] == 0
    assert mod.decide([], {"uat", "prod"}, {})[0] == 0


def test_bootstrap_cleanup_only_selects_own_assets(tmp_path):
    mod = load("cleanup_bootstrap_assets")
    assets = {"files": {
        "aaa": {"displayName": "proto-ecom-dev-mcf Template", "source": {"path": "t.json"},
                "destinations": {"d": {"objectKey": "aaa.json"}}},
        "bbb": {"displayName": "IntakeFn/Code", "source": {"path": "asset.bbb"},
                "destinations": {"d": {"objectKey": "bbb.zip"}}},
        "ccc": {"displayName": "proto-ecom-dev-mcf/Custom::S3AutoDeleteObjectsCustomResourceProvider Code",
                "source": {"path": "asset.ccc"}, "destinations": {"d": {"objectKey": "ccc.zip"}}},
        "ddd": {"displayName": "LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a/Code",
                "source": {"path": "asset.ddd"}, "destinations": {"d": {"objectKey": "ddd.zip"}}},
    }}
    (tmp_path / "proto-ecom-dev-mcf.assets.json").write_text(json.dumps(assets))
    (tmp_path / "someone-else.assets.json").write_text(json.dumps({"files": {"eee": {
        "displayName": "x Template", "source": {}, "destinations": {"d": {"objectKey": "eee.json"}}}}}))
    assert mod.own_keys_from_assembly(tmp_path, "proto-ecom") == {"aaa.json", "bbb.zip"}


def test_residual_redaction():
    mod = load("check_residuals")
    assert mod.redact("arn:aws:iam::123456789012:role/x") == "arn:aws:iam::111122223333:role/x"
