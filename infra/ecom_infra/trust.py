"""Confused-deputy protection on service-role trust policies.

Adds aws:SourceAccount (StringEquals) and aws:SourceArn (ArnLike) to the AssumeRole statement of
roles whose service documents these keys for trust policies (verified 2026-10-21):
- codebuild.amazonaws.com: SourceArn = the CodeBuild project ARN
  https://docs.aws.amazon.com/codebuild/latest/userguide/cross-service-confused-deputy-prevention.html
- ecs-tasks.amazonaws.com (task roles): SourceArn = arn:aws:ecs:<REGION>:<ACCOUNT>:* (a specific
  cluster is not supported) https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html
- vpc-flow-logs.amazonaws.com: SourceArn = flow-log ARN (wildcard id)
  https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs-iam-role.html
- events.amazonaws.com (rule target roles): SourceArn = rule ARN
  https://docs.aws.amazon.com/eventbridge/latest/userguide/cross-service-confused-deputy-prevention.html
Roles whose service documents no such guidance (CodePipeline service role, ECS task execution
role, ECS infrastructure role, Lambda execution roles) are left unchanged and listed in
SECURITY-NOTES.md. ARNs are built from deterministic names, so no circular references arise.
"""

from __future__ import annotations

import json

from aws_cdk import Stack
from aws_cdk import aws_iam as iam

from .config import AppConfig


def _services(doc: dict) -> set[str]:
    out = set()
    for st in doc.get("Statement", []):
        svc = (st.get("Principal") or {}).get("Service")
        out.update([svc] if isinstance(svc, str) else (svc or []))
    return out


def source_arn_for(service: str, role: iam.CfnRole, stack: Stack, cfg: AppConfig) -> str | None:
    region, account, partition = stack.region, stack.account, stack.partition
    path = role.node.path
    if service == "codebuild.amazonaws.com":
        return f"arn:{partition}:codebuild:{region}:{account}:project/{cfg.project}-*"
    if service == "ecs-tasks.amazonaws.com" and "/TaskRole/" in path + "/":
        return f"arn:{partition}:ecs:{region}:{account}:*"
    if service == "vpc-flow-logs.amazonaws.com":
        return f"arn:{partition}:ec2:{region}:{account}:vpc-flow-log/*"
    if service == "events.amazonaws.com":
        return f"arn:{partition}:events:{region}:{account}:rule/*"
    return None


def apply_trust_conditions(stack: Stack, cfg: AppConfig) -> list[str]:
    """Returns the construct paths that received the conditions (used by tests and docs)."""
    changed = []
    for node in stack.node.find_all():
        if not isinstance(node, iam.CfnRole):
            continue
        doc = stack.resolve(node.assume_role_policy_document)
        if not isinstance(doc, dict):
            continue
        statements = doc.get("Statement", [])
        for idx, st in enumerate(statements):
            services = _services({"Statement": [st]})
            if len(services) != 1:
                continue
            arn = source_arn_for(next(iter(services)), node, stack, cfg)
            if arn is None:
                continue
            condition = dict(st.get("Condition") or {})
            condition.setdefault("StringEquals", {})["aws:SourceAccount"] = stack.account
            condition.setdefault("ArnLike", {})["aws:SourceArn"] = arn
            node.add_property_override(f"AssumeRolePolicyDocument.Statement.{idx}.Condition", condition)
            changed.append(node.node.path)
    return changed


def describe(stack: Stack) -> str:  # debugging aid
    return json.dumps([n.node.path for n in stack.node.find_all() if isinstance(n, iam.CfnRole)])
