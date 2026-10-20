"""ECR scan gate used by buildspec/build.yml (after push) and by scripts/deploy/ecs_deploy.sh
(before every promotion).

Waits for the image scan to finish and fails when findings exist at any blocked severity.
Works with both registry scan types:
- BASIC: status COMPLETE, counts in imageScanFindings.findingSeverityCounts
- ENHANCED (Amazon Inspector): status ACTIVE, findings in imageScanFindings.enhancedFindings
Fails closed on FAILED / UNSUPPORTED_IMAGE / timeout.
Usage: python3 scan_gate.py <REPO> <TAG> <CRITICAL,HIGH> [timeout_seconds] [fail|warn] [--quarantine|--verify-only]
"warn" only applies when no scan result is available in time (sandbox profile); findings always fail.
--quarantine (build): a blocked image keeps its digest under "quarantine-<TAG>" and loses <TAG>,
  so no environment can be promoted to it.
--verify-only (deploy): re-check the stored result; no quarantine, no scan-counts.json.
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
import time

DONE = {"COMPLETE", "ACTIVE"}
SETTLE_SECONDS = 60
BAD = {"FAILED", "UNSUPPORTED_IMAGE", "FINDINGS_UNAVAILABLE", "SCAN_ELIGIBILITY_EXPIRED"}


def describe(repo: str, tag: str) -> dict:
    """All pages of describe-image-scan-findings merged into one response.

    Findings are paginated (and for enhanced scanning findingSeverityCounts only covers the
    returned page), so every page is read before counting.
    """
    merged: dict = {}
    findings: list = []
    enhanced: list = []
    token = None
    while True:
        cmd = ["aws", "ecr", "describe-image-scan-findings", "--repository-name", repo,
               "--image-id", f"imageTag={tag}", "--no-paginate", "--max-results", "1000", "--output", "json"]
        if token:
            cmd += ["--next-token", token]
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if out.returncode != 0:
            if "ScanNotFoundException" in out.stderr:
                return {"imageScanStatus": {"status": "PENDING"}}
            raise SystemExit(f"describe-image-scan-findings failed: {out.stderr.strip()[:500]}")
        page = json.loads(out.stdout)
        merged = merged or page
        f = page.get("imageScanFindings") or {}
        findings += f.get("findings") or []
        enhanced += f.get("enhancedFindings") or []
        token = page.get("nextToken")
        if not token:
            break
    if findings or enhanced:
        merged.setdefault("imageScanFindings", {})
        merged["imageScanFindings"]["findings"] = findings
        merged["imageScanFindings"]["enhancedFindings"] = enhanced
    return merged


def severity_counts(resp: dict) -> dict[str, int]:
    findings = resp.get("imageScanFindings") or {}
    items = (findings.get("findings") or []) + (findings.get("enhancedFindings") or [])
    if items:  # count every finding across all pages
        return dict(collections.Counter(f.get("severity", "UNTRIAGED") for f in items))
    return dict(findings.get("findingSeverityCounts") or {})


def aws(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["aws", "ecr", *args], capture_output=True, text=True, check=False)


def quarantine(repo: str, tag: str) -> None:
    """Re-tag the blocked image as quarantine-<TAG> (kept for analysis) and remove <TAG>."""
    got = aws("batch-get-image", "--repository-name", repo, "--image-ids", f"imageTag={tag}",
              "--query", "images[0].imageManifest", "--output", "text")
    if got.returncode == 0 and got.stdout.strip() not in ("", "None"):
        put = aws("put-image", "--repository-name", repo, "--image-tag", f"quarantine-{tag}",
                  "--image-manifest", got.stdout.strip())
        if put.returncode != 0 and "ImageAlreadyExists" not in put.stderr and "TagAlreadyExists" not in put.stderr:
            print(f"WARNING could not add quarantine tag: {put.stderr.strip()[:300]}", file=sys.stderr)
    rm = aws("batch-delete-image", "--repository-name", repo, "--image-ids", f"imageTag={tag}")
    if rm.returncode != 0:
        print(f"WARNING could not remove tag {tag}: {rm.stderr.strip()[:300]}", file=sys.stderr)
    print(f"image {repo}:{tag} quarantined (tag removed; digest kept as quarantine-{tag})", file=sys.stderr)


def main(argv: list[str]) -> int:
    flags = {a for a in argv[1:] if a.startswith("--")}
    argv = [a for a in argv if not a.startswith("--")]
    rc = gate(argv, verify_only="--verify-only" in flags)
    if rc != 0 and "--quarantine" in flags:
        quarantine(argv[1], argv[2])
    return rc


def gate(argv: list[str], verify_only: bool = False) -> int:
    repo, tag, blocked = argv[1], argv[2], [s for s in argv[3].split(",") if s]
    timeout = int(argv[4]) if len(argv) > 4 else 900
    on_unavailable = argv[5] if len(argv) > 5 else "fail"
    deadline = time.time() + timeout
    write = (lambda data: None) if verify_only else (lambda data: json.dump(data, open("scan-counts.json", "w")))
    previous: dict | None = None
    while True:
        resp = describe(repo, tag)
        status = (resp.get("imageScanStatus") or {}).get("status", "PENDING")
        if status == "COMPLETE":
            break
        if status == "ACTIVE":
            # Enhanced scanning reports ACTIVE as soon as monitoring starts, while findings are
            # still being added. Require the same counts on two polls SETTLE_SECONDS apart (or
            # any blocked finding) before deciding; the deploy re-check covers later findings.
            counts = severity_counts(resp)
            if any(counts.get(s, 0) for s in blocked) or (counts and counts == previous):
                break
            previous = counts
            if time.time() + SETTLE_SECONDS <= deadline:
                print(f"scan ACTIVE, waiting for findings to settle (counts {counts})")
                time.sleep(SETTLE_SECONDS)
                continue
            break
        if status in BAD:
            print(f"scan gate failed: scan status {status}", file=sys.stderr)
            return 1
        if time.time() > deadline:
            msg = f"scan not finished after {timeout}s (status {status})"
            write({"status": status, "result": "unavailable"})
            if on_unavailable == "warn":
                print(f"WARNING scan gate: {msg}; continuing because scan_unavailable_action=warn")
                return 0
            print(f"scan gate failed: {msg}", file=sys.stderr)
            return 1
        print(f"waiting for scan (status {status})")
        time.sleep(15)
    counts = severity_counts(resp)
    write(counts)
    hits = {s: counts.get(s, 0) for s in blocked if counts.get(s, 0) > 0}
    if hits:
        print(f"scan gate failed: findings at blocked severities {hits}; all counts {counts}", file=sys.stderr)
        return 1
    print(f"scan gate passed ({status}); counts {counts}; blocked {blocked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
