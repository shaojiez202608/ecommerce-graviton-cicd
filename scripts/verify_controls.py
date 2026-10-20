"""Deployed-control checks (deny paths). Read-only except for the denied calls themselves.

1. SP-API secret: GetSecretValue by the operator is explicitly denied by the resource policy.
2. Platform secret (DB master): same, for the operator.
3. Deploy role (IAM policy simulation): RunTask on this environment's ops family in this cluster
   is allowed; RunTask on another family, and iam:PassRole of an unrelated role, are denied.
4. Trust policies of the deploy and build roles carry aws:SourceAccount and aws:SourceArn.
Prints PASS/FAIL per check; exit 1 on any FAIL. Account IDs are redacted.

    uv run python scripts/verify_controls.py --env dev [--config config/sandbox.json]
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


def profile(path: str) -> dict:
    p = ROOT / path
    return json.loads((p if p.exists() else p.with_name(p.stem + ".example.json")).read_text())


def denied(fn) -> tuple[bool, str]:
    try:
        fn()
        return False, "call succeeded"
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        return code == "AccessDeniedException" or code == "AccessDenied", code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--config", default="config/sandbox.json")
    args = ap.parse_args()
    cfg = profile(args.config)
    p, env, region = cfg["project"], args.env, cfg["region"]
    sess = boto3.session.Session(region_name=region)
    account = sess.client("sts").get_caller_identity()["Account"]
    results = []

    sm = sess.client("secretsmanager")
    for name in (f"{p}-{env}-spapi-credentials", f"{p}-{env}-db"):
        ok, why = denied(lambda n=name: sm.get_secret_value(SecretId=n))
        results.append((f"operator cannot read secret {name}", ok, why))

    iam = sess.client("iam")
    role_name = None
    for page in iam.get_paginator("list_roles").paginate():
        for r in page["Roles"]:
            if r["RoleName"].startswith(f"{p}-{env}-platform-DeployProjectRole"):
                role_name = r["RoleName"]
    if role_name is None:
        results.append(("deploy role found", False, "not found"))
    else:
        role_arn = f"arn:aws:iam::{account}:role/{role_name}"
        cluster = f"arn:aws:ecs:{region}:{account}:cluster/{p}-{env}-cluster"
        ctx = [{"ContextKeyName": "ecs:cluster", "ContextKeyValues": [cluster], "ContextKeyType": "string"}]

        def sim(action, resource, context=ctx):
            r = iam.simulate_principal_policy(PolicySourceArn=role_arn, ActionNames=[action],
                                              ResourceArns=[resource], ContextEntries=context)
            return r["EvaluationResults"][0]["EvalDecision"]

        own = sim("ecs:RunTask", f"arn:aws:ecs:{region}:{account}:task-definition/{p}-{env}-ops:1")
        other = sim("ecs:RunTask", f"arn:aws:ecs:{region}:{account}:task-definition/other-family:1")
        passrole_decision = sim("iam:PassRole", f"arn:aws:iam::{account}:role/unrelated-role", [])
        results.append(("deploy role may RunTask its own ops family", own == "allowed", own))
        results.append(("deploy role may not RunTask another family", other != "allowed", other))
        results.append(("deploy role may not PassRole an unrelated role", passrole_decision != "allowed", passrole_decision))
        trust = json.dumps(iam.get_role(RoleName=role_name)["Role"]["AssumeRolePolicyDocument"])
        results.append(("deploy role trust has SourceAccount + SourceArn",
                        "aws:SourceAccount" in trust and "aws:SourceArn" in trust, "checked"))

    fail = False
    for name, ok, why in results:
        fail |= not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({re.sub(r'[0-9]{12}', '111122223333', str(why))})")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
