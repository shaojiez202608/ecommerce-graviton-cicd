"""ECR scan gate used by buildspec/build.yml.

Waits for the image scan to finish and fails the build when findings exist at any blocked
severity. Works with both registry scan types:
- BASIC: status COMPLETE, counts in imageScanFindings.findingSeverityCounts
- ENHANCED (Amazon Inspector): status ACTIVE, findings in imageScanFindings.enhancedFindings
Fails closed on FAILED / UNSUPPORTED_IMAGE / timeout.
Usage: python3 scan_gate.py <repo> <tag> <CRITICAL,HIGH> [timeout_seconds] [fail|warn]
"warn" only applies when no scan result is available in time (sandbox profile); findings always fail.
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
import time

DONE = {"COMPLETE", "ACTIVE"}
BAD = {"FAILED", "UNSUPPORTED_IMAGE", "FINDINGS_UNAVAILABLE", "SCAN_ELIGIBILITY_EXPIRED"}


def describe(repo: str, tag: str) -> dict:
    out = subprocess.run(
        ["aws", "ecr", "describe-image-scan-findings", "--repository-name", repo,
         "--image-id", f"imageTag={tag}", "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        if "ScanNotFoundException" in out.stderr:
            return {"imageScanStatus": {"status": "PENDING"}}
        raise SystemExit(f"describe-image-scan-findings failed: {out.stderr.strip()[:500]}")
    return json.loads(out.stdout)


def severity_counts(resp: dict) -> dict[str, int]:
    findings = resp.get("imageScanFindings") or {}
    counts = dict(findings.get("findingSeverityCounts") or {})
    if not counts and findings.get("enhancedFindings"):
        counts = dict(collections.Counter(f.get("severity", "UNTRIAGED") for f in findings["enhancedFindings"]))
    return counts


def main(argv: list[str]) -> int:
    repo, tag, blocked = argv[1], argv[2], [s for s in argv[3].split(",") if s]
    timeout = int(argv[4]) if len(argv) > 4 else 900
    on_unavailable = argv[5] if len(argv) > 5 else "fail"
    deadline = time.time() + timeout
    while True:
        resp = describe(repo, tag)
        status = (resp.get("imageScanStatus") or {}).get("status", "PENDING")
        if status in DONE:
            break
        if status in BAD:
            print(f"scan gate failed: scan status {status}", file=sys.stderr)
            return 1
        if time.time() > deadline:
            msg = f"scan not finished after {timeout}s (status {status})"
            json.dump({"status": status, "result": "unavailable"}, open("scan-counts.json", "w"))
            if on_unavailable == "warn":
                print(f"WARNING scan gate: {msg}; continuing because scan_unavailable_action=warn")
                return 0
            print(f"scan gate failed: {msg}", file=sys.stderr)
            return 1
        print(f"waiting for scan (status {status})")
        time.sleep(15)
    counts = severity_counts(resp)
    json.dump(counts, open("scan-counts.json", "w"))
    hits = {s: counts.get(s, 0) for s in blocked if counts.get(s, 0) > 0}
    if hits:
        print(f"scan gate failed: findings at blocked severities {hits}; all counts {counts}", file=sys.stderr)
        return 1
    print(f"scan gate passed ({status}); counts {counts}; blocked {blocked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
