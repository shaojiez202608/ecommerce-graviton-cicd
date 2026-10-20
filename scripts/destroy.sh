#!/usr/bin/env bash
# Tear down everything this project created in the profile's account/region.
#   - confirmation 1: type the project prefix (or CONFIRM_DESTROY=<project>)
#   - confirmation 2: deleting data (databases, buckets, pre-deploy snapshots) is confirmed
#     separately (or CONFIRM_DATA=yes); without it, data-holding leftovers are only listed
#   - every step warns and continues on failure; the residual check runs at the end
# Out-of-stack leftovers handled here: task-definition revisions registered by the deploy script,
# log groups created by services (RDS exports, Container Insights, CDK handlers), secrets in
# scheduled deletion, pre-deploy RDS snapshots, ECR quarantine tags (removed with the repository),
# and this project's objects in the shared CDK bootstrap bucket (identified by asset hash).
# Usage: [ECOM_CONFIG=config/<profile>.json] scripts/destroy.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/lib/prefix.sh"
P="$PROJECT"
REGION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["region"])' "$_ECOM_CFG")
REMOVAL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["removal_policy"])' "$_ECOM_CFG")
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
warn() { echo "  WARNING: $*" >&2; }
ask() {  # ask <prompt> <expected> <env-value>
  [ "${3:-}" = "$2" ] && return 0
  [ -t 0 ] || { echo "non-interactive: set the confirmation variable" >&2; return 1; }
  read -r -p "$1 " a; [ "$a" = "$2" ]
}

echo "[destroy] profile $ECOM_CONFIG, prefix $P, region $REGION, removal_policy $REMOVAL"
ask "type the prefix '$P' to destroy all its stacks:" "$P" "${CONFIRM_DESTROY:-}" || { echo "aborted"; exit 1; }
DATA=no
if ask "type 'yes' to also delete data (databases, buckets, pre-deploy snapshots):" yes "${CONFIRM_DATA:-}"; then DATA=yes; fi
[ "$REMOVAL" = "retain" ] && [ "$DATA" = "no" ] && echo "  retain profile: databases, buckets and secrets stay after stack deletion"

echo "[destroy] 1. cdk destroy"
(cd "$ROOT" && npx -y aws-cdk@2 destroy --all -c config="$ECOM_CONFIG" --force) || warn "cdk destroy reported errors; continuing"

echo "[destroy] 2. task definition revisions registered by the deploy script"
for arn in $(aws ecs list-task-definitions --family-prefix "$P" --status ACTIVE --query 'taskDefinitionArns' --output text); do
  aws ecs deregister-task-definition --task-definition "$arn" >/dev/null || warn "deregister ${arn##*/}"
done
inactive=$(aws ecs list-task-definitions --family-prefix "$P" --status INACTIVE --query 'taskDefinitionArns' --output text)
for arn in $inactive; do
  aws ecs delete-task-definitions --task-definitions "$arn" >/dev/null || warn "delete ${arn##*/}"
done

echo "[destroy] 3. log groups created outside the stacks"
for lg in $(aws logs describe-log-groups --query "logGroups[?contains(logGroupName,'$P')].logGroupName" --output text); do
  aws logs delete-log-group --log-group-name "$lg" && echo "  deleted $lg" || warn "log group $lg"
done

echo "[destroy] 4. secrets in scheduled deletion"
for s in $(aws secretsmanager list-secrets --include-planned-deletion --query "SecretList[?starts_with(Name,'$P')].Name" --output text); do
  aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery >/dev/null && echo "  deleted $s" || warn "secret $s"
done

echo "[destroy] 5. pre-deploy RDS snapshots"
snaps=$(aws rds describe-db-snapshots --snapshot-type manual --query "DBSnapshots[?starts_with(DBSnapshotIdentifier,'$P')].DBSnapshotIdentifier" --output text)
if [ -n "$snaps" ] && [ "$DATA" = "yes" ]; then
  for s in $snaps; do aws rds delete-db-snapshot --db-snapshot-identifier "$s" >/dev/null && echo "  deleted $s" || warn "snapshot $s"; done
elif [ -n "$snaps" ]; then echo "  kept (no data confirmation): $snaps"; fi

echo "[destroy] 6. this project's objects in the CDK bootstrap bucket"
(cd "$ROOT" && npx -y aws-cdk@2 synth -q -c config="$ECOM_CONFIG" -o cdk.out/destroy >/dev/null 2>&1) || warn "synth for asset hashes failed"
(cd "$ROOT" && CONFIRM_ASSETS=yes uv run python scripts/cleanup_bootstrap_assets.py --assembly cdk.out --prefix "$P" \
   --region "$REGION" ${ASSET_HASH_LISTS:+$(for f in $ASSET_HASH_LISTS; do printf -- '--hash-list %s ' "$f"; done)} --delete) \
  || warn "bootstrap asset cleanup incomplete"

echo "[destroy] 7. residual check"
bash "$ROOT/scripts/check_residuals.sh"
