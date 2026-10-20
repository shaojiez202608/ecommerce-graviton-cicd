#!/usr/bin/env bash
# Read-only pre-deploy checks. Changes nothing in the account.
#   1. credentials: account (last 4 digits only) and region; you confirm they are the intended ones
#   2. toolchain: python3, uv, node/npx, AWS CLI, git, zip (Docker is reported, not required)
#   3. region/service availability for the configured instance classes and engine versions; quotas
#   4. `cdk diff`: refuses deletions/replacements unless ALLOW_DESTRUCTIVE=1 (data stores in
#      protected environments also need ALLOW_DATA_REPLACEMENT=1), see scripts/check_destructive.py
# Usage: [ECOM_CONFIG=config/<profile>.json] [CONFIRM=<last-4-digits>] scripts/preflight.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/lib/prefix.sh"
CFG="$_ECOM_CFG"
fail=0
ok() { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; fail=1; }
note() { printf '  note  %s\n' "$*"; }

echo "[preflight] profile $ECOM_CONFIG"
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from ecom_infra.config import load; load(sys.argv[2])' \
  "$ROOT/infra" "$CFG" >/dev/null 2>/tmp/preflight-config.err && ok "configuration valid" \
  || { bad "configuration invalid: $(tail -1 /tmp/preflight-config.err)"; }
REGION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["region"])' "$CFG")
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

echo "[preflight] 1. credentials"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || { bad "no valid AWS credentials"; exit 1; }
LAST4="${ACCOUNT: -4}"
echo "  target account ********$LAST4, region $REGION"
if [ "${CONFIRM:-}" != "$LAST4" ]; then
  if [ -t 0 ]; then
    read -r -p "  type the last 4 digits of the account to confirm: " answer
    [ "$answer" = "$LAST4" ] || { bad "account not confirmed; stop if this is not your intended account"; exit 1; }
  else
    bad "set CONFIRM=<last 4 digits of the target account> (non-interactive run)"; exit 1
  fi
fi
ok "account and region confirmed"

echo "[preflight] 2. toolchain"
for t in python3 uv node npx aws git zip; do command -v "$t" >/dev/null && ok "$t" || bad "$t missing"; done
aws --version 2>&1 | grep -q 'aws-cli/2' && ok "AWS CLI v2" || bad "AWS CLI v2 required"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then note "docker running (not required)"; else note "docker not running (not required: images build on CodeBuild)"; fi

echo "[preflight] 3. region availability and quotas"
python3 - "$CFG" <<'PY' > /tmp/preflight-sizing.txt
import json, sys
c = json.load(open(sys.argv[1]))
for name, e in c["environments"].items():
    db = e["db_instance_class"]; db = db if db.startswith("db.") else "db." + db
    print(name, db, e["db_engine_version"], e["cache_node_type"], e["search_instance_type"], e["search_engine_version"])
PY
while read -r env db dbv cache search searchv; do
  n=$(aws rds describe-orderable-db-instance-options --engine mysql --engine-version "$dbv" --db-instance-class "$db" \
      --query 'length(OrderableDBInstanceOptions)' --output text 2>/dev/null)
  [ "${n:-0}" != "0" ] && ok "$env: RDS MySQL $dbv on $db" || bad "$env: RDS MySQL $dbv on $db not orderable in $REGION"
  aws opensearch list-instance-type-details --engine-version "$searchv" --query 'InstanceTypeDetails[].InstanceType' \
      --output text 2>/dev/null | tr '\t' '\n' | grep -qx "$search" \
    && ok "$env: OpenSearch $searchv on $search" || bad "$env: $search not offered for $searchv in $REGION"
  aws elasticache describe-reserved-cache-nodes-offerings --cache-node-type "$cache" --product-description valkey \
      --query 'length(ReservedCacheNodesOfferings)' --output text 2>/dev/null | grep -qv '^0$' \
    && ok "$env: ElastiCache Valkey on $cache" || note "$env: could not confirm $cache for Valkey in $REGION (check the official page)"
done < /tmp/preflight-sizing.txt
envs=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["environments"]))' "$CFG")
vpcs=$(aws ec2 describe-vpcs --query 'length(Vpcs)' --output text)
vpc_q=$(aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE --query 'Quota.Value' --output text 2>/dev/null || echo 5)
[ "$(python3 -c "print(int($vpcs + $envs <= float('$vpc_q')))")" = "1" ] && ok "VPC quota ($vpcs used + $envs new <= ${vpc_q%.*})" \
  || bad "VPC quota: $vpcs used + $envs new > ${vpc_q%.*}"
eips=$(aws ec2 describe-addresses --query 'length(Addresses)' --output text)
eip_q=$(aws service-quotas get-service-quota --service-code ec2 --quota-code L-0263D0A3 --query 'Quota.Value' --output text 2>/dev/null || echo 5)
nats=$(python3 -c 'import json,sys; print(sum(e["nat_gateways"] for e in json.load(open(sys.argv[1]))["environments"].values()))' "$CFG")
[ "$(python3 -c "print(int($eips + $nats <= float('$eip_q')))")" = "1" ] && ok "Elastic IP quota ($eips used + $nats new <= ${eip_q%.*})" \
  || bad "Elastic IP quota: $eips used + $nats new > ${eip_q%.*}"
aws cloudformation describe-stacks --stack-name CDKToolkit --query 'Stacks[0].StackStatus' --output text >/dev/null 2>&1 \
  && ok "CDK bootstrap present (not modified)" || bad "CDKToolkit stack missing: bootstrap the account/region first"

echo "[preflight] 4. destructive-change guard (cdk diff)"
EXTRA=()
[ -n "${CDK_CONTEXT_ARGS:-}" ] && read -r -a EXTRA <<< "$CDK_CONTEXT_ARGS"
(cd "$ROOT" && npx -y aws-cdk@2 diff --all -c config="$ECOM_CONFIG" ${EXTRA[@]+"${EXTRA[@]}"} --no-color) > /tmp/preflight-diff.txt 2>&1 \
  || note "cdk diff returned non-zero (differences found or first deployment)"
grep -qE '^Stack ' /tmp/preflight-diff.txt || { bad "cdk diff produced no stack output (see /tmp/preflight-diff.txt)"; }
python3 "$ROOT/scripts/check_destructive.py" /tmp/preflight-diff.txt --protected "${PROTECTED_ENVS:-uat,prod}" || fail=1

[ "$fail" = "0" ] && echo "[preflight] PASS" || echo "[preflight] FAIL"
exit "$fail"
