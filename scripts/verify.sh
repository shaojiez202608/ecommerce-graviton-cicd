#!/usr/bin/env bash
# End-to-end verification of a deployed sandbox (one command):
#   1. last pipeline execution of each pipeline succeeded (build -> deploy -> smoke ran in the VPC)
#   2. deny paths: secret resource policies, deploy-role scope, trust conditions (verify_controls.py)
#   3. protected promotion is refused without --break-glass (customer template, no AWS call)
#   4. MCF bridge end to end with the mock SP-API, including the outbox drill (mcf_e2e.py)
# Usage: [ECOM_CONFIG=config/<profile>.json] [ENV_NAME=dev] scripts/verify.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/lib/prefix.sh"
ENV_NAME="${ENV_NAME:-dev}"
export AWS_REGION="${AWS_REGION:-$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["region"])' "$_ECOM_CFG")}"
rc=0
echo "[verify] 1. pipelines"
for p in $(aws codepipeline list-pipelines --query "pipelines[?starts_with(name,'$PROJECT-pipeline-')].name" --output text); do
  s=$(aws codepipeline list-pipeline-executions --pipeline-name "$p" --max-items 1 --query 'pipelineExecutionSummaries[0].status' --output text | head -1)
  echo "  $p last execution: $s"; [ "$s" = "Succeeded" ] || rc=1
done
echo "[verify] 2. deny paths"
uv run --project "$ROOT" python "$ROOT/scripts/verify_controls.py" --env "$ENV_NAME" --config "$ECOM_CONFIG" || rc=1
echo "[verify] 3. protected promotion"
if ECOM_CONFIG=config/customer.example.json bash "$ROOT/scripts/promote_image.sh" prod none >/dev/null 2>&1; then
  echo "  FAIL promote to prod was not refused"; rc=1
else echo "  PASS promote to prod refused without --break-glass"; fi
echo "[verify] 4. MCF bridge (mock SP-API)"
uv run --project "$ROOT" python "$ROOT/scripts/mcf_e2e.py" --env "$ENV_NAME" --config "$ECOM_CONFIG" --outbox-drill || rc=1
[ "$rc" = "0" ] && echo "[verify] PASS" || echo "[verify] FAIL"
exit "$rc"
