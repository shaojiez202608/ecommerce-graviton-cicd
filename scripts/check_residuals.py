"""Residual check after `make destroy` (read-only). Exit code 1 when anything of this project is left.

Checks every category by name prefix and by the Project tag, in each region given (default: the
profile's region). The tagging index lags behind deletions, so every tagged ARN is looked up in its
own service and only counted when it still exists in a non-terminal state. Account IDs are
replaced by 111122223333 in all output.

    uv run python scripts/check_residuals.py [--config config/sandbox.json] [--region us-east-1 ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
ACCT = re.compile(r"\b\d{12}\b")


def redact(v) -> str:
    return ACCT.sub("111122223333", str(v))


def profile(path: str) -> dict:
    p = ROOT / path
    if not p.exists():
        p = p.with_name(p.stem + ".example.json")
    return json.loads(p.read_text())


class Checker:
    def __init__(self, region: str, prefix: str, tag_value: str):
        self.r, self.p, self.tag = region, prefix, tag_value
        self.s = boto3.session.Session(region_name=region)
        self.residual: list[str] = []
        self.notes: list[str] = []

    def c(self, svc):
        return self.s.client(svc)

    def report(self, label: str, items: list, residual: bool = True) -> None:
        items = [redact(i) for i in items]
        print(f"  {label:<34} {', '.join(items) if items else '<none>'}")
        if items and residual:
            self.residual.extend(f"{label}: {i}" for i in items)

    def pages(self, svc, op, key, **kw):
        out = []
        for page in self.c(svc).get_paginator(op).paginate(**kw):
            out.extend(page.get(key, []))
        return out

    def run(self) -> None:  # noqa: C901 - one flat list of categories
        p = self.p
        print(f"region {self.r}")
        stacks = [s["StackName"] + ":" + s["StackStatus"] for s in self.pages("cloudformation", "list_stacks",
                  "StackSummaries") if s["StackName"].startswith(p) and s["StackStatus"] != "DELETE_COMPLETE"]
        self.report("cloudformation stacks", stacks)
        s3 = self.c("s3")
        self.report("s3 buckets (incl. versions)", [b["Name"] for b in s3.list_buckets()["Buckets"]
                                                     if b["Name"].startswith(p)])
        self.report("log groups", [g["logGroupName"] for g in self.pages("logs", "describe_log_groups", "logGroups")
                                   if p in g["logGroupName"]])
        self.report("ecr repositories", [r["repositoryName"] for r in self.pages("ecr", "describe_repositories",
                    "repositories") if r["repositoryName"].startswith(p)])
        self.report("lambda functions", [f["FunctionName"] for f in self.pages("lambda", "list_functions", "Functions")
                                         if f["FunctionName"].startswith(p)])
        iam = self.c("iam")
        self.report("iam roles", [r["RoleName"] for r in self.pages("iam", "list_roles", "Roles")
                                  if r["RoleName"].startswith(p)])
        self.report("iam policies (local)", [x["PolicyName"] for x in self.pages("iam", "list_policies", "Policies",
                    Scope="Local") if x["PolicyName"].startswith(p)])
        del iam
        rds = self.c("rds")
        self.report("rds instances", [d["DBInstanceIdentifier"] for d in self.pages("rds", "describe_db_instances",
                    "DBInstances") if d["DBInstanceIdentifier"].startswith(p)])
        snaps = self.pages("rds", "describe_db_snapshots", "DBSnapshots", SnapshotType="manual")
        self.report("rds manual snapshots (incl. pre-deploy)", [s["DBSnapshotIdentifier"] for s in snaps
                    if s["DBSnapshotIdentifier"].startswith(p)])
        auto = self.pages("rds", "describe_db_snapshots", "DBSnapshots", SnapshotType="automated")
        self.report("rds automated snapshots", [s["DBSnapshotIdentifier"] for s in auto
                    if s["DBInstanceIdentifier"].startswith(p)])
        self.report("rds parameter/subnet groups",
                    [g["DBParameterGroupName"] for g in self.pages("rds", "describe_db_parameter_groups",
                     "DBParameterGroups") if g["DBParameterGroupName"].startswith(p)]
                    + [g["DBSubnetGroupName"] for g in self.pages("rds", "describe_db_subnet_groups", "DBSubnetGroups")
                       if g["DBSubnetGroupName"].startswith(p)])
        del rds
        self.report("elasticache groups/snapshots/subnets",
                    [g["ReplicationGroupId"] for g in self.pages("elasticache", "describe_replication_groups",
                     "ReplicationGroups") if g["ReplicationGroupId"].startswith(p)]
                    + [s["SnapshotName"] for s in self.pages("elasticache", "describe_snapshots", "Snapshots")
                       if s["SnapshotName"].startswith(p)]
                    + [g["CacheSubnetGroupName"] for g in self.pages("elasticache", "describe_cache_subnet_groups",
                       "CacheSubnetGroups") if g["CacheSubnetGroupName"].startswith(p)])
        self.report("opensearch domains", [d["DomainName"] for d in self.c("opensearch").list_domain_names()["DomainNames"]
                                           if d["DomainName"].startswith(p)])
        self.report("efs file systems", [f["FileSystemId"] for f in self.pages("efs", "describe_file_systems",
                    "FileSystems") if (f.get("Name") or "").startswith(p)])
        ecs = self.c("ecs")
        clusters = [a for a in self.pages("ecs", "list_clusters", "clusterArns") if f"/{p}" in a]
        active = [c["clusterName"] for c in (ecs.describe_clusters(clusters=clusters)["clusters"] if clusters else [])
                  if c["status"] != "INACTIVE"]
        self.report("ecs clusters (not INACTIVE)", active)
        # list_task_definitions' familyPrefix is a full family name; enumerate families first.
        families = self.pages("ecs", "list_task_definition_families", "families", familyPrefix=p, status="ALL")
        tds, inactive = [], []
        for fam in families:
            tds += self.pages("ecs", "list_task_definitions", "taskDefinitionArns", familyPrefix=fam, status="ACTIVE")
            inactive += self.pages("ecs", "list_task_definitions", "taskDefinitionArns", familyPrefix=fam,
                                   status="INACTIVE")
        self.report("ecs task definitions (ACTIVE)", [a.rsplit("/", 1)[-1] for a in tds])
        self.report("ecs task definitions (INACTIVE)", [a.rsplit("/", 1)[-1] for a in inactive])
        ec2 = self.c("ec2")
        tagf = [{"Name": "tag:Project", "Values": [self.tag]}]
        self.report("vpcs", [v["VpcId"] for v in ec2.describe_vpcs(Filters=tagf)["Vpcs"]])
        self.report("nat gateways (not deleted)", [n["NatGatewayId"] for n in ec2.describe_nat_gateways(
            Filters=[{"Name": "tag:Project", "Values": [self.tag]}])["NatGateways"] if n["State"] != "deleted"])
        self.report("elastic ips", [a["AllocationId"] for a in ec2.describe_addresses(Filters=tagf)["Addresses"]])
        self.report("load balancers", [lb["LoadBalancerName"] for lb in self.pages("elbv2", "describe_load_balancers",
                    "LoadBalancers") if lb["LoadBalancerName"].startswith(p)])
        self.report("secrets (incl. scheduled deletion)", [s["Name"] for s in self.pages("secretsmanager",
                    "list_secrets", "SecretList", IncludePlannedDeletion=True) if s["Name"].startswith(p)])
        self.report("ssm parameters", [x["Name"] for x in self.pages("ssm", "describe_parameters", "Parameters",
                    ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": [f"/{p}"]}])])
        self.report("sqs queues", self.c("sqs").list_queues(QueueNamePrefix=p).get("QueueUrls", []))
        self.report("sns topics", [t["TopicArn"] for t in self.pages("sns", "list_topics", "Topics")
                                   if f":{p}" in t["TopicArn"]])
        self.report("event buses", [b["Name"] for b in self.c("events").list_event_buses(NamePrefix=p)["EventBuses"]])
        self.report("dynamodb tables", [t for t in self.pages("dynamodb", "list_tables", "TableNames") if t.startswith(p)])
        self.report("codebuild projects", [x for x in self.pages("codebuild", "list_projects", "projects") if x.startswith(p)])
        self.report("codepipelines", [x["name"] for x in self.pages("codepipeline", "list_pipelines", "pipelines")
                                      if x["name"].startswith(p)])
        self.report("cloudwatch alarms", [a["AlarmName"] for a in self.pages("cloudwatch", "describe_alarms",
                    "MetricAlarms") if a["AlarmName"].startswith(p)])
        self.report("api gateways", [a["name"] for a in self.c("apigateway").get_rest_apis().get("items", [])
                                     if a["name"].startswith(p)]
                    + [a["Name"] for a in self.c("apigatewayv2").get_apis().get("Items", []) if a["Name"].startswith(p)])
        self.kms()
        self.tagged()

    def kms(self) -> None:
        kms = self.c("kms")
        found = []
        for k in self.pages("kms", "list_keys", "Keys"):
            try:
                tags = kms.list_resource_tags(KeyId=k["KeyId"]).get("Tags", [])
            except ClientError:
                continue
            if any(t["TagKey"] == "Project" and t["TagValue"] == self.tag for t in tags):
                state = kms.describe_key(KeyId=k["KeyId"])["KeyMetadata"]["KeyState"]
                found.append(f"{k['KeyId']}:{state}")
        self.report("kms keys (tagged, any state)", found)

    def tagged(self) -> None:
        """Tag index lags; confirm each ARN in its own service before counting it."""
        arns = self.pages("resourcegroupstaggingapi", "get_resources", "ResourceTagMappingList",
                          TagFilters=[{"Key": "Project", "Values": [self.tag]}])
        live, gone = [], []
        for m in arns:
            arn = m["ResourceARN"]
            (live if self.exists(arn) else gone).append(arn)
        self.report("tagged resources (confirmed live)", live)
        if gone:
            self.report("tagged ARNs already deleted (index lag)", gone, residual=False)

    def exists(self, arn: str) -> bool:  # noqa: C901
        svc, res = arn.split(":")[2], arn.split(":", 5)[5]
        try:
            if svc == "ecs" and res.startswith("cluster/"):
                cl = self.c("ecs").describe_clusters(clusters=[arn])["clusters"]
                return bool(cl) and cl[0]["status"] != "INACTIVE"
            if svc == "ecs" and res.startswith("service/"):
                parts = res.split("/")
                sv = self.c("ecs").describe_services(cluster=parts[1], services=[arn])["services"]
                return bool(sv) and sv[0]["status"] != "INACTIVE"
            if svc == "ecs" and res.startswith("task-definition/"):
                td = self.c("ecs").describe_task_definition(taskDefinition=arn)["taskDefinition"]
                return td["status"] == "ACTIVE"
            if svc == "ec2" and res.startswith("natgateway/"):
                n = self.c("ec2").describe_nat_gateways(NatGatewayIds=[res.split("/")[1]])["NatGateways"]
                return bool(n) and n[0]["State"] != "deleted"
            if svc == "ec2" and res.startswith("vpc-flow-log/"):
                f = self.c("ec2").describe_flow_logs(FlowLogIds=[res.split("/")[1]])["FlowLogs"]
                return bool(f)
            if svc == "ec2":
                kind, rid = res.split("/", 1)
                if kind == "vpc":
                    return bool(self.c("ec2").describe_vpcs(VpcIds=[rid])["Vpcs"])
                if kind == "subnet":
                    return bool(self.c("ec2").describe_subnets(SubnetIds=[rid])["Subnets"])
                if kind == "security-group":
                    return bool(self.c("ec2").describe_security_groups(GroupIds=[rid])["SecurityGroups"])
                if kind == "elastic-ip":
                    return bool(self.c("ec2").describe_addresses(AllocationIds=[rid])["Addresses"])
            if svc == "logs":
                name = res.split(":", 1)[1].removesuffix(":*")
                return any(g["logGroupName"] == name for g in self.c("logs").describe_log_groups(
                    logGroupNamePrefix=name)["logGroups"])
            if svc == "rds" and res.startswith("snapshot:"):
                self.c("rds").describe_db_snapshots(DBSnapshotIdentifier=res.split(":", 1)[1])
                return True
            if svc == "secretsmanager":
                self.c("secretsmanager").describe_secret(SecretId=arn)
                return True
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if "NotFound" in code or code in ("ClientException", "InvalidParameterException",
                                              "InvalidVpcID.NotFound", "InvalidGroup.NotFound",
                                              "InvalidSubnetID.NotFound", "InvalidAllocationID.NotFound",
                                              "ResourceNotFoundException", "DBSnapshotNotFound"):
                return False
            raise
        return True  # unknown type: count it, a human confirms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/sandbox.json")
    ap.add_argument("--region", action="append", default=None)
    ap.add_argument("--prefix", default=None)
    args = ap.parse_args()
    cfg = profile(args.config)
    prefix = args.prefix or cfg["project"]
    residual = []
    for region in args.region or [cfg["region"]]:
        ch = Checker(region, prefix, cfg["tags"]["Project"])
        ch.run()
        residual += [f"{region} {r}" for r in ch.residual]
    print("conclusion:", "已清理，无残留" if not residual else "仍有残留：" + "; ".join(residual))
    return 1 if residual else 0


if __name__ == "__main__":
    sys.exit(main())
