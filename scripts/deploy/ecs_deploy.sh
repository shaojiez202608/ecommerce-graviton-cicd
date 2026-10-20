#!/usr/bin/env bash
# Promote one already-built image to one environment (runs inside the env's VPC in CodeBuild).
# Order matters (Magento key points 3 and 4):
#   0. verify the image: tag still exists (not quarantined) and its scan result passes the gate
#   1. register new task definition revisions that point at the promoted image tag
#   2. (optional) RDS snapshot before migration, older pre-deploy snapshots pruned to N
#   3. run ONE migration task (setup:install / setup:upgrade) and wait; abort on failure.
#      When a schema upgrade is needed the task leaves maintenance mode ON.
#   4. update the ECS service (rolling with circuit-breaker rollback, or native blue/green)
#   5. run ONE cache:flush task, which also closes the maintenance window
# On any failure after step 3 started, the EXIT trap applies the environment's
# maintenance_on_failure policy (disable | keep) through a one-off maintenance task.
# Usage: ecs_deploy.sh <ENV> <imageDetail.json>
set -euo pipefail
ENV_NAME="$1"; DETAIL="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${SSM_PREFIX:?SSM_PREFIX must be set (environment variable of the deploy project)}"
IMAGE_URI=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["imageUri"])' "$DETAIL")
IMAGE_TAG="${IMAGE_URI##*:}"; REPO_NAME="${IMAGE_URI#*/}"; REPO_NAME="${REPO_NAME%:*}"
META=$(aws ssm get-parameter --name "$PREFIX/$ENV_NAME/deploy" --query Parameter.Value --output text)
j() { python3 -c 'import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2], ""); print(",".join(v) if isinstance(v,list) else v)' "$META" "$1"; }
CLUSTER=$(j cluster); SERVICE=$(j service); WEB_FAMILY=$(j webFamily); OPS_FAMILY=$(j opsFamily)
SUBNETS=$(j subnets); SG=$(j securityGroup); DB=$(j dbInstance); SNAP=$(j snapshotBeforeMigrate)
DESIRED=$(j desiredCount); NAME_PREFIX=$(j namePrefix); SNAP_PREFIX=$(j snapshotPrefix)
SNAP_KEEP=$(j predeploySnapshotsKeep); MAINT_POLICY=$(j maintenanceOnFailure)
OPS_STARTED_BY="${NAME_PREFIX}-ops"
TASK_WAIT_ROUNDS="${TASK_WAIT_ROUNDS:-3}"   # each `aws ecs wait tasks-stopped` round polls for up to 10 minutes
echo "[deploy] env=$ENV_NAME image=$IMAGE_URI cluster=$CLUSTER service=$SERVICE"

# ---------------------------------------------------------------- 0. image gate
aws ecr describe-images --repository-name "$REPO_NAME" --image-ids imageTag="$IMAGE_TAG" >/dev/null \
  || { echo "[deploy] image tag $IMAGE_TAG not found (never built, or quarantined by the scan gate)" >&2; exit 1; }
python3 "$HERE/scan_gate.py" "$REPO_NAME" "$IMAGE_TAG" "${BLOCK_SEVERITIES:?}" "${SCAN_VERIFY_TIMEOUT_SECONDS:-120}" \
  "${SCAN_UNAVAILABLE_ACTION:?}" --verify-only

register() {  # family -> new task definition ARN with the container image replaced
  local family="$1"
  aws ecs describe-task-definition --task-definition "$family" --query taskDefinition --output json > td.json
  python3 - "$IMAGE_URI" <<'PY'
import json, sys
td = json.load(open("td.json"))
for c in td["containerDefinitions"]:
    if c["name"] == "web":
        c["image"] = sys.argv[1]
keep = ["family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions", "volumes",
        "requiresCompatibilities", "cpu", "memory", "runtimePlatform"]
json.dump({k: td[k] for k in keep if k in td}, open("td-new.json", "w"))
PY
  aws ecs register-task-definition --cli-input-json file://td-new.json --query taskDefinition.taskDefinitionArn --output text
}

run_task() {  # ops task with a command; waits (bounded) and returns the container exit code
  local td="$1" cmd="$2" label="$3" running overrides arn code reason
  running=$(aws ecs list-tasks --cluster "$CLUSTER" --started-by "$OPS_STARTED_BY" --desired-status RUNNING --query 'length(taskArns)' --output text)
  if [ "$running" != "0" ]; then echo "[deploy] another ops task is running; refusing to start a concurrent $label" >&2; return 75; fi
  overrides=$(printf '{"containerOverrides":[{"name":"web","command":["%s"]}]}' "$cmd")
  arn=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$td" --launch-type FARGATE --count 1 \
    --started-by "$OPS_STARTED_BY" \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
    --overrides "$overrides" --query 'tasks[0].taskArn' --output text)
  echo "[deploy] $label task: ${arn##*/}"
  local stopped=0
  for _ in $(seq 1 "$TASK_WAIT_ROUNDS"); do
    if aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$arn"; then stopped=1; break; fi
  done
  if [ "$stopped" = "0" ]; then
    # Timed out: stop the task (SIGTERM reaches the script through the init process; its trap cleans up)
    echo "[deploy] $label did not finish in time; stopping it" >&2
    aws ecs stop-task --cluster "$CLUSTER" --task "$arn" --reason "deploy timeout ($label)" >/dev/null
    aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$arn" || true
  fi
  code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$arn" --query 'tasks[0].containers[0].exitCode' --output text)
  reason=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$arn" --query 'tasks[0].stoppedReason' --output text)
  echo "[deploy] $label exit=$code reason=$reason"
  [ "$stopped" = "1" ] || return 124
  [ "$code" = "0" ] || return 1
  return 0
}

MIGRATION_STARTED=0; OPS_TD=""
on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "$MIGRATION_STARTED" = "1" ] && [ -n "$OPS_TD" ]; then
    # A maintenance window may be open (marker on shared var/). Apply the environment's policy.
    echo "[deploy] deployment failed after the migration step started; maintenance policy: $MAINT_POLICY" >&2
    run_task "$OPS_TD" "/usr/local/bin/magento-maintenance $MAINT_POLICY" "maintenance-$MAINT_POLICY" \
      || echo "[deploy] WARNING: could not apply maintenance policy; check var/.maintenance.flag manually" >&2
  fi
  exit "$rc"
}
trap on_exit EXIT

# ---------------------------------------------------------------- 1. task definitions
WEB_TD=$(register "$WEB_FAMILY"); OPS_TD=$(register "$OPS_FAMILY")
echo "[deploy] web=$WEB_TD ops=$OPS_TD"

# ---------------------------------------------------------------- 2. snapshot (uat/prod)
if [ "$SNAP" = "True" ] || [ "$SNAP" = "true" ]; then
  SNAP_ID="${SNAP_PREFIX}-$(date -u +%Y%m%d%H%M%S)"  # matches the IAM grant on <PREFIX>-<ENV>-predeploy-*
  aws rds create-db-snapshot --db-instance-identifier "$DB" --db-snapshot-identifier "$SNAP_ID" \
    --tags Key=Purpose,Value=predeploy Key=ImageTag,Value="$IMAGE_TAG" >/dev/null
  aws rds wait db-snapshot-available --db-snapshot-identifier "$SNAP_ID"
  echo "[deploy] snapshot $SNAP_ID ready"
  # Keep only the newest N pre-deploy snapshots of this environment.
  for old in $(aws rds describe-db-snapshots --db-instance-identifier "$DB" --snapshot-type manual \
      --query "reverse(sort_by(DBSnapshots[?starts_with(DBSnapshotIdentifier,'${SNAP_PREFIX}-')],&SnapshotCreateTime))[${SNAP_KEEP:-3}:].DBSnapshotIdentifier" \
      --output text); do
    [ "$old" = "None" ] && continue
    aws rds delete-db-snapshot --db-snapshot-identifier "$old" >/dev/null && echo "[deploy] pruned snapshot $old"
  done
fi

# ---------------------------------------------------------------- 3. migration (single task)
MIGRATION_STARTED=1
run_task "$OPS_TD" /usr/local/bin/magento-migrate migrate || { echo "[deploy] migrate failed; service left unchanged" >&2; exit 1; }

# ---------------------------------------------------------------- 4. service update
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition "$WEB_TD" \
  --desired-count "$DESIRED" --query 'service.deployments[0].id' --output text
echo "[deploy] waiting for service to become stable"
if ! aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"; then
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" || true
fi
# services-stable can return before rolloutState settles; poll the PRIMARY deployment.
for _ in $(seq 1 40); do
  read -r PRIMARY_TD ROLLOUT < <(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition,rolloutState]' --output text)
  echo "[deploy] primary=${PRIMARY_TD##*/} rollout=$ROLLOUT"
  if [ "$PRIMARY_TD" != "$WEB_TD" ]; then
    echo "[deploy] service rolled back to ${PRIMARY_TD##*/}; failing the stage" >&2; exit 1
  fi
  case "$ROLLOUT" in
    COMPLETED) break ;;
    FAILED) echo "[deploy] deployment FAILED" >&2; exit 1 ;;
  esac
  sleep 15
done
[ "$ROLLOUT" = "COMPLETED" ] || { echo "[deploy] deployment did not complete in time" >&2; exit 1; }

# ---------------------------------------------------------------- 5. cache flush + close maintenance
run_task "$OPS_TD" /usr/local/bin/magento-cache-flush cache-flush || { echo "[deploy] cache-flush failed" >&2; exit 1; }
MIGRATION_STARTED=0
aws ssm put-parameter --name "$PREFIX/$ENV_NAME/current_image_tag" --type String --overwrite \
  --value "$IMAGE_TAG" >/dev/null
echo "[deploy] $ENV_NAME now runs $IMAGE_URI"
