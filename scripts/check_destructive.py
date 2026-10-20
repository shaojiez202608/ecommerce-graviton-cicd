"""Destructive-change guard for `cdk diff` output (used by scripts/preflight.sh).

Refuses (exit 3) when the diff deletes or replaces any resource, unless ALLOW_DESTRUCTIVE=1.
Replacing or deleting a data store (RDS, OpenSearch, ElastiCache, EFS, DynamoDB, S3, Secrets
Manager) in a protected environment (default: uat, prod) additionally needs
ALLOW_DATA_REPLACEMENT=1, because the data is lost.

    npx -y aws-cdk@2 diff --all ... > diff.txt; python3 scripts/check_destructive.py diff.txt [--protected uat,prod]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

DATA_TYPES = ("AWS::RDS::DBInstance", "AWS::RDS::DBCluster", "AWS::OpenSearchService::Domain",
              "AWS::ElastiCache::ReplicationGroup", "AWS::EFS::FileSystem", "AWS::DynamoDB::GlobalTable",
              "AWS::DynamoDB::Table", "AWS::S3::Bucket", "AWS::SecretsManager::Secret")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
STACK = re.compile(r"^Stack (\S+)")
RESOURCE = re.compile(r"^\[(?P<op>[-~+])\]\s+(?P<type>AWS::\S+|Custom::\S+)\s+(?P<path>\S+)\s+(?P<lid>\S+)(?P<rest>.*)$")


@dataclass(frozen=True)
class Finding:
    stack: str
    rtype: str
    path: str
    action: str  # destroy | replace | may-replace

    @property
    def data(self) -> bool:
        return self.rtype in DATA_TYPES


def parse(text: str) -> list[Finding]:
    findings: list[Finding] = []
    stack, current = "", None
    for raw in text.splitlines():
        line = ANSI.sub("", raw).rstrip()
        m = STACK.match(line)
        if m:
            stack, current = m.group(1), None
            continue
        m = RESOURCE.match(line.strip())
        if m:
            current = (m.group("type"), m.group("path"))
            rest = m.group("rest").lower()
            if m.group("op") == "-":
                findings.append(Finding(stack, *current, "destroy"))
            elif "replace" in rest:
                findings.append(Finding(stack, *current, "may-replace" if "may" in rest else "replace"))
            continue
        if current and "(requires replacement)" in line:
            findings.append(Finding(stack, *current, "replace"))
        elif current and "(may cause replacement)" in line:
            findings.append(Finding(stack, *current, "may-replace"))
    return list(dict.fromkeys(findings))


def protected_stack(stack: str, protected: set[str]) -> bool:
    return any(f"-{env}-" in f"{stack}-" for env in protected)


def decide(findings: list[Finding], protected: set[str], env: dict) -> tuple[int, list[str]]:
    msgs, rc = [], 0
    data_hits = [f for f in findings if f.data and protected_stack(f.stack, protected)]
    if findings and env.get("ALLOW_DESTRUCTIVE") != "1":
        rc = 3
        msgs.append("refusing: the change deletes or replaces resources (set ALLOW_DESTRUCTIVE=1 to proceed)")
    if data_hits and env.get("ALLOW_DATA_REPLACEMENT") != "1":
        rc = 3
        msgs.append("refusing: data stores in protected environments would be replaced or deleted "
                    "(also set ALLOW_DATA_REPLACEMENT=1 after taking a backup)")
    for f in findings:
        mark = " [DATA, protected]" if f in data_hits else (" [data]" if f.data else "")
        msgs.append(f"  {f.action:<11} {f.stack} {f.rtype} {f.path}{mark}")
    return rc, msgs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("diff_file")
    ap.add_argument("--protected", default="uat,prod")
    args = ap.parse_args(argv)
    findings = parse(open(args.diff_file, encoding="utf-8", errors="replace").read())
    rc, msgs = decide(findings, {e for e in args.protected.split(",") if e}, dict(os.environ))
    print("\n".join(msgs) if msgs else "no deletions or replacements in the diff")
    return rc


if __name__ == "__main__":
    sys.exit(main())
