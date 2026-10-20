"""CDK assertions for both profiles (sandbox: dev only; customer: four environments)."""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from ecom_infra.app_builder import build_app

ROOT = Path(__file__).resolve().parents[1]


def synth(profile: str, image_tag=None, nag=True):
    app = cdk.App(context={"@aws-cdk/customresources:installLatestAwsSdkDefault": False})
    stacks = build_app(app, config_path=str(ROOT / "config" / f"{profile}.json"), image_tag=image_tag, nag=nag,
                       synth_check=(profile == "customer"))
    return app, {name: Template.from_stack(s) for name, s in stacks.items()}


@pytest.fixture(scope="module")
def sandbox():
    return synth("sandbox")[1]


@pytest.fixture(scope="module")
def customer():
    return synth("customer", image_tag="abc123")[1]


def all_resources(templates, rtype):
    out = []
    for t in templates.values():
        out.extend(t.find_resources(rtype).values())
    return out


def test_stack_names_are_prefixed(sandbox, customer):
    assert set(sandbox) == {"proto-ecom-cicd", "proto-ecom-dev-platform", "proto-ecom-dev-mcf"}
    assert {f"proto-ecom-{e}-platform" for e in ("dev", "test", "uat", "prod")} <= set(customer)
    assert all(n.startswith("proto-ecom-") for n in customer)


def test_cdk_nag_passes_with_documented_acknowledgements():
    for profile in ("sandbox", "customer"):
        app, _ = synth(profile, nag=True)
        app.synth()  # raises if any AwsSolutions finding is unacknowledged


def test_no_open_ingress(sandbox, customer):
    for templates in (sandbox, customer):
        for sg in all_resources(templates, "AWS::EC2::SecurityGroup"):
            for rule in sg["Properties"].get("SecurityGroupIngress", []):
                assert rule.get("CidrIp") != "0.0.0.0/0" and rule.get("CidrIpv6") != "::/0"
        for rule in all_resources(templates, "AWS::EC2::SecurityGroupIngress"):
            assert rule["Properties"].get("CidrIp") != "0.0.0.0/0"


def test_alb_is_internal(sandbox):
    sandbox["proto-ecom-dev-platform"].has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internal"})


def test_everything_compute_is_arm64(customer):
    for td in all_resources(customer, "AWS::ECS::TaskDefinition"):
        assert td["Properties"]["RuntimePlatform"]["CpuArchitecture"] == "ARM64"
    own_lambdas = [f for f in all_resources(customer, "AWS::Lambda::Function")
                   if "mcf_bridge" in json.dumps(f["Properties"].get("Handler", ""))]
    assert own_lambdas and all(f["Properties"]["Architectures"] == ["arm64"] for f in own_lambdas)
    for p in all_resources(customer, "AWS::CodeBuild::Project"):
        env = p["Properties"]["Environment"]
        assert env["Type"] == "ARM_CONTAINER"
        assert env["Image"] == "aws/codebuild/amazonlinux-aarch64-standard:3.0"


def test_graviton_instance_families(customer):
    for db in all_resources(customer, "AWS::RDS::DBInstance"):
        assert db["Properties"]["DBInstanceClass"].split(".")[1].endswith("g")
        assert db["Properties"]["StorageEncrypted"] is True
        assert db["Properties"]["Engine"] == "mysql"
    for rg in all_resources(customer, "AWS::ElastiCache::ReplicationGroup"):
        p = rg["Properties"]
        assert p["Engine"] == "valkey" and p["CacheNodeType"].split(".")[1].endswith("g")
        assert p["TransitEncryptionEnabled"] and p["AtRestEncryptionEnabled"]
    for d in all_resources(customer, "AWS::OpenSearchService::Domain"):
        p = d["Properties"]
        assert p["ClusterConfig"]["InstanceType"].split(".")[0].endswith("g")
        assert p["NodeToNodeEncryptionOptions"]["Enabled"] and p["EncryptionAtRestOptions"]["Enabled"]
        assert "VPCOptions" in p and p["DomainEndpointOptions"]["EnforceHTTPS"]


def test_rolling_vs_blue_green(customer):
    def service(env):
        return next(iter(customer[f"proto-ecom-{env}-platform"].find_resources("AWS::ECS::Service").values()))["Properties"]
    for env in ("dev", "test"):
        cfg = service(env)["DeploymentConfiguration"]
        assert cfg["DeploymentCircuitBreaker"] == {"Enable": True, "Rollback": True}
        assert cfg.get("Strategy") in (None, "ROLLING")
    for env in ("uat", "prod"):
        s = service(env)
        assert s["DeploymentConfiguration"]["Strategy"] == "BLUE_GREEN"
        assert "AlternateTargetGroupArn" in s["LoadBalancers"][0]["AdvancedConfiguration"]


def test_desired_count_zero_until_an_image_is_promoted(sandbox, customer):
    sandbox["proto-ecom-dev-platform"].has_resource_properties("AWS::ECS::Service", {"DesiredCount": 0})
    customer["proto-ecom-prod-platform"].has_resource_properties("AWS::ECS::Service", {"DesiredCount": 2})


def test_release_pipeline_has_two_approvals_and_stage_rollback(customer):
    pipelines = customer["proto-ecom-cicd"].find_resources("AWS::CodePipeline::Pipeline")
    release = next(p for p in pipelines.values() if p["Properties"]["Name"] == "proto-ecom-pipeline-release")["Properties"]
    names = [s["Name"] for s in release["Stages"]]
    assert names == ["Source", "Build", "Deploy-test", "Approve-uat", "Deploy-uat", "Approve-prod", "Deploy-prod"]
    approvals = [a for s in release["Stages"] for a in s["Actions"] if a["ActionTypeId"]["Category"] == "Approval"]
    assert len(approvals) == 2
    for s in release["Stages"]:
        if s["Name"].startswith("Deploy-"):
            assert s["OnFailure"] == {"Result": "ROLLBACK"}
    assert release["PipelineType"] == "V2" and release["ExecutionMode"] == "QUEUED"
    develop = next(p for p in pipelines.values() if p["Properties"]["Name"] == "proto-ecom-pipeline-develop")
    assert [s["Name"] for s in develop["Properties"]["Stages"]] == ["Source", "Build", "Deploy-dev"]


def test_one_build_promoted_by_tag(customer):
    """Every deploy stage consumes the single build artifact (no per-environment build)."""
    pipelines = customer["proto-ecom-cicd"].find_resources("AWS::CodePipeline::Pipeline")
    for p in pipelines.values():
        builds = [a for s in p["Properties"]["Stages"] for a in s["Actions"]
                  if a["ActionTypeId"]["Category"] == "Build" and a["Name"] == "BuildImageArm64"]
        assert len(builds) == 1
        for s in p["Properties"]["Stages"]:
            if s["Name"].startswith("Deploy-"):
                assert s["Actions"][0]["InputArtifacts"] == [{"Name": "Image"}]


def test_ecr_scan_and_immutable_tags(sandbox):
    sandbox["proto-ecom-cicd"].has_resource_properties("AWS::ECR::Repository", {
        "ImageScanningConfiguration": {"ScanOnPush": True}, "ImageTagMutability": "IMMUTABLE"})


def test_buckets_private_and_tls_only(customer):
    for templates in (customer,):
        for b in all_resources(templates, "AWS::S3::Bucket"):
            assert b["Properties"]["PublicAccessBlockConfiguration"] == {
                "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
            assert "BucketEncryption" in b["Properties"]
        for pol in all_resources(templates, "AWS::S3::BucketPolicy"):
            stmts = pol["Properties"]["PolicyDocument"]["Statement"]
            assert any(s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false" for s in stmts)


def test_removal_policy_follows_profile(sandbox, customer):
    db_s = next(iter(sandbox["proto-ecom-dev-platform"].find_resources("AWS::RDS::DBInstance").values()))
    db_c = next(iter(customer["proto-ecom-prod-platform"].find_resources("AWS::RDS::DBInstance").values()))
    assert db_s["DeletionPolicy"] == "Delete" and db_s["Properties"]["DeletionProtection"] is False
    assert db_c["DeletionPolicy"] in ("Retain", "Snapshot") and db_c["Properties"]["DeletionProtection"] is True
    assert db_c["Properties"]["MultiAZ"] is True


def test_tags_on_resources(sandbox):
    sandbox["proto-ecom-dev-mcf"].has_resource_properties("AWS::DynamoDB::GlobalTable", {
        "Replicas": Match.array_with([Match.object_like({"Tags": Match.array_with([
            {"Key": "Teardown", "Value": "after-test"}])})])})
    sandbox["proto-ecom-dev-platform"].has_resource_properties("AWS::RDS::DBInstance", {
        "Tags": Match.array_with([{"Key": "Project", "Value": "ecommerce-graviton-cicd"}])})


def test_mcf_queue_dlq_and_alarms(sandbox):
    t = sandbox["proto-ecom-dev-mcf"]
    t.has_resource_properties("AWS::SQS::Queue", {"RedrivePolicy": Match.object_like({"maxReceiveCount": 3})})
    t.has_resource_properties("AWS::Lambda::EventSourceMapping", {"FunctionResponseTypes": ["ReportBatchItemFailures"]})
    t.resource_count_is("AWS::CloudWatch::Alarm", 7)
    t.has_resource_properties("AWS::Lambda::Function", {"Environment": {"Variables": Match.object_like({"SPAPI_MODE": "mock"})}})
    secret = next(iter(t.find_resources("AWS::SecretsManager::Secret").values()))
    assert "REPLACE_ME" in json.dumps(secret["Properties"]["SecretString"])


def test_secrets_injected_not_plain_env(sandbox):
    td = next(iter(sandbox["proto-ecom-dev-platform"].find_resources(
        "AWS::ECS::TaskDefinition", {"Properties": {"Family": "proto-ecom-dev-web"}}).values()))
    c = td["Properties"]["ContainerDefinitions"][0]
    env_names = {e["Name"] for e in c["Environment"]}
    secret_names = {s["Name"] for s in c["Secrets"]}
    assert {"MAGENTO_DB_PASSWORD", "MAGENTO_CRYPT_KEY", "MAGENTO_OPENSEARCH_PASSWORD", "MAGENTO_BASE_URL"} <= secret_names
    assert not any("PASSWORD" in n or "KEY" in n for n in env_names)
    assert "MAGENTO_ADMIN_PASSWORD" not in secret_names  # only the one-off ops task gets admin creds
    assert "MAGENTO_DB_MASTER_PASSWORD" not in secret_names  # web tasks use the app user only


def test_scripts_do_not_hard_code_the_prefix():
    for path in (ROOT / "scripts").rglob("*"):
        if path.is_file() and path.suffix in (".sh", ".py"):
            assert "proto-ecom" not in path.read_text(), path


def test_snapshot_name_and_iam_grant_share_the_configured_prefix():
    _, templates = synth("customer", image_tag="abc123")
    t = json.dumps(templates["proto-ecom-uat-platform"].to_json())
    # deploy script names snapshots "<snapshotPrefix>-<timestamp>"; IAM allows "<project>-<env>-predeploy-*"
    assert '\\"snapshotPrefix\\":\\"proto-ecom-uat-predeploy\\"' in t
    assert ":snapshot:proto-ecom-uat-predeploy-*" in t
    assert '\\"namePrefix\\":\\"proto-ecom-uat\\"' in t


def test_unhealthy_target_alarm_covers_both_blue_green_groups(customer):
    def unhealthy(env):
        alarms = customer[f"proto-ecom-{env}-platform"].find_resources("AWS::CloudWatch::Alarm")
        return [a for a in alarms.values() if a["Properties"].get("MetricName") == "UnHealthyHostCount"]
    assert len(unhealthy("prod")) == 2 and len(unhealthy("dev")) == 1


def test_storage_and_bake_time_come_from_the_profile(customer):
    prod = customer["proto-ecom-prod-platform"]
    prod.has_resource_properties("AWS::RDS::DBInstance", {"AllocatedStorage": "20"})
    prod.has_resource_properties("AWS::OpenSearchService::Domain", {"EBSOptions": Match.object_like({"VolumeSize": 20})})
    prod.has_resource_properties("AWS::ECS::Service", {"DeploymentConfiguration": Match.object_like(
        {"BakeTimeInMinutes": 10})})


def test_https_environments_listen_on_443_with_the_configured_certificate(sandbox, customer):
    for env in ("uat", "prod"):
        t = customer[f"proto-ecom-{env}-platform"]
        t.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {
            "Port": 443, "Protocol": "HTTPS", "Certificates": Match.any_value(),
            "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09"})
        t.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {
            "Port": 80, "DefaultActions": [Match.object_like({"Type": "redirect"})]})
        rule = next(iter(t.find_resources("AWS::ElasticLoadBalancingV2::ListenerRule").values()))
        assert "AlbHttps" in json.dumps(rule["Properties"]["ListenerArn"])
        meta = json.dumps(t.find_resources("AWS::SSM::Parameter"))
        assert '\\"scheme\\":\\"https\\"' in meta and f"{env}.shop.example.com" in meta
    for templates, env in ((customer, "dev"), (sandbox, "dev")):
        listeners = templates[f"proto-ecom-{env}-platform"].find_resources("AWS::ElasticLoadBalancingV2::Listener")
        assert [ln["Properties"]["Port"] for ln in listeners.values()] == [80]


def test_https_without_certificate_is_rejected(tmp_path):
    from ecom_infra.config import load
    raw = json.loads((ROOT / "config" / "sandbox.json").read_text())
    raw["environments"]["dev"].update(base_url_scheme="https", site_hostname="shop.example.com")
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="certificate_arn"):
        load(p)
    raw["environments"]["dev"]["certificate_arn"] = "arn:aws:acm:us-east-1:<ACCOUNT_ID>:certificate/example"
    p.write_text(json.dumps(raw))
    assert load(p).environments["dev"].https


def test_smoke_test_follows_the_scheme():
    smoke = (ROOT / "scripts" / "deploy" / "smoke_test.sh").read_text()
    assert '--connect-to "$HOST:443:$ALB:443"' in smoke and 'BASE="https://$HOST"' in smoke
    assert "-k" not in smoke.split() and "--insecure" not in smoke


def test_mcf_intake_timeout_covers_the_batch_and_queue_visibility(sandbox, customer):
    from ecom_infra.config import load
    for profile, templates in (("sandbox", sandbox), ("customer", customer)):
        mcf = load(ROOT / "config" / f"{profile}.json", synth_check=True).environments["dev"].mcf
        t = templates["proto-ecom-dev-mcf"]
        fn = next(f for f in t.find_resources("AWS::Lambda::Function").values()
                  if f["Properties"].get("Handler") == "mcf_bridge.handlers.order_intake")["Properties"]
        worst = mcf.intake_batch_size * (4 * mcf.spapi_http_timeout_seconds)
        assert fn["Timeout"] > worst
        esm = next(iter(t.find_resources("AWS::Lambda::EventSourceMapping").values()))["Properties"]
        assert esm["BatchSize"] == mcf.intake_batch_size
        queue = next(q for q in t.find_resources("AWS::SQS::Queue").values() if "RedrivePolicy" in q["Properties"])
        assert queue["Properties"]["VisibilityTimeout"] >= 6 * fn["Timeout"]
        env = fn["Environment"]["Variables"]
        assert json.loads(env["MARKETPLACES"])["US"] == {"id": "ATVPDKIKX0DER", "region": "na"}
        assert int(env["STUCK_RECEIVED_MINUTES"]) * 60 > queue["Properties"]["VisibilityTimeout"] * mcf.max_attempts


def test_mcf_outbox_index_and_cancel_route(sandbox):
    t = sandbox["proto-ecom-dev-mcf"]
    table = next(iter(t.find_resources("AWS::DynamoDB::GlobalTable").values()))["Properties"]
    assert {g["IndexName"] for g in table["GlobalSecondaryIndexes"]} == {"gsi1", "gsi2"}
    t.has_resource_properties("AWS::Events::Rule", {"EventPattern": {
        "source": ["storefront.orders"], "detail-type": ["Order Paid", "Order Cancelled"]}})
